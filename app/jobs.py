"""自動実行（毎日00:10 JST）と手動実行のジョブ。

1. 接続確認 → 2. 4院の新規・更新投稿の取得 → 3. 添付Excelの取得・解析 → 4. 対象日と数値の照合
→ 5. 重複排除・不整合検出 → 6. 日次データ保存 → 7. 月次・年次の再集計 → 8. 分析コメント → 9. 結果の記録・通知

- DBのロックで多重実行を防ぐ（期限つき。異常終了しても期限後に解放）
- 各段階は冪等。途中で失敗しても、次回（または「失敗分を再実行」）で重複登録せずに続きから処理する
- 停止期間があっても、前回成功した範囲の続きから補完する
"""
from __future__ import annotations

import logging
import os
import socket
import traceback
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

from . import aggregate
from .analysis import generate_daily
from .bootstrap import get_setting, put_setting
from .collector.sync import collect_clinic
from .collector.talknote import Credentials, LoginFailed, ReauthRequired, SessionExpired, TalknoteClient, TalknoteError
from .db import session_scope
from .issues import upsert_issue
from .models import Clinic, JobLock, JobRun, JobStep, SourceFile, SourcePost
from .notify import notify
from .pipeline import process_post, reconcile_day, refresh_days
from .security import get_secret, put_secret

log = logging.getLogger("jobs")
HOLDER = f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# ロック
# ---------------------------------------------------------------------------

def acquire_lock(name: str = "collect", ttl_min: int = 90) -> bool:
    now = datetime.utcnow()
    with session_scope() as s:
        row = s.get(JobLock, name, with_for_update=True)
        if row and row.expires_at > now and row.holder != HOLDER:
            return False
        if row:
            row.holder, row.acquired_at, row.expires_at = HOLDER, now, now + timedelta(minutes=ttl_min)
        else:
            s.add(JobLock(name=name, holder=HOLDER, acquired_at=now, expires_at=now + timedelta(minutes=ttl_min)))
    with session_scope() as s:  # 競合時に確実に自分が持っているか再確認
        row = s.get(JobLock, name)
        return row is not None and row.holder == HOLDER


def release_lock(name: str = "collect") -> None:
    with session_scope() as s:
        s.execute(delete(JobLock).where(JobLock.name == name, JobLock.holder == HOLDER))


# ---------------------------------------------------------------------------
# 認証情報
# ---------------------------------------------------------------------------

def load_credentials(s) -> Credentials | None:
    sec = get_secret(s, "talknote_credentials")
    if sec and sec.get("login_id") and sec.get("password"):
        return Credentials(sec["login_id"], sec["password"])
    lid, pw = os.environ.get("TALKNOTE_LOGIN_ID"), os.environ.get("TALKNOTE_PASSWORD")
    if lid and pw:
        return Credentials(lid, pw)
    return None


def _set_auth_state(s, reauth: bool, message: str) -> None:
    put_setting(s, "auth_state", {"reauth_required": reauth, "message": message,
                                  "at": datetime.utcnow().isoformat()})


# ---------------------------------------------------------------------------
# キュー
# ---------------------------------------------------------------------------

def enqueue(kind: str, params: dict | None = None, user: str = "scheduler") -> int:
    with session_scope() as s:
        dup = s.scalar(select(JobRun).where(JobRun.kind == kind, JobRun.status.in_(("queued", "running"))))
        if dup and kind == "daily":
            return dup.id
        run = JobRun(kind=kind, status="queued", params=params or {}, requested_by=user)
        s.add(run)
        s.flush()
        return run.id


def run_next_queued() -> int | None:
    with session_scope() as s:
        run = s.scalar(select(JobRun).where(JobRun.status == "queued").order_by(JobRun.queued_at))
        if run is None:
            return None
        rid = run.id
    execute(rid)
    return rid


def _step(run_id: int, step: str, status: str, clinic: str = "", detail: dict | None = None) -> None:
    with session_scope() as s:
        s.add(JobStep(run_id=run_id, step=step, clinic_code=clinic, status=status, detail=detail or {},
                      finished_at=datetime.utcnow()))


