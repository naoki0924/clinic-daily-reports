"""抽出→検証→保存。

process_post   : 1投稿を分類し、本文とExcelから観測値を作る（同じ出典の行は置き換え＝冪等）
reconcile_day  : 院×日 の採用値（daily_fact）を作り直す。本文とExcelは一度だけ計上する
refresh_days   : 未提出／休診／取得失敗／要確認 の日別状態を作る
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

from .bootstrap import active_rule
from .config import settings
from .issues import IssueTracker
from .models import (
    DIM_COLS, CalendarFlag, Clinic, DailyFact, DayStatus, Issue, ManualAdjustment, Observation, Report,
    SourceComment, SourceFile, SourcePost,
)
from .normalize import nfkc, squash
from .parsing.body import parse_body
from .parsing.excel import ExcelFormatError, WorkbookResult, parse_workbook, recalc_with_libreoffice
from .rules import RECONCILE_METRICS
from .security import decrypt_text, load_file
from .targetdate import decide

CORRECTION_RE = re.compile(r"訂正|修正|差し?替え|再送|再投稿|誤り")
COMMENT_HINT_RE = re.compile(r"訂正|修正|おかし|違い|間違|差し引|差引|正しく|再計算|二重|重複|足して")
EXCEL_EXT = (".xlsx", ".xlsm")
LATER_WINDOW_DAYS = 40  # 累積型Excelで、後日に過去の日の値が書き換えられるのを検知する範囲

_wb_cache: dict[tuple, WorkbookResult | Exception] = {}


def dims_of(row) -> dict:
    return {c: getattr(row, c) or "" for c in DIM_COLS}


def dims_tuple(d: dict) -> tuple:
    return tuple((d.get(c) or "") for c in DIM_COLS)


def dims_label(d: dict) -> str:
    return " / ".join(f"{v}" for c in DIM_COLS if (v := d.get(c)))


# ---------------------------------------------------------------------------
# 分類
# ---------------------------------------------------------------------------

def classify(text: str, rule: dict, has_excel: bool = False) -> tuple[str, str]:
    c = rule.get("classify", {})
    if not text.strip():
        return ("attachment_only", "本文がなく添付だけの投稿（前後の日報の補足として扱う）") if has_excel             else ("non_report", "本文なし")
    head = "\n".join(nfkc(text).strip().splitlines()[:3])
    for kw in c.get("other_clinic", []):
        if kw in head:
            return "other_clinic", f"冒頭に「{kw}」（他院の日報）"
    first = nfkc(text).strip().splitlines()[0] if text.strip() else ""
    for kw in c.get("monthly", []):
        if kw in first:
            return "monthly", f"1行目に「{kw}」（月次集計の投稿）"
    norm = nfkc(text)
    if any(nfkc(k) in norm for k in c.get("must", [])):
        return "daily", "日報の見出しあり"
    return "non_report", "日報の見出しがない"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def _load_wb(f: SourceFile, rule: dict, months: set[tuple[int, int]], ver: int) -> WorkbookResult:
    key = (f.sha256, ver, frozenset(months))
    if key in _wb_cache:
        res = _wb_cache[key]
        if isinstance(res, Exception):
            raise res
        return res
    data = load_file(f.storage_path)
    try:
        res = parse_workbook(data, rule, months)
        missing = [o for d in res.days.values() for o in d.obs if o.num.suspicious.startswith("数式の計算結果が保存")]
        if missing and settings.libreoffice_path:
            res = parse_workbook(recalc_with_libreoffice(data, settings.libreoffice_path), rule, months)
            res.warnings.append("計算結果が無い数式があったため LibreOffice で再計算して読み取った")
    except Exception as e:
        _wb_cache[key] = e
        raise
    if len(_wb_cache) > 64:
        _wb_cache.clear()
    _wb_cache[key] = res
    return res


def _months_around(d: date) -> set[tuple[int, int]]:
    prev = (d.replace(day=1) - timedelta(days=1))
    return {(d.year, d.month), (prev.year, prev.month)}


EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # 空の本文
LINK_WINDOW = timedelta(hours=24)


def _nearest_daily(s, post: SourcePost) -> SourcePost | None:
    """本文が空の添付だけの投稿を、同じ院の最も近い日報の投稿に結び付ける。"""
    cands = s.scalars(select(SourcePost).where(
        SourcePost.clinic_code == post.clinic_code, SourcePost.body_hash != EMPTY_HASH, SourcePost.deleted.is_(False),
        SourcePost.posted_at >= post.posted_at - LINK_WINDOW, SourcePost.posted_at <= post.posted_at + LINK_WINDOW,
        SourcePost.category.in_(("daily", "unclassified")))).all()
    return min(cands, key=lambda p: abs((p.posted_at - post.posted_at).total_seconds()), default=None)


def _excel_files(s, post: SourcePost) -> list[SourceFile]:
    files = list(s.scalars(select(SourceFile).where(SourceFile.post_pk == post.id)).all())
    for other in s.scalars(select(SourcePost).where(
            SourcePost.clinic_code == post.clinic_code, SourcePost.body_hash == EMPTY_HASH, SourcePost.deleted.is_(False),
            SourcePost.posted_at >= post.posted_at - LINK_WINDOW, SourcePost.posted_at <= post.posted_at + LINK_WINDOW)):
        near = _nearest_daily(s, other)
        if near is not None and near.post_id == post.post_id:
            files += s.scalars(select(SourceFile).where(SourceFile.post_pk == other.id)).all()
    return sorted([f for f in files if f.file_name.lower().endswith(EXCEL_EXT)], key=lambda f: (f.attached_at, f.file_id))


# ---------------------------------------------------------------------------
# 投稿の処理
# ---------------------------------------------------------------------------

def process_post(s, post: SourcePost) -> set[tuple[str, date]]:
    clinic = post.clinic_code
    affected: set[tuple[str, date]] = set()
    tracker = IssueTracker(s, f"post:{post.post_id}:")
    old = s.scalar(select(Report).where(Report.post_id == post.post_id))
    if old and old.target_date:
        affected.add((clinic, old.target_date))
    s.execute(delete(Observation).where(Observation.post_id == post.post_id,
                                        Observation.source_kind.in_(("body", "excel", "excel_later"))))

    if post.deleted:
        if old:
            s.delete(old)
        tracker.close_untouched()
        return affected

    text = decrypt_text(post.body_enc)
    body_ver, body_rule = active_rule(s, clinic, "body")
    xl_ver, xl_rule = active_rule(s, clinic, "excel")
    has_excel = bool(s.scalar(select(SourceFile.id).where(SourceFile.post_pk == post.id).limit(1)))
    cat, reason = classify(text, body_rule, has_excel)
    post.category, post.category_reason = cat, reason
    if cat == "attachment_only":
        if old:
            s.delete(old)
        near = _nearest_daily(s, post)
        tracker.close_untouched()
        if near is not None and near.post_id != post.post_id:
            post.category_reason = f"{reason}：投稿 {near.post_id}（{near.posted_at:%m/%d %H:%M}）に結び付け"
            s.flush()
            affected |= process_post(s, near)
        return affected
    if cat != "daily":
        for f in s.scalars(select(SourceFile).where(SourceFile.post_pk == post.id, SourceFile.status == "stored")):
            f.status, f.error = "skipped", f"日報以外の投稿（{reason}）の添付"
        if old:
            s.delete(old)
        if cat == "other_clinic":
            tracker.add("other", clinic=clinic, kind="other_clinic", severity="info", post_id=post.post_id,
                        title=f"このノートに他院の日報と思われる投稿があります（集計から除外）：{reason}")
        tracker.close_untouched()
        return affected

    body = parse_body(text, body_rule)
    files = _excel_files(s, post)
    hint = xl_rule.get("file_name")
    if hint:
        for f in files:
            if not re.search(hint, nfkc(f.file_name)):
                f.status, f.error = "skipped", f"日報のExcelではない（ファイル名に「{hint}」がない）"
        files = [f for f in files if re.search(hint, nfkc(f.file_name))]
    usable = [f for f in files if f.storage_path]
    primary = usable[-1] if usable else None  # コメントに添付された修正版を含め、最新の版を使う
    for f in usable[:-1]:
        if f.status in ("stored", "parsed"):
            f.status, f.error = "superseded", f"新しい版（ファイルID {primary.file_id}）に置き換え"
    if not files:
        tracker.add("noexcel", clinic=clinic, kind="excel_missing", severity="info", post_id=post.post_id,
                    title="日報にExcelの添付がありません（本文のみで計上）")
    elif primary is None:
        tracker.add("excel_fetch", clinic=clinic, kind="fetch_failed", severity="error", post_id=post.post_id,
                    file_id=files[-1].file_id, title=f"添付Excel「{files[-1].file_name}」を取得できていません")

    guess = posted = post.posted_at.date()
    if body.date:
        try:
            guess = date(body.date.year or posted.year, body.date.month, body.date.day)
        except ValueError:
            pass
    months = _months_around(posted) | _months_around(guess)

    wb: WorkbookResult | None = None
    if primary:
        try:
            wb = _load_wb(primary, xl_rule, months, xl_ver)
            primary.status, primary.error = "parsed", ""
            prev_sig = primary.format_signature
            primary.format_signature, primary.parsed_rule_version = wb.signature, xl_ver
            last = s.scalar(select(SourceFile).join(SourcePost, SourceFile.post_pk == SourcePost.id)
                            .where(SourcePost.clinic_code == clinic, SourceFile.format_signature != "",
                                   SourceFile.file_id != primary.file_id, SourceFile.attached_at < primary.attached_at)
                            .order_by(SourceFile.attached_at.desc()))
            if last and last.format_signature != wb.signature and prev_sig != wb.signature:
                tracker.add("fmtchange", clinic=clinic, kind="excel_format", severity="info", post_id=post.post_id,
                            file_id=primary.file_id,
                            title="Excelの列構成が前回と変わりました（見出しで列を特定して読み取り済み。値を確認してください）",
                            detail={"previous_file": last.file_id, "previous_signature": last.format_signature,
                                    "signature": wb.signature})
            for i, w in enumerate(wb.warnings[:10]):
                tracker.add(f"xlwarn{i}", clinic=clinic, kind="excel_format", severity="info", post_id=post.post_id,
                            file_id=primary.file_id, title=f"Excelの注意: {w}")
        except ExcelFormatError as e:
            primary.status, primary.error = "failed", str(e)
            tracker.add("xlerror", clinic=clinic, kind="excel_format", severity="error", post_id=post.post_id,
                        file_id=primary.file_id,
                        title="Excelを読み取れませんでした（書式変更の疑い。誤ったセルから取り込まないよう停止）",
                        detail={"error": str(e), "file_name": primary.file_name})

    excel_last = None
    if wb:
        cands = [d for d in (wb.last_input_day(y, m) for (y, m) in months) if d and d <= posted + timedelta(days=1)]
        excel_last = max(cands) if cands else None
    decision = decide(post.posted_at, body.date, [f.file_name for f in files], excel_last)
    target = decision.target

    if wb and target and (target.year, target.month) not in wb.layouts and primary:
        try:
            wb = _load_wb(primary, xl_rule, months | {(target.year, target.month)}, xl_ver)
        except ExcelFormatError as e:
            tracker.add("xlerror2", clinic=clinic, kind="excel_format", severity="error", post_id=post.post_id,
                        file_id=primary.file_id, title=f"対象月のシートを読み取れません: {e}")

    report = old or Report(post_id=post.post_id, clinic_code=clinic, date_status="review", date_evidence={})
    report.clinic_code = clinic
    report.target_date = target
    report.primary_file_id = primary.file_id if primary else None
    report.date_status = decision.status
    report.date_evidence = {**decision.evidence, "problems": decision.problems,
                            "body_rule_version": body_ver, "excel_rule_version": xl_ver}
    report.relation = "original" if report.relation in (None, "") else report.relation
    if old is None:
        s.add(report)
    if decision.status == "review":
        tracker.add("date", clinic=clinic, kind="date_review", target_date=target, post_id=post.post_id,
                    title=f"対象日の判定が要確認（採用候補 {target or '不明'}）",
                    detail={"problems": decision.problems, "candidates": decision.evidence.get("candidates", [])})
    if target is None:
        tracker.close_untouched()
        return affected

    for o in body.observations:
        loc = f"本文{o.line_no}行目「{o.excerpt}」" if o.line_no else "本文（記載なし）"
        s.add(Observation(clinic_code=clinic, target_date=target, metric=o.metric, **_dims(o.dims),
                          value=o.num.value, state=o.num.state, raw=o.num.raw[:200],
                          suspicious="; ".join(filter(None, [o.num.suspicious, *[n for n in o.notes if "確認中" in n or "複数" in n]])),
                          source_kind="body", post_id=post.post_id, location=loc[:255], rule_version=body_ver,
                          definition_confirmed=o.definition_confirmed))

    closed = False
    if wb:
        row = wb.days.get(target)
        if row is None:
            tracker.add("norow", clinic=clinic, kind="excel_format", severity="warn", target_date=target,
                        post_id=post.post_id, file_id=primary.file_id,
                        title=f"添付Excelに対象日（{target}）の行が見つかりません")
        else:
            closed = row.closed
            for eo in row.obs:
                s.add(Observation(clinic_code=clinic, target_date=target, metric=eo.metric, **_dims(eo.dims),
                                  value=eo.num.value, state=eo.num.state, raw=(eo.num.raw or "")[:200],
                                  suspicious=eo.num.suspicious, source_kind="excel", post_id=post.post_id,
                                  file_id=primary.file_id,
                                  location=(f"{primary.file_name} / {eo.location}" + (f"（数式 {eo.formula[:60]}）" if eo.formula else "")
                                            + (f"［{'・'.join(eo.notes)}］" if eo.notes else ""))[:255],
                                  rule_version=xl_ver, definition_confirmed=eo.definition_confirmed))
                if eo.num.suspicious:
                    tracker.add(f"formula:{eo.cell}", clinic=clinic, kind="excel_formula", target_date=target,
                                post_id=post.post_id, file_id=primary.file_id, metric=eo.metric,
                                title=f"{eo.location}: {eo.num.suspicious}")
        # Excelの月合計行と、日別行の合計の照合（合計行は計上には使わない）
        tym = (target.year, target.month)
        for key, tot in (wb.month_totals.get(tym) or {}).items():
            if tot is None or not key.endswith("{}") or key[:-2] not in ("sales_net", "sales_gross", "receipt"):
                continue
            ds = sum(o.num.value or 0 for dr in wb.days.values() if (dr.day.year, dr.day.month) == tym
                     for o in dr.obs if f"{o.metric}{{}}" == key and not o.dims)
            from .issues import upsert_issue
            mkey = f"monthtotal:{clinic}:{tym[0]}-{tym[1]:02d}:{key}"
            prev = s.scalar(select(Issue).where(Issue.dedupe_key == mkey))
            if prev and (prev.detail or {}).get("attached_at", "") > primary.attached_at.isoformat():
                continue  # より新しい添付で判定済み
            if abs(ds - tot) <= 0.5:
                if prev and prev.status == "open":
                    prev.status, prev.resolution, prev.resolved_by = "resolved", "最新の添付で一致", "system"
                continue
            if True:
                # 同じ月の合計行は毎日の添付に含まれるので、院×月で1件にまとめる（最新の添付の値で更新）
                upsert_issue(s, f"monthtotal:{clinic}:{tym[0]}-{tym[1]:02d}:{key}", clinic=clinic, kind="inconsistency", target_date=target,
                            post_id=post.post_id, file_id=primary.file_id, metric=key[:-2],
                            title=f"Excelの月合計行（{tot:,.0f}）と日別行の合計（{ds:,.0f}）が一致しません：{key[:-2]}（{tym[0]}年{tym[1]}月）",
                            detail={"total_row": tot, "day_sum": ds, "diff": ds - tot,
                                    "attached_at": primary.attached_at.isoformat(), "file_name": primary.file_name})
        # 後日のExcelに含まれる過去日の値（訂正の検知用。計上はしない）
        for d, drow in wb.days.items():
            if 1 <= (target - d).days <= LATER_WINDOW_DAYS:
                for eo in drow.obs:
                    if eo.metric in RECONCILE_METRICS:
                        s.add(Observation(clinic_code=clinic, target_date=d, metric=eo.metric, **_dims(eo.dims),
                                          value=eo.num.value, state=eo.num.state, raw=(eo.num.raw or "")[:200],
                                          source_kind="excel_later", post_id=post.post_id, file_id=primary.file_id,
                                          location=f"{primary.file_name} / {eo.location}"[:255], rule_version=xl_ver))
                affected.add((clinic, d))
        # Excelの休診欄
        for d, drow in wb.days.items():
            if drow.closed and (posted - d).days <= 62:
                flag = s.scalar(select(CalendarFlag).where(CalendarFlag.clinic_code == clinic, CalendarFlag.target_date == d))
                if flag is None:
                    s.add(CalendarFlag(clinic_code=clinic, target_date=d, closed=True,
                                       source=f"{primary.file_name} / {drow.row}行目"))
    report.is_closed_day = closed

    for c in s.scalars(select(SourceComment).where(SourceComment.post_id == post.post_id)):
        if c.is_correction_hint:
            tracker.add(f"comment:{c.comment_id}", clinic=clinic, kind="comment_correction", target_date=target,
                        post_id=post.post_id,
                        title="コメントで数値の誤り・訂正の指摘があります（内容を確認し、必要なら採用値を修正してください）",
                        detail={"comment_id": c.comment_id, "commented_at": c.posted_at.isoformat()})

    affected.add((clinic, target))
    tracker.close_untouched()
    return affected


def _dims(d: dict) -> dict:
    return {c: (d.get(c) or "")[:64] for c in DIM_COLS}


# ---------------------------------------------------------------------------
# 採用値
# ---------------------------------------------------------------------------

def _num(o: Observation | None) -> float | None:
    return o.value if o is not None and o.state in ("value", "zero") else None


def _active_reports(s, clinic: str, d: date) -> tuple[Report | None, list[Report], bool]:
    reps = s.execute(select(Report, SourcePost).join(SourcePost, SourcePost.post_id == Report.post_id)
                     .where(Report.clinic_code == clinic, Report.target_date == d, SourcePost.deleted.is_(False),
                            SourcePost.category == "daily")
                     .order_by(SourcePost.posted_at)).all()
    if not reps:
        return None, [], False
    if len(reps) == 1:
        r = reps[0][0]
        r.relation, r.superseded_by = "original", None
        return r, [r], False
    base, base_post = reps[0]
    base.relation, base.superseded_by = "original", None
    ambiguous = False
    for r, p in reps[1:]:
        r.superseded_by = None
        head = "\n".join(decrypt_text(p.body_enc).splitlines()[:3])
        if CORRECTION_RE.search(nfkc(head)):
            r.relation = "correction"
            base.superseded_by = r.id
            base, base_post = r, p
            continue
        if _same_values(s, base.post_id, r.post_id, d):
            r.relation = "repost"
            r.superseded_by = base.id
            continue
        r.relation = "review"
        ambiguous = True
    return base, [x[0] for x in reps], ambiguous


def _same_values(s, post_a: int, post_b: int, d: date) -> bool:
    def vals(pid):
        rows = s.scalars(select(Observation).where(Observation.post_id == pid, Observation.target_date == d,
                                                   Observation.source_kind == "body")).all()
        return {(o.metric, dims_tuple(dims_of(o))): (o.state, o.value) for o in rows}
    return vals(post_a) == vals(post_b)


def reconcile_day(s, clinic: str, d: date) -> None:
    tracker = IssueTracker(s, f"day:{clinic}:{d.isoformat()}:")
    s.execute(delete(DailyFact).where(DailyFact.clinic_code == clinic, DailyFact.target_date == d))
    active, reports, ambiguous = _active_reports(s, clinic, d)
    manual = {(m.metric, dims_tuple(m.dims or {})): m for m in s.scalars(
        select(ManualAdjustment).where(ManualAdjustment.clinic_code == clinic, ManualAdjustment.target_date == d,
                                       ManualAdjustment.active.is_(True)).order_by(ManualAdjustment.created_at))}
    if active is None and not manual:
        tracker.close_untouched()
        return
    if ambiguous:
        tracker.add("dup", clinic=clinic, kind="duplicate_report", target_date=d,
                    title="同じ日の日報が複数あり、訂正・再投稿・追加実績のどれか判定できません（最初の投稿の値を要確認として表示）",
                    detail={"post_ids": [r.post_id for r in reports]})

    obs = []
    if active:
        obs = s.scalars(select(Observation).where(Observation.post_id == active.post_id, Observation.target_date == d,
                                                  Observation.source_kind.in_(("body", "excel")))).all()
    active_posted = None
    if active:
        active_posted = s.scalar(select(SourcePost.posted_at).where(SourcePost.post_id == active.post_id))
    later_rows = s.execute(select(Observation, SourcePost.posted_at).join(SourcePost, SourcePost.post_id == Observation.post_id)
                           .where(Observation.clinic_code == clinic, Observation.target_date == d,
                                  Observation.source_kind == "excel_later")
                           .order_by(SourcePost.posted_at)).all()
    later: dict[tuple, Observation] = {}
    for o, posted_at in later_rows:
        if active_posted is None or posted_at > active_posted:
            later[(o.metric, dims_tuple(dims_of(o)))] = o  # 最も新しいファイルの値

    grouped: dict[tuple, dict[str, Observation]] = defaultdict(dict)
    for o in obs:
        grouped[(o.metric, dims_tuple(dims_of(o)))][o.source_kind] = o
    for k in manual:
        grouped.setdefault(k, {})
    if active:
        for k in later:  # 当日のExcelに無い項目も、後日のExcelにあれば候補にする
            grouped.setdefault(k, {})

    for (metric, dt), src in grouped.items():
        dims = dict(zip(DIM_COLS, dt))
        b, x = src.get("body"), src.get("excel")
        bv, xv = _num(b), _num(x)
        status, value, state, adopted, note = "confirmed", None, "absent", None, ""
        m = manual.get((metric, dt))
        if m:
            value, state, adopted = m.after_value, m.after_state, m.source_obs_id
            status, note = "confirmed", f"手動で採用（{m.user}）: {m.reason}"
            fact = DailyFact(clinic_code=clinic, target_date=d, metric=metric, **dims, value=value, state=state,
                             status=status, adopted_obs_id=adopted, adopted_by=m.user, note=note[:500])
            s.add(fact)
            continue
        if bv is not None and xv is not None:
            if abs(bv - xv) <= 0.5:
                value, state, adopted = bv, b.state, b.id
                note = "本文とExcelが一致"
            else:
                status, state = "review", "conflict"
                note = f"本文 {bv:,.0f} / Excel {xv:,.0f}（差 {bv - xv:+,.0f}）"
                hint = _explain_diff(obs, abs(bv - xv), metric)
                if hint:
                    note += f"／{hint}"
                tracker.add(f"mismatch:{metric}:{'|'.join(dt)}", clinic=clinic, kind="body_excel_mismatch",
                            target_date=d, metric=metric, post_id=active.post_id if active else None,
                            file_id=x.file_id,
                            title=f"本文とExcelの値が異なります：{metric}{(' ' + dims_label(dims)) if dims_label(dims) else ''}"
                                  + (f"（{hint}）" if hint else ""),
                            detail={"body": {"value": bv, "raw": b.raw, "location": b.location, "obs_id": b.id},
                                    "excel": {"value": xv, "raw": x.raw, "location": x.location, "obs_id": x.id},
                                    "diff": bv - xv})
        elif xv is None and bv is None and (lo := later.get((metric, dt))) is not None and _num(lo) is not None                 and (x is None or x.state in ("blank", "absent")):
            # 当日の添付にExcelがない（または空欄）→ 後日の累積Excelにある当日の行の値を暫定で採用
            value, state, adopted, status = lo.value, lo.state, lo.id, "provisional"
            note = f"当日の添付Excelに値がないため、後日のExcelの値を暫定採用（{lo.location}）"
        elif bv is not None or xv is not None:
            o = b if bv is not None else x
            value, state, adopted = o.value, o.state, o.id
            other = x if o is b else b
            if o is x and "数式の結果の0" in (x.location or "") and other is not None and other.state in ("dash", "blank", "absent"):
                status = "provisional"
                note = f"Excelの数式結果0を採用（本文は「{other.raw or other.state}」）"
            else:
                note = "本文のみ" if o is b else "Excelのみ"
        else:
            o = b or x
            state = o.state if o else "absent"
            if state == "error":
                status = "review"
            note = "記載なし／空欄／「-」（0とはみなさない）" if state in ("blank", "dash", "absent") else ""

        for o in (b, x):
            if o is not None and o.suspicious:
                status = "review"
                tracker.add(f"susp:{o.source_kind}:{metric}:{'|'.join(dt)}", clinic=clinic, kind="suspicious_value",
                            target_date=d, metric=metric, post_id=o.post_id, file_id=o.file_id,
                            title=f"誤記・計算の疑い：{metric}（{o.source_kind}）{o.suspicious}",
                            detail={"raw": o.raw, "location": o.location, "value": o.value})
        if any(o is not None and not o.definition_confirmed for o in (b, x)) and status == "confirmed":
            status = "provisional"
            note = (note + "／定義未確認").lstrip("／")
        if metric == "deposit" and status == "confirmed" and value is not None:
            status = "provisional"
            note = (note + "／デポジットの性質（新規預り・利用）が未確定").lstrip("／")

        lo = later.get((metric, dt))
        if lo is not None and value is not None and _num(lo) is not None and abs(_num(lo) - value) > 0.5:
            status = "review"
            tracker.add(f"later:{metric}:{'|'.join(dt)}", clinic=clinic, kind="excel_later_change", target_date=d,
                        metric=metric, post_id=lo.post_id, file_id=lo.file_id,
                        title=f"後日の日報に添付されたExcelで {d:%m/%d} の値が変わっています：{metric}",
                        detail={"adopted": value, "later": _num(lo), "later_location": lo.location})
        if ambiguous and status == "confirmed":
            status = "review"
        if active and active.date_status == "review" and status == "confirmed":
            status = "review"
        s.add(DailyFact(clinic_code=clinic, target_date=d, metric=metric, **dims, value=value, state=state,
                        status=status, adopted_obs_id=adopted, note=note[:500]))
    s.flush()
    if active:
        _consistency(s, tracker, clinic, d, active.post_id)
    tracker.close_untouched()


_DIFF_LABELS = {"deposit": "デポジット", "transfer": "振込", "ar_new": "売掛", "offset": "相殺", "cpc_amount": "CPC受託"}


def _explain_diff(obs: list, diff: float, metric: str) -> str:
    """差額が同じ日報の他の項目（デポジット・振込等）と一致すれば、定義の違いの可能性として示す（推測で値は変えない）。"""
    if diff <= 0.5:
        return ""
    for o in obs:
        if o.metric in _DIFF_LABELS and o.metric != metric and _num(o) is not None and abs(_num(o) - diff) <= 0.5:
            src = "本文" if o.source_kind == "body" else "Excel"
            return f"差額が{src}の{_DIFF_LABELS[o.metric]} {diff:,.0f} と一致（定義の違いの可能性）"
    return ""


def _consistency(s, tracker: IssueTracker, clinic: str, d: date, post_id: int) -> None:
    """合計と内訳、新規人数と総人数などの矛盾を検出する（原文の数値は修正しない）。"""
    rows = s.scalars(select(Observation).where(Observation.post_id == post_id, Observation.target_date == d,
                                               Observation.source_kind == "body")).all()
    by = defaultdict(list)
    for o in rows:
        by[o.metric].append(o)

    def single(metric):
        for o in by.get(metric, []):
            if not any(dims_of(o).values()):
                return _num(o)
        return None

    total, new = single("visitors_total"), single("visitors_new")
    segs = [_num(o) for o in by.get("visitors_segment", []) if _num(o) is not None]
    if total is not None and segs and abs(sum(segs) - total) > 0:
        tracker.add("visitors_seg", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id,
                    metric="visitors_total",
                    title=f"来院者合計 {total:.0f}名 と 顧客区分の内訳合計 {sum(segs):.0f}名 が一致しません",
                    detail={"total": total, "segments_sum": sum(segs)})
    if total is not None and new is not None and new > total:
        tracker.add("visitors_new", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id,
                    metric="visitors_new",
                    title=f"新規来院 {new:.0f} が 来院者合計 {total:.0f} を上回っています",
                    detail={"new": new, "total": total})
    cfg = (active_rule(s, clinic, "body")[1].get("consistency") or {})
    receipt = single(cfg.get("receipt_metric", "receipt"))
    comps = cfg.get("receipt_components", ["payment"])
    excl = set(cfg.get("exclude_methods", ["デポジット", "売掛", "相殺"]))
    pays = [o for m in comps for o in by.get(m, []) if _num(o) is not None
            and not (m == "payment" and o.method in excl) and (m == "payment" or not any(dims_of(o).values()))]
    if receipt is not None and any(o.metric == "payment" for o in pays):
        ps = sum(_num(o) for o in pays)
        if abs(ps - receipt) > 0.5:
            tracker.add("receipt_pay", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id,
                        metric="receipt",
                        title=f"本文の入金 {receipt:,.0f}円 と 決済方法別の合計 {ps:,.0f}円 が一致しません",
                        detail={"receipt": receipt, "payments_sum": ps, "rule": comps,
                                "payments": {f"{o.metric} {o.market} {o.method}".strip(): o.value for o in pays}})
    inq_total = single("inquiries")
    inq_cat = [_num(o) for o in by.get("inquiries", []) if o.category and _num(o) is not None]
    if inq_total is not None and inq_cat and abs(sum(inq_cat) - inq_total) > 0:
        tracker.add("inq", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id, metric="inquiries",
                    title=f"問い合わせ合計 {inq_total:.0f}件 と カテゴリ別の合計 {sum(inq_cat):.0f}件 が一致しません",
                    detail={"total": inq_total, "sum": sum(inq_cat)})
    xs = {o.metric: _num(o) for o in s.scalars(select(Observation).where(
        Observation.post_id == post_id, Observation.target_date == d, Observation.source_kind == "excel",
        Observation.market == "", Observation.category == "", Observation.method == ""))}
    parts = [_num(o) for o in s.scalars(select(Observation).where(
        Observation.post_id == post_id, Observation.target_date == d, Observation.source_kind == "excel",
        Observation.metric == "market_sales_net")) if _num(o) is not None]
    if parts and xs.get("sales_net") is not None and abs(sum(parts) - xs["sales_net"]) > 0.5:
        tracker.add("market_split", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id,
                    metric="market_sales_net",
                    title=f"Excelの国内・インバウンド等の合計 {sum(parts):,.0f}円 が 売上（手数料抜き） {xs['sales_net']:,.0f}円 と一致しません（列の読み違い・書式変更の疑い）",
                    detail={"parts_sum": sum(parts), "sales_net": xs["sales_net"]})
    if xs.get("sales_gross") is not None and xs.get("sales_net") is not None and xs["sales_net"] > xs["sales_gross"] + 0.5:
        tracker.add("gross_net", clinic=clinic, kind="inconsistency", target_date=d, post_id=post_id, metric="sales_net",
                    title="Excelの売上（手数料抜き）が手数料込みを上回っています",
                    detail={"gross": xs["sales_gross"], "net": xs["sales_net"]})


# ---------------------------------------------------------------------------
# 日別の状態
# ---------------------------------------------------------------------------

def refresh_days(s, clinic_code: str, start: date, end: date, today: date,
                 failed_dates: set[date] | None = None) -> None:
    clinic = s.scalar(select(Clinic).where(Clinic.code == clinic_code))
    failed_dates = failed_dates or set()
    d = start
    while d <= end:
        st = s.scalar(select(DayStatus).where(DayStatus.clinic_code == clinic_code, DayStatus.target_date == d))
        if st is None:
            st = DayStatus(clinic_code=clinic_code, target_date=d, status="missing")
            s.add(st)
        if clinic.open_from and d < clinic.open_from:
            st.status, st.note, st.report_id = "preopen", f"開院前（開院日 {clinic.open_from}）", None
            d += timedelta(days=1)
            continue
        rep, _, _ = _active_reports(s, clinic_code, d)
        cal_closed = _is_calendar_closed(s, clinic, d)
        open_issues = s.scalar(select(Issue.id).where(Issue.clinic_code == clinic_code, Issue.target_date == d,
                                                      Issue.status == "open", Issue.severity != "info").limit(1))
        review_fact = s.scalar(select(DailyFact.id).where(DailyFact.clinic_code == clinic_code, DailyFact.target_date == d,
                                                          DailyFact.status == "review").limit(1))
        if rep is not None:
            st.report_id = rep.id
            if rep.is_closed_day:
                st.status, st.note = "closed", "日報で休診"
            elif rep.date_status == "review" or open_issues or review_fact:
                st.status, st.note = "review", "要確認事項あり"
            else:
                st.status, st.note = "submitted", ""
        else:
            st.report_id = None
            if d in failed_dates:
                st.status, st.note = "failed", "取得に失敗"
            elif cal_closed:
                st.status, st.note = "closed", cal_closed
            elif d >= today:
                st.status, st.note = "future", "未到来"
            else:
                st.status, st.note = "missing", "日報が見つからない"
        d += timedelta(days=1)


def _is_calendar_closed(s, clinic: Clinic, d: date) -> str:
    iso = d.isoformat()
    if iso in (clinic.open_dates or []):
        return ""
    if iso in (clinic.closed_dates or []):
        return "休診日（設定）"
    if d.weekday() in (clinic.closed_weekdays or []):
        return "定休日（設定）"
    flag = s.scalar(select(CalendarFlag).where(CalendarFlag.clinic_code == clinic.code, CalendarFlag.target_date == d))
    if flag and flag.closed:
        return f"休診（Excelの休診欄: {flag.source}）"
    return ""


def is_correction_comment(text: str) -> bool:
    return bool(COMMENT_HINT_RE.search(nfkc(text)))


def reprocess(s, clinic_codes: list[str], start: date, end: date) -> set[tuple[str, date]]:
    """読み取りルール変更後の再解析。保存済みの原文・原本から作り直す（Talknoteには接続しない）。"""
    affected: set[tuple[str, date]] = set()
    posts = s.scalars(select(SourcePost).where(SourcePost.clinic_code.in_(clinic_codes),
                                               SourcePost.posted_at >= datetime.combine(start - timedelta(days=3), datetime.min.time()),
                                               SourcePost.posted_at < datetime.combine(end + timedelta(days=45), datetime.min.time()))).all()
    for p in posts:
        affected |= process_post(s, p)
    s.flush()
    return {(c, d) for c, d in affected if start - timedelta(days=LATER_WINDOW_DAYS) <= d <= end} | \
           {(c, d) for c in clinic_codes for d in _dates(start, end)}


def _dates(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def check_against_latest(s, clinic: str, start: date, end: date) -> int:
    """院の最新の添付Excel（累積型）にある全日の値と、採用値を照合する。
    何週間も後にExcelの過去の日が書き換えられた場合も検知する（値は変えず、要確認にする）。"""
    f = s.scalar(select(SourceFile).join(SourcePost, SourceFile.post_pk == SourcePost.id)
                 .where(SourcePost.clinic_code == clinic, SourceFile.status == "parsed")
                 .order_by(SourceFile.attached_at.desc()))
    if f is None:
        return 0
    ver, rule = active_rule(s, clinic, "excel")
    months = {(d.year, d.month) for d in _dates(start, end)}
    try:
        wb = _load_wb(f, rule, months, ver)
    except ExcelFormatError:
        return 0
    tracker = IssueTracker(s, f"latest:{clinic}:")
    n = 0
    for d, row in wb.days.items():
        if not (start <= d <= end):
            continue
        for eo in row.obs:
            if eo.metric not in RECONCILE_METRICS or any(eo.dims.values()) or eo.num.value is None:
                continue
            fact = s.scalar(select(DailyFact).where(DailyFact.clinic_code == clinic, DailyFact.target_date == d,
                                                    DailyFact.metric == eo.metric, DailyFact.market == "",
                                                    DailyFact.cust == "", DailyFact.category == "",
                                                    DailyFact.channel == "", DailyFact.method == ""))
            if fact is None or fact.value is None or fact.adopted_by != "auto":
                continue
            if abs(fact.value - eo.num.value) > 0.5:
                n += 1
                tracker.add(f"{d}:{eo.metric}", clinic=clinic, kind="excel_later_change", target_date=d,
                            metric=eo.metric, file_id=f.file_id,
                            title=f"最新のExcelで {d:%m/%d} の値が採用値と異なります：{eo.metric}（採用 {fact.value:,.0f} ／ 最新 {eo.num.value:,.0f}）",
                            detail={"adopted": fact.value, "latest": eo.num.value,
                                    "latest_location": f"{f.file_name} / {eo.location}"})
    tracker.close_untouched()
    return n
