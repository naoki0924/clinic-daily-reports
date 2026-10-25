"""画面用のデータ取得。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select

from .. import aggregate as ag
from ..models import (
    DIM_COLS, Aggregate, AnalysisNote, Clinic, DailyFact, DayStatus, Issue, JobRun, MetricDef, Observation, Report,
    SourceFile, SourcePost, Target,
)

STATUS_LABEL = {"submitted": "提出済", "review": "要確認", "missing": "未提出", "closed": "休診", "preopen": "開院前", "failed": "取得失敗",
                "future": "未到来", None: "—"}
STATE_LABEL = {"dash": "「-」", "blank": "空欄", "absent": "未記載", "conflict": "差異あり", "error": "読取失敗"}
FACT_STATUS = {"confirmed": "確定", "provisional": "暫定", "estimated": "推定", "review": "要確認"}
KEY_METRICS = ["sales_net", "sales_gross", "receipt", "ar_new", "deposit", "visitors_total", "visitors_new",
               "contracts", "inquiries"]


def clinics(s, allowed: list[str] | None = None) -> list[Clinic]:
    rows = s.scalars(select(Clinic).where(Clinic.enabled.is_(True)).order_by(Clinic.sort_order)).all()
    return [c for c in rows if allowed is None or c.code in allowed]


def metric_defs(s) -> dict[str, MetricDef]:
    return {m.code: m for m in s.scalars(select(MetricDef).order_by(MetricDef.sort_order))}


def facts_for(s, codes: list[str], start: date, end: date, metrics: list[str] | None = None,
              dims: dict | None = None) -> dict[tuple, DailyFact]:
    q = select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.target_date >= start, DailyFact.target_date <= end)
    if metrics:
        q = q.where(DailyFact.metric.in_(metrics))
    dims = dims or {}
    for c in DIM_COLS:
        q = q.where(getattr(DailyFact, c) == (dims.get(c) or ""))
    return {(f.clinic_code, f.target_date, f.metric): f for f in s.scalars(q)}


def statuses(s, codes: list[str], start: date, end: date) -> dict[tuple, DayStatus]:
    rows = s.scalars(select(DayStatus).where(DayStatus.clinic_code.in_(codes), DayStatus.target_date >= start,
                                             DayStatus.target_date <= end))
    return {(r.clinic_code, r.target_date): r for r in rows}


def dashboard(s, codes: list[str], today: date) -> dict:
    y = today - timedelta(days=1)
    f = facts_for(s, codes, y - timedelta(days=7), y, KEY_METRICS)
    st = statuses(s, codes, y - timedelta(days=13), y)
    ms, _ = ag.month_range(y.year, y.month)
    cards = []
    for c in clinics(s, codes):
        m = {}
        for k in ("sales_net", "receipt", "visitors_new", "contracts", "inquiries"):
            cur, prev, wk = f.get((c.code, y, k)), f.get((c.code, y - timedelta(days=1), k)), f.get((c.code, y - timedelta(days=7), k))
            m[k] = {"fact": cur, "prev": prev.value if prev else None, "wk": wk.value if wk else None}
        mtd = ag.total(s, [c.code], "sales_net", ms, y, today=today)
        tgt = ag.target_value(s, c.code, y.year, y.month, "sales_net")
        cards.append({"clinic": c, "status": st.get((c.code, y)), "m": m, "mtd": mtd, "target": tgt})
    all_mtd = ag.total(s, codes, "sales_net", ms, y, today=today)
    ly = ag.total(s, codes, "sales_net", ag.shift_year(ms, -1), ag.shift_year(y, -1), today=today)
    pm_y, pm_m = ag.prev_month(y.year, y.month)
    pm_s = date(pm_y, pm_m, 1)
    pm_e = min(ag.month_range(pm_y, pm_m)[1], pm_s + (y - ms))
    pm = ag.total(s, codes, "sales_net", pm_s, pm_e, today=today)
    grid_days = [y - timedelta(days=i) for i in range(13, -1, -1)]
    issue_counts = dict(s.execute(select(Issue.kind, func.count()).where(Issue.status == "open", Issue.clinic_code.in_(codes),
                                                                     Issue.severity != "info")
                                  .group_by(Issue.kind)).all())
    note = s.scalar(select(AnalysisNote).where(AnalysisNote.scope == "daily").order_by(AnalysisNote.period.desc()))
    last_run = s.scalar(select(JobRun).where(JobRun.kind.in_(("daily", "manual"))).order_by(JobRun.queued_at.desc()))
    from .. import menu as M
    share = M.market_share(s, codes, ms, y)
    trend_start = y - timedelta(days=29)
    tf = facts_for(s, codes, trend_start, y, ["sales_net"])
    trend = []
    for i in range(30):
        dd = trend_start + timedelta(days=i)
        vals = [f.value for (c, d_, _), f in tf.items() if d_ == dd and f.value is not None and f.status != "review"]
        trend.append({"d": dd.strftime("%m/%d"), "v": sum(vals) if vals else None})
    return dict(share=share, trend=trend, day=y, cards=cards, all_mtd=all_mtd, last_year=ly, prev_month=pm, prev_month_range=(pm_s, pm_e),
                prev_month_cov=ag.coverage(s, codes, pm_s, pm_e),
                all_target=ag.target_value(s, "ALL", y.year, y.month, "sales_net"), grid_days=grid_days, grid=st,
                issue_counts=issue_counts, note=note, last_run=last_run, month_start=ms)


def month_table(s, codes: list[str], y: int, m: int, metric: str, dims: dict | None = None) -> dict:
    start, end = ag.month_range(y, m)
    f = facts_for(s, codes, start, end, [metric], dims)
    st = statuses(s, codes, start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return dict(days=days, facts=f, statuses=st, start=start, end=end)


def paired_rate(s, code: str, num: str, den: str, start: date, end: date) -> dict:
    """分子・分母の両方がある日だけで、合計どうしを割る。"""
    rows = s.scalars(select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.metric.in_((num, den)),
                                             DailyFact.target_date >= start, DailyFact.target_date <= end,
                                             DailyFact.market == "", DailyFact.cust == "", DailyFact.category == "",
                                             DailyFact.channel == "", DailyFact.method == ""))
    by = defaultdict(dict)
    for r in rows:
        if r.value is not None and r.status != "review":
            by[r.target_date][r.metric] = r.value
    n = sum(v[num] for v in by.values() if num in v and den in v)
    d = sum(v[den] for v in by.values() if num in v and den in v)
    days = sum(1 for v in by.values() if num in v and den in v)
    return {"value": (n / d) if d else None, "num": n, "den": d, "days": days}


def compare(cur: float | None, base: float | None) -> str:
    if cur is None or base is None:
        return "比較不可"
    if base == 0:
        return "算出不可（基準0）"
    return f"{(cur - base) / base * 100:+.1f}%"


def evidence(s, code: str, d: date, metric: str, dims: dict) -> dict:
    q = select(Observation).where(Observation.clinic_code == code, Observation.target_date == d, Observation.metric == metric)
    for c in DIM_COLS:
        q = q.where(getattr(Observation, c) == (dims.get(c) or ""))
    obs = s.scalars(q.order_by(Observation.source_kind)).all()
    fq = select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.target_date == d, DailyFact.metric == metric)
    for c in DIM_COLS:
        fq = fq.where(getattr(DailyFact, c) == (dims.get(c) or ""))
    fact = s.scalar(fq)
    posts = {p.post_id: p for p in s.scalars(select(SourcePost).where(SourcePost.post_id.in_({o.post_id for o in obs if o.post_id})))}
    files = {f.file_id: f for f in s.scalars(select(SourceFile).where(SourceFile.file_id.in_({o.file_id for o in obs if o.file_id})))}
    return dict(obs=obs, fact=fact, posts=posts, files=files)


def agg_rows(s, ptype: str, periods: list[str], codes: list[str], metric: str, dims_key: str = "") -> dict:
    rows = s.scalars(select(Aggregate).where(Aggregate.period_type == ptype, Aggregate.period.in_(periods),
                                             Aggregate.clinic_code.in_(codes + ["ALL"]), Aggregate.metric == metric,
                                             Aggregate.dims_key == dims_key))
    return {(r.clinic_code, r.period): r for r in rows}


def category_breakdown(s, codes: list[str], start: date, end: date, metric: str = "category_sales") -> dict:
    rows = s.scalars(select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.metric == metric,
                                             DailyFact.target_date >= start, DailyFact.target_date <= end))
    out = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.value is not None and r.status != "review":
            out[r.clinic_code][f"{r.market} {r.category}".strip()] += r.value
    return {k: dict(sorted(v.items(), key=lambda kv: -kv[1])) for k, v in out.items()}


def dims_options(s, metric: str) -> dict[str, list[str]]:
    out = {}
    for c in DIM_COLS:
        vals = [v for v in s.scalars(select(func.distinct(getattr(DailyFact, c))).where(DailyFact.metric == metric)) if v]
        if vals:
            out[c] = sorted(vals)
    return out


def targets(s) -> list[Target]:
    return s.scalars(select(Target).order_by(Target.year.desc(), Target.month.desc(), Target.clinic_code)).all()


def reports_list(s, codes: list[str], start: date, end: date):
    return s.execute(select(Report, SourcePost).join(SourcePost, SourcePost.post_id == Report.post_id)
                     .where(Report.clinic_code.in_(codes), Report.target_date >= start, Report.target_date <= end)
                     .order_by(Report.target_date.desc(), Report.clinic_code)).all()


def other_posts(s, codes: list[str], start: date, end: date):
    from datetime import datetime
    return s.scalars(select(SourcePost).where(SourcePost.clinic_code.in_(codes), SourcePost.category != "daily",
                                              SourcePost.posted_at >= datetime.combine(start, datetime.min.time()),
                                              SourcePost.posted_at <= datetime.combine(end, datetime.max.time()))
                     .order_by(SourcePost.posted_at.desc())).all()