def execute(run_id: int) -> None:
    if not acquire_lock():
        with session_scope() as s:
            run = s.get(JobRun, run_id)
            if run.status == "queued" and (datetime.utcnow() - run.queued_at) > timedelta(hours=6):
                run.status, run.error = "failed", "他の処理が長時間実行中のため中止"
        return
    try:
        with session_scope() as s:
            run = s.get(JobRun, run_id)
            if run.status != "queued":
                return
            run.status, run.started_at = "running", datetime.utcnow()
            kind, params = run.kind, dict(run.params or {})
        try:
            if kind in ("daily", "manual", "retry_failed"):
                summary, status = run_collection(run_id, kind, params)
            elif kind == "reparse":
                summary, status = run_reparse(run_id, params)
            elif kind == "connection_test":
                summary, status = connection_test(run_id)
            elif kind == "backup":
                from .backup import run_backup
                with session_scope() as s:
                    keep = min(int(get_setting(s, "backup").get("keep", 7)), 7)  # DBのみ。容量を圧迫しないよう最大7世代
                summary, status = {"file": str(run_backup(keep))}, "success"
            else:
                summary, status = {"error": f"不明な処理 {kind}"}, "failed"
            err = ""
        except Exception as e:  # noqa: BLE001
            log.exception("job failed")
            summary, status, err = {}, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        with session_scope() as s:
            run = s.get(JobRun, run_id)
            run.status, run.summary, run.error, run.finished_at = status, summary, err[:4000], datetime.utcnow()
            if status in ("failed", "reauth_required", "partial"):
                notify(s, "error" if status != "partial" else "warn",
                       {"failed": "自動収集に失敗しました", "reauth_required": "Talknoteの再認証が必要です",
                        "partial": "自動収集が一部失敗しました"}[status],
                       (err or "") + "\n" + str(summary)[:1500])
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------

def collection_range(s, kind: str, params: dict, today: date) -> tuple[date, date]:
    sched = get_setting(s, "schedule")
    floor = date.fromisoformat(get_setting(s, "collection").get("start_date", "2026-09-01"))
    if kind == "manual" and params.get("start"):
        return max(date.fromisoformat(params["start"]), floor), date.fromisoformat(params.get("end") or (today - timedelta(days=1)).isoformat())
    end = today - timedelta(days=1)
    start = today - timedelta(days=int(sched.get("recheck_days", 31)))
    last = s.scalar(select(JobRun).where(JobRun.kind.in_(("daily", "manual")), JobRun.status.in_(("success", "partial")))
                    .order_by(JobRun.finished_at.desc()))
    covered = (last.summary or {}).get("range_end") if last else None
    if covered:
        gap_start = date.fromisoformat(covered) + timedelta(days=1)
        start = min(start, gap_start)  # 停止期間の補完
    else:
        start = min(start, floor) if (today - floor).days <= 400 else start
    return max(start, floor), end


