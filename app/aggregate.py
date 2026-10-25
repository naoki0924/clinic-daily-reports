"""日次（採用値）→ 月次・年次の集計と、期間比較の計算。

- 集計は daily_fact だけを使う（観測値を直接足さない）ので二重計上しない。
- 残高（kind=balance）は日別に合算しない。
- 全院合計は定義が統一できた指標（cross_clinic_sum）だけ。
- 欠損・要確認・暫定値・推定値を含む集計は provisional=True（画面で「暫定」と表示）。
- 推定値（estimated）：本文とExcelの差異などで確定できない日を、仮の値で集計に含めたもの。確認キューには残る。
- 率は日別の率の平均ではなく、分子・分母それぞれの合計から計算する。分母0は算出不可。
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

from .config import JST
from .models import Aggregate, Clinic, DailyFact, DayStatus, MetricDef, Target
from .pipeline import DIM_COLS


def today_jst() -> date:
    return datetime.now(JST).date()


def dims_key(row) -> str:
    return "|".join(f"{c}={getattr(row, c)}" for c in DIM_COLS if getattr(row, c))


@dataclass
class Total:
    value: float | None = None
    days_with_value: int = 0
    days_review: int = 0
    days_provisional: int = 0
    days_estimated: int = 0
    days_missing: int = 0
    days_failed: int = 0
    days_closed: int = 0
    days_expected: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def provisional(self) -> bool:
        return bool(self.days_review or self.days_provisional or self.days_estimated or self.days_missing or self.days_failed)

    @property
    def coverage_note(self) -> str:
        parts = []
        if self.days_missing:
            parts.append(f"未提出{self.days_missing}日")
        if self.days_failed:
            parts.append(f"取得失敗{self.days_failed}日")
        if self.days_review:
            parts.append(f"要確認{self.days_review}日")
        if self.days_estimated:
            parts.append(f"推定値{self.days_estimated}日")
        if self.days_provisional:
            parts.append(f"暫定値{self.days_provisional}日")
        return "・".join(parts)


def metric_defs(s) -> dict[str, MetricDef]:
    return {m.code: m for m in s.scalars(select(MetricDef))}


def clinic_codes(s, only: list[str] | None = None) -> list[str]:
    q = select(Clinic.code).where(Clinic.enabled.is_(True)).order_by(Clinic.sort_order)
    codes = list(s.scalars(q))
    return [c for c in codes if not only or c in only]


def total(s, clinics: list[str], metric: str, start: date, end: date, dims: dict | None = None,
          dims_any: bool = False, today: date | None = None) -> Total:
    """期間合計。dims=None なら区分なしの値、dims_any=True なら区分つきを全部合計。"""
    today = today or today_jst()
    q = select(DailyFact).where(DailyFact.clinic_code.in_(clinics), DailyFact.metric == metric,
                                DailyFact.target_date >= start, DailyFact.target_date <= end)
    if not dims_any:
        dims = dims or {}
        for c in DIM_COLS:
            q = q.where(getattr(DailyFact, c) == (dims.get(c) or ""))
    t = Total()
    vals, days = 0.0, set()
    review_days, prov_days, est_days = set(), set(), set()
    for f in s.scalars(q):
        if f.status == "review":
            review_days.add((f.clinic_code, f.target_date))
        elif f.status == "estimated":
            est_days.add((f.clinic_code, f.target_date))
        elif f.status == "provisional":
            prov_days.add((f.clinic_code, f.target_date))
        if f.value is not None and f.status != "review":
            vals += f.value
            days.add((f.clinic_code, f.target_date))
    t.value = vals if days else None
    t.days_with_value = len(days)
    t.days_review = len(review_days)
    t.days_estimated = len(est_days - review_days)
    t.days_provisional = len(prov_days - review_days - est_days)
    st_rows = s.execute(select(DayStatus.status, func.count()).where(
        DayStatus.clinic_code.in_(clinics), DayStatus.target_date >= start,
        DayStatus.target_date <= min(end, today - timedelta(days=1))).group_by(DayStatus.status)).all()
    st = dict(st_rows)
    t.days_missing, t.days_failed, t.days_closed = st.get("missing", 0), st.get("failed", 0), st.get("closed", 0)
    t.days_expected = sum(v for k, v in st.items() if k not in ("closed", "preopen"))
    return t


def rate(num: Total, den: Total) -> float | None:
    if num.value is None or den.value is None or den.value == 0:
        return None
    return num.value / den.value


def month_range(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2/29
        return d.replace(year=d.year + years, day=28)


def prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def elapsed_end(start: date, end: date, today: date) -> tuple[date, bool]:
    """進行中の期間なら昨日までを返す（True=進行中）。"""
    last = today - timedelta(days=1)
    if start <= last < end:
        return last, True
    return end, False


def coverage(s, clinics: list[str], start: date, end: date) -> dict:
    """比較期間のデータ量（データ不足の表示に使う）。"""
    n = s.scalar(select(func.count(func.distinct(DailyFact.target_date))).where(
        DailyFact.clinic_code.in_(clinics), DailyFact.target_date >= start, DailyFact.target_date <= end)) or 0
    days = (end - start).days + 1
    return {"days_with_data": n, "days": days, "insufficient": n < max(1, days * 0.5)}


# ---------------------------------------------------------------------------
# 月次・年次の集計テーブル（日次の更新に連動して再計算）
# ---------------------------------------------------------------------------

def recompute(s, months: set[tuple[int, int]]) -> int:
    defs = metric_defs(s)
    clinics = clinic_codes(s)
    today = today_jst()
    n = 0
    years = {y for y, _ in months}
    for (y, m) in sorted(months):
        start, end = month_range(y, m)
        n += _recompute_period(s, "M", f"{y:04d}-{m:02d}", start, end, clinics, defs, today)
    for y in sorted(years):
        n += _recompute_period(s, "Y", f"{y:04d}", date(y, 1, 1), date(y, 12, 31), clinics, defs, today)
    return n


def _recompute_period(s, ptype: str, period: str, start: date, end: date, clinics: list[str],
                      defs: dict[str, MetricDef], today: date) -> int:
    s.execute(delete(Aggregate).where(Aggregate.period_type == ptype, Aggregate.period == period))
    facts = s.scalars(select(DailyFact).where(DailyFact.target_date >= start, DailyFact.target_date <= end)).all()
    acc: dict[tuple, dict] = defaultdict(lambda: {"v": 0.0, "days": set(), "review": set(), "prov": set()})
    for f in facts:
        md = defs.get(f.metric)
        if md is None or md.kind == "balance":
            continue
        k = (f.clinic_code, f.metric, dims_key(f))
        a = acc[k]
        if f.status == "review":
            a["review"].add(f.target_date)
        elif f.status in ("provisional", "estimated"):
            a["prov"].add(f.target_date)
        if f.value is not None and f.status != "review":
            a["v"] += f.value
            a["days"].add(f.target_date)
    st_last = min(end, today - timedelta(days=1))
    status_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for c, st, cnt in s.execute(select(DayStatus.clinic_code, DayStatus.status, func.count()).where(
            DayStatus.target_date >= start, DayStatus.target_date <= st_last).group_by(DayStatus.clinic_code, DayStatus.status)):
        status_counts[c][st] = cnt
    n = 0
    all_acc: dict[tuple, dict] = defaultdict(lambda: {"v": 0.0, "clinics": set(), "prov": False, "notes": []})
    for (clinic, metric, dk), a in acc.items():
        sc = status_counts.get(clinic, {})
        missing = sc.get("missing", 0) + sc.get("failed", 0)
        prov = bool(a["review"] or a["prov"] or missing)
        value = a["v"] if a["days"] else None
        s.add(Aggregate(period_type=ptype, period=period, clinic_code=clinic, metric=metric, dims_key=dk,
                        value=value, days_with_value=len(a["days"]), days_missing=missing,
                        days_review=len(a["review"]), provisional=prov))
        n += 1
        if dk == "" and defs[metric].cross_clinic_sum and value is not None:
            t = all_acc[metric]
            t["v"] += value
            t["clinics"].add(clinic)
            t["prov"] = t["prov"] or prov
    open_from = {c.code: c.open_from for c in s.scalars(select(Clinic))}
    for metric, t in all_acc.items():
        # 開院前の院は「値のない院」に数えない
        lacking = [c for c in clinics if c not in t["clinics"] and not (open_from.get(c) and open_from[c] > end)]
        note = ("値のない院: " + ", ".join(lacking)) if lacking else ""
        s.add(Aggregate(period_type=ptype, period=period, clinic_code="ALL", metric=metric, dims_key="",
                        value=t["v"], days_with_value=len(t["clinics"]), provisional=t["prov"] or bool(lacking),
                        note=note))
        n += 1
    return n


def target_value(s, clinic: str, y: int, m: int, metric: str) -> float | None:
    row = s.scalar(select(Target).where(Target.clinic_code == clinic, Target.year == y, Target.month == m,
                                        Target.metric == metric))
    return row.value if row else None
