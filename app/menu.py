"""メニュー（施術カテゴリ）分析。

元データは各院Excelの「カテゴリ別 売上・件数」（採用値）。BiOはCPC受託メニュー、大阪は本文のカテゴリ別来院・契約件数も使う。
- 院ごとにカテゴリの区分が違うので、院をまたいでカテゴリを合算しない
- 件数は施術・会計の件数で、来院者の実人数ではない
- 平均単価＝売上÷件数（同じ期間の合計どうし）。件数0は算出不可
- 要確認の日の値は使わない
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select

from . import aggregate as ag
from .models import DailyFact, Observation
from .normalize import nfkc

_PRICE_NOTE = re.compile(r"[\(（][^)）]*[¥￥\d][^)）]*[\)）].*$")


def menu_name(category: str) -> str:
    """見出しに書き込まれた単価や注記を除いて名前をそろえる（例：『NK細胞(¥171,600/件)※…』→『NK細胞』）。"""
    s = nfkc(category or "").strip()
    s = _PRICE_NOTE.sub("", s).strip()
    s = re.sub(r"※.*$", "", s).strip()
    return s or nfkc(category or "")


@dataclass
class MenuRow:
    market: str
    name: str
    sales: float = 0.0
    count: float = 0.0
    days: set = field(default_factory=set)
    prev_sales: float | None = None
    prev_count: float | None = None
    monthly: dict = field(default_factory=dict)   # "YYYY-MM" -> sales

    @property
    def unit_price(self) -> float | None:
        return self.sales / self.count if self.count else None

    @property
    def change(self) -> float | None:
        if self.prev_sales in (None, 0):
            return None
        return (self.sales - self.prev_sales) / self.prev_sales


def _facts(s, code: str, metrics: tuple[str, ...], start: date, end: date, excel_only: bool = False):
    q = select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.metric.in_(metrics),
                                DailyFact.target_date >= start, DailyFact.target_date <= end,
                                DailyFact.value.isnot(None), DailyFact.status != "review")
    rows = s.scalars(q).all()
    if excel_only and rows:
        ids = {r.adopted_obs_id for r in rows if r.adopted_obs_id}
        kinds = dict(s.execute(select(Observation.id, Observation.source_kind).where(Observation.id.in_(ids))).all())
        rows = [r for r in rows if kinds.get(r.adopted_obs_id) in ("excel", "excel_later")]
    return rows


def menu_table(s, code: str, start: date, end: date, *, sales_metric="category_sales", count_metric="category_count",
               market: str = "", excel_only: bool = False, prev: tuple[date, date] | None = None) -> list[MenuRow]:
    rows: dict[tuple, MenuRow] = {}

    def key(f):
        return (f.market or "", menu_name(f.category))

    for f in _facts(s, code, (sales_metric, count_metric), start, end, excel_only):
        if market and f.market != market:
            continue
        r = rows.setdefault(key(f), MenuRow(*key(f)))
        if f.metric == sales_metric:
            r.sales += f.value
            r.days.add(f.target_date)
            ym = f.target_date.strftime("%Y-%m")
            r.monthly[ym] = r.monthly.get(ym, 0) + f.value
        else:
            r.count += f.value
    if prev:
        pv: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for f in _facts(s, code, (sales_metric, count_metric), prev[0], prev[1], excel_only):
            if market and f.market != market:
                continue
            pv[key(f)][0 if f.metric == sales_metric else 1] += f.value
        has_prev = bool(pv)
        for k, r in rows.items():
            if has_prev:
                r.prev_sales, r.prev_count = pv.get(k, [0.0, 0.0])
        for k, (ps, pc) in pv.items():
            if k not in rows and ps:
                rows[k] = MenuRow(*k, prev_sales=ps, prev_count=pc)
    out = [r for r in rows.values() if r.sales or r.count or r.prev_sales]
    out.sort(key=lambda r: (-r.sales, -r.count))
    return out


def market_split(rows: list[MenuRow]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        out[r.market or "—"] += r.sales
    return dict(out)


def visits_contracts(s, code: str, start: date, end: date) -> list[dict]:
    """大阪：本文のカテゴリ別 来院件数・契約件数（新規／既存）。率は同じカテゴリの合計どうしで計算。"""
    acc: dict[tuple, dict] = {}
    for f in _facts(s, code, ("category_visits", "category_contracts"), start, end):
        k = (f.market, f.cust, f.category)
        a = acc.setdefault(k, {"market": f.market, "cust": f.cust, "category": f.category, "visits": 0.0, "contracts": 0.0})
        a["visits" if f.metric == "category_visits" else "contracts"] += f.value
    rows = [a for a in acc.values() if a["visits"] or a["contracts"]]
    for a in rows:
        a["rate"] = a["contracts"] / a["visits"] if a["visits"] else None
    rows.sort(key=lambda a: (-a["contracts"], -a["visits"]))
    return rows


def period_range(view: str, y: int, m: int, today: date) -> tuple[date, date, tuple[date, date], str, bool]:
    """表示期間と比較期間。進行中の期間は同じ経過日数で比較する。"""
    if view == "year":
        start, end = date(y, 1, 1), date(y, 12, 31)
        cmp_end, running = ag.elapsed_end(start, end, today)
        prev = (date(y - 1, 1, 1), ag.shift_year(cmp_end, -1))
        label = f"{y}年" + ("（1/1〜" + cmp_end.strftime("%m/%d") + "）" if running else "")
        return start, cmp_end, prev, label, running
    start, end = ag.month_range(y, m)
    cmp_end, running = ag.elapsed_end(start, end, today)
    py, pm = ag.prev_month(y, m)
    ps, pe = ag.month_range(py, pm)
    if running:
        pe = min(pe, ps + (cmp_end - start))
    label = f"{y}年{m}月" + ("（1日〜" + str(cmp_end.day) + "日）" if running else "")
    return start, cmp_end, (ps, pe), label, running


def insights(rows: list[MenuRow], prev_label: str) -> list[str]:
    """プログラムで計算した事実だけを文章にする（推測は書かない）。"""
    total = sum(r.sales for r in rows)
    if not total:
        return ["この期間のカテゴリ別売上の記録がありません。"]
    lines = []
    top = [r for r in rows if r.sales][:3]
    lines.append("売上上位：" + "、".join(f"{(r.market + ' ') if r.market else ''}{r.name}（{r.sales / total * 100:.1f}%）" for r in top))
    lines.append(f"上位3メニューで売上の {sum(r.sales for r in top) / total * 100:.1f}% を占めています。")
    movers = [r for r in rows if r.prev_sales not in (None,) and (r.sales or r.prev_sales)]
    if movers and any(r.prev_sales for r in movers):
        up = max(movers, key=lambda r: r.sales - (r.prev_sales or 0))
        down = min(movers, key=lambda r: r.sales - (r.prev_sales or 0))
        if up.sales - (up.prev_sales or 0) > 0:
            lines.append(f"{prev_label}から最も増えたのは {up.market} {up.name}（{up.sales - (up.prev_sales or 0):+,.0f}円）。")
        if down.sales - (down.prev_sales or 0) < 0:
            lines.append(f"最も減ったのは {down.market} {down.name}（{down.sales - (down.prev_sales or 0):+,.0f}円）。")
    hi = [r for r in rows if r.unit_price][:10]
    if hi:
        best = max(hi, key=lambda r: r.unit_price)
        lines.append(f"上位メニューのうち平均単価が最も高いのは {best.market} {best.name}（{best.unit_price:,.0f}円／件）。")
    return lines


def months_of(y: int) -> list[str]:
    return [f"{y}-{m:02d}" for m in range(1, 13)]


def daily_series(s, code: str, metric: str, start: date, end: date) -> dict[date, float]:
    out = {}
    for f in s.scalars(select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.metric == metric,
                                               DailyFact.target_date >= start, DailyFact.target_date <= end,
                                               DailyFact.market == "", DailyFact.cust == "", DailyFact.category == "",
                                               DailyFact.channel == "", DailyFact.method == "",
                                               DailyFact.value.isnot(None), DailyFact.status != "review")):
        out[f.target_date] = f.value
    return out


def days_between(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# 国内／インバウンド別の構成（売上（手数料抜き）の内訳）
# ---------------------------------------------------------------------------
MARKET_ORDER = ["国内", "インバウンド", "クーポン経由", "植毛"]


def market_share(s, codes: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """院ごとの {区分: 金額}。要確認の日は除く。院をまたいだ合計は "ALL"。"""
    out: dict[str, dict[str, float]] = {c: defaultdict(float) for c in codes}
    for f in s.scalars(select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.metric == "market_sales_net",
                                               DailyFact.target_date >= start, DailyFact.target_date <= end,
                                               DailyFact.value.isnot(None), DailyFact.status != "review")):
        out[f.clinic_code][f.market] += f.value
    allm: dict[str, float] = defaultdict(float)
    for c in codes:
        for k, v in out[c].items():
            allm[k] += v
    out["ALL"] = allm
    return {c: {k: v for k, v in sorted(d.items(), key=lambda kv: (MARKET_ORDER.index(kv[0]) if kv[0] in MARKET_ORDER else 9))
                if v} for c, d in out.items()}


def market_monthly(s, codes: list[str], y: int) -> dict[str, dict[str, float]]:
    """{"YYYY-MM": {区分: 金額}}（指定院の合計）。"""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for f in s.scalars(select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.metric == "market_sales_net",
                                               DailyFact.target_date >= date(y, 1, 1), DailyFact.target_date <= date(y, 12, 31),
                                               DailyFact.value.isnot(None), DailyFact.status != "review")):
        out[f.target_date.strftime("%Y-%m")][f.market] += f.value
    return {k: dict(v) for k, v in out.items()}


def review_days(s, codes: list[str], start: date, end: date) -> dict[str, int]:
    out = defaultdict(set)
    for f in s.scalars(select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.metric == "market_sales_net",
                                               DailyFact.target_date >= start, DailyFact.target_date <= end,
                                               DailyFact.status == "review")):
        out[f.clinic_code].add(f.target_date)
    return {k: len(v) for k, v in out.items()}