def run_collection(run_id: int, kind: str, params: dict) -> tuple[dict, str]:
    today = aggregate.today_jst()
    with session_scope() as s:
        start, end = collection_range(s, kind, params, today)
        creds = load_credentials(s)
        state = get_secret(s, "talknote_session")
        clinics = [(c.code, c.group_id) for c in s.scalars(select(Clinic).where(Clinic.enabled.is_(True)).order_by(Clinic.sort_order))]
        if params.get("clinics"):
            clinics = [c for c in clinics if c[0] in params["clinics"]]
        last_ok = s.scalar(select(JobRun.started_at).where(JobRun.status == "success").order_by(JobRun.started_at.desc()))
    summary = {"range_start": start.isoformat(), "range_end": end.isoformat(), "clinics": {}}
    since = start - timedelta(days=1)
    updated_since = (last_ok + timedelta(hours=9) - timedelta(days=1)) if last_ok else None  # UTC→JST、1日の余裕
    changed: set[int] = set()
    failed_clinics: set[str] = set()

    # 1-3. 接続・取得・添付
    try:
        with TalknoteClient(creds, state) as client:
            msg = client.ensure_login(clinics[0][1])
            _step(run_id, "接続確認", "success", detail={"message": msg})
            with session_scope() as s:
                _set_auth_state(s, False, msg)
                if client.state_changed:
                    put_secret(s, "talknote_session", client.storage_state)
            for code, gid in clinics:
                for attempt in (1, 2):
                    try:
                        with session_scope() as s:
                            st = collect_clinic(s, client, code, gid, since, updated_since,
                                                get_setting(s, "retention").get("raw_days", 730))
                            if kind == "retry_failed":
                                _reset_failed_files(s, code)
                        changed |= st.changed_post_ids
                        summary["clinics"][code] = st.as_dict()
                        _step(run_id, "投稿の取得", "success" if not st.errors else "partial", code, st.as_dict())
                        if st.files_failed:
                            failed_clinics.add(code)
                        break
                    except SessionExpired:
                        if attempt == 2:
                            raise
                        client.storage_state = None
                        client.login()
                        with session_scope() as s:
                            put_secret(s, "talknote_session", client.storage_state)
                    except TalknoteError as e:
                        failed_clinics.add(code)
                        summary["clinics"][code] = {"error": str(e)}
                        _step(run_id, "投稿の取得", "failed", code, {"error": str(e)})
                        with session_scope() as s:
                            upsert_issue(s, f"fetch:{code}:{today}", clinic=code, kind="fetch_failed", severity="error",
                                         title=f"{code} の投稿取得に失敗しました：{e}", target_date=None)
                        break
    except ReauthRequired as e:
        with session_scope() as s:
            _set_auth_state(s, True, str(e))
        _step(run_id, "接続確認", "failed", detail={"error": str(e)})
        _finish_without_fetch(run_id, clinics, start, end, today, {c for c, _ in clinics})
        return {**summary, "error": str(e)}, "reauth_required"
    except LoginFailed as e:
        with session_scope() as s:
            _set_auth_state(s, True, str(e))
        _step(run_id, "接続確認", "failed", detail={"error": str(e)})
        _finish_without_fetch(run_id, clinics, start, end, today, {c for c, _ in clinics})
        return {**summary, "error": str(e)}, "reauth_required"

    # 4-6. 解析・照合・保存
    affected = _process(run_id, changed, kind == "retry_failed")
    days_done = _reconcile_range(run_id, clinics, start, end, today, affected, failed_clinics)
    summary.update(days_reconciled=days_done, posts_processed=len(changed))
    # 7-8
    _after(run_id, start, end, affected)
    status = "partial" if failed_clinics else "success"
    return summary, status


def _reset_failed_files(s, clinic: str) -> None:
    for f in s.scalars(select(SourceFile).join(SourcePost, SourceFile.post_pk == SourcePost.id).where(
            SourcePost.clinic_code == clinic, SourceFile.status == "failed_fetch")):
        f.attempts = 0


def _process(run_id: int, post_ids: set[int], include_failed: bool) -> set[tuple[str, date]]:
    """投稿ごとに保存（コミット）する。途中で止まっても処理済みの分は残り、次回は続きから再処理される。"""
    affected: set[tuple[str, date]] = set()
    with session_scope() as s:
        ids = set(post_ids)
        if include_failed:
            ids |= set(s.scalars(select(SourcePost.post_id).join(SourceFile, SourceFile.post_pk == SourcePost.id)
                                 .where(SourceFile.status.in_(("failed", "stored")))))
        # 前回途中で止まった投稿（添付が未解析のまま）も拾う
        ids |= set(s.scalars(select(SourcePost.post_id).join(SourceFile, SourceFile.post_pk == SourcePost.id)
                             .where(SourceFile.status == "stored")))
    ok, ng = 0, 0
    for pid in sorted(ids):
        try:
            with session_scope() as s:
                post = s.scalar(select(SourcePost).where(SourcePost.post_id == pid))
                if post is None:
                    continue
                affected |= process_post(s, post)
            ok += 1
        except Exception as e:  # noqa: BLE001
            ng += 1
            log.exception("process_post failed")
            with session_scope() as s:
                clinic = s.scalar(select(SourcePost.clinic_code).where(SourcePost.post_id == pid)) or ""
                upsert_issue(s, f"post:{pid}:process", clinic=clinic, kind="excel_format", severity="error",
                             post_id=pid, title=f"投稿の解析に失敗しました：{type(e).__name__}: {str(e)[:200]}")
    _step(run_id, "解析・照合", "success" if not ng else "partial", detail={"ok": ok, "failed": ng})
    return affected


def _reconcile_range(run_id, clinics, start, end, today, affected, failed_clinics) -> int:
    n = 0
    with session_scope() as s:
        days = {(c, d) for c, d in affected}
        d = start
        while d <= end:
            for c, _ in clinics:
                days.add((c, d))
            d += timedelta(days=1)
        for c, d in sorted(days):
            reconcile_day(s, c, d)
            n += 1
        for c, _ in clinics:
            ds = [d for cc, d in days if cc == c]
            if ds:
                failed = set(_dates(start, end)) if c in failed_clinics else set()
                refresh_days(s, c, min(ds), max(max(ds), end), today, failed)
    _step(run_id, "日次データ保存", "success", detail={"days": n})
    return n


def _finish_without_fetch(run_id, clinics, start, end, today, failed):
    with session_scope() as s:
        for c, _ in clinics:
            refresh_days(s, c, start, end, today, set(_dates(start, end)) if c in failed else set())


def _after(run_id: int, start: date, end: date, affected: set[tuple[str, date]]) -> None:
    from .pipeline import check_against_latest
    with session_scope() as s:
        floor = date.fromisoformat(get_setting(s, "collection").get("start_date", "2026-09-01"))
        today = aggregate.today_jst()
        found = {}
        for c in [c.code for c in s.scalars(select(Clinic).where(Clinic.enabled.is_(True)))]:
            found[c] = check_against_latest(s, c, floor, today - timedelta(days=1))
            refresh_days(s, c, floor, today - timedelta(days=1), today)
    _step(run_id, "最新Excelとの照合", "success", detail=found)
    months = {(d.year, d.month) for _, d in affected} | {(d.year, d.month) for d in _dates(start, end)}
    with session_scope() as s:
        n = aggregate.recompute(s, months)
    _step(run_id, "月次・年次の再集計", "success", detail={"months": sorted(f"{y}-{m:02d}" for y, m in months), "rows": n})
    try:
        with session_scope() as s:
            note = generate_daily(s)
        _step(run_id, "分析コメント", "success", detail={"generator": note.generator})
    except Exception as e:  # noqa: BLE001
        _step(run_id, "分析コメント", "failed", detail={"error": str(e)[:300]})


def _dates(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def run_reparse(run_id: int, params: dict) -> tuple[dict, str]:
    """保存済みの原文・原本から再解析（ルール変更後など）。Talknoteには接続しない。"""
    from .pipeline import reprocess

    start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
    today = aggregate.today_jst()
    with session_scope() as s:
        clinics = params.get("clinics") or [c.code for c in s.scalars(select(Clinic).where(Clinic.enabled.is_(True)))]
        days = reprocess(s, clinics, start, end)
        for c, d in sorted(days):
            reconcile_day(s, c, d)
        for c in clinics:
            refresh_days(s, c, start, end, today)
    _after(run_id, start, end, days)
    return {"range_start": start.isoformat(), "range_end": end.isoformat(), "days": len(days)}, "success"


def connection_test(run_id: int) -> tuple[dict, str]:
    with session_scope() as s:
        creds = load_credentials(s)
        state = get_secret(s, "talknote_session")
        clinics = [(c.code, c.name, c.group_id) for c in s.scalars(select(Clinic).order_by(Clinic.sort_order))]
    res = {"login": None, "clinics": {}}
    try:
        with TalknoteClient(creds, state) as client:
            res["login"] = client.ensure_login(clinics[0][2])
            with session_scope() as s:
                if client.state_changed:
                    put_secret(s, "talknote_session", client.storage_state)
                _set_auth_state(s, False, res["login"])
            for code, name, gid in clinics:
                try:
                    msgs = client.list_posts(gid, 0, 1)
                    res["clinics"][code] = {"ok": True, "name": name,
                                            "latest": msgs[0]["regist_date"] if msgs else None,
                                            "has_attachment": bool(msgs and msgs[0].get("attach_file"))}
                except TalknoteError as e:
                    res["clinics"][code] = {"ok": False, "name": name, "error": str(e)}
    except (LoginFailed, ReauthRequired) as e:
        res["login"] = f"失敗: {e}"
        with session_scope() as s:
            _set_auth_state(s, True, str(e))
        return res, "reauth_required"
    ok = all(v.get("ok") for v in res["clinics"].values())
    return res, "success" if ok else "partial"
