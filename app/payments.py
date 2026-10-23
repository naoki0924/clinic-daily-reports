"""入金の支払い方法別の内訳。

実データ（2026年）で確認した各院Excelの構造：
- 入金合計 ＝ 現金 ＋ クレジット・アプリ決済 計 ＋ 振込 ＋ デポジット（約9割の日で一致）
- WeChatPay・Alipay・銀聯は「クレジット・アプリ決済 計」の内数。足し上げると二重になるため内訳として別に示す
- 9ru は 振込・デポジット が本文の【入金内訳】にだけある日があるので、その日は本文の値を使う
- 合計と内訳が合わない日は、差額を「内訳の記載なし（入金合計との差）」として示す（推測で配分しない）
- 要確認の日は使わない（他の画面と同じ）
- 日報は日ごとの合計なので、1件ごとの入金（取引）は分からない
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select

from .models import DailyFact

MAIN = ["現金", "クレジット・アプリ決済", "振込", "デポジット"]
OTHER = "内訳の記載なし"
APPS = ["WeChatPay", "Alipay", "銀聯"]
CARD = "クレジットカード等"
COLORS = {"現金": "#1f5a6b", "クレジット・アプリ決済": "#3aa0a6", "振込": "#93cfb3", "デポジット": "#2a2f33",
          OTHER: "#c9d3d3", "WeChatPay": "#4f7f8c", "Alipay": "#6fb7bd", "銀聯": "#a9c9cc", CARD: "#3aa0a6"}
_METRICS = ("receipt", "payment", "transfer", "deposit")
_PAY_KEYS = {"現金", "クレジット", "振込", "デポジット", *APPS}


def _day(m: dict) -> dict:
    """1院1日の値 {key: value} から内訳を作る。key は metric または payment:方法。"""
    g = lambda k: m.get(k) or 0  # noqa: E731
    transfer = m["transfer"] if m.get("transfer") is not None else m.get("payment:振込")
    deposit = m["deposit"] if m.get("deposit") is not None else m.get("payment:デポジット")
    main = {"現金": g("payment:現金"), "クレジット・アプリ決済": g("payment:クレジット"),
            "振込": transfer or 0, "デポジット": deposit or 0}
    receipt = m.get("receipt")
    parts = sum(main.values())
    other = (receipt - parts) if receipt is not None else 0
    apps = {a: g(f"payment:{a}") for a in APPS}
    return {"receipt": receipt if receipt is not None else parts, "main": main, "other": other, "apps": apps}


def collect(s, codes: list[str], start: date, end: date) -> dict[str, dict[date, dict]]:
    """{院: {日: 内訳}}。院全体の値（市場の区分なし）だけを使う。"""
    raw: dict[tuple, dict] = defaultdict(dict)
    review: dict[str, set] = defaultdict(set)
    for f in s.scalars(select(DailyFact).where(
            DailyFact.clinic_code.in_(codes), DailyFact.metric.in_(_METRICS), DailyFact.market == "",
            DailyFact.target_date >= start, DailyFact.target_date <= end)):
        if f.status == "review":
            # 入金合計か内訳のどれかが要確認の日は、その日ごと除く（一部だけ抜くと差額が大きく見えるため）
            if f.metric != "payment" or f.method in _PAY_KEYS:
                review[f.clinic_code].add(f.target_date)
            continue
        key = f.metric + (f":{f.method}" if f.method else "")
        raw[(f.clinic_code, f.target_date)][key] = f.value
    out: dict[str, dict[date, dict]] = {c: {} for c in codes}
    for (c, d), m in raw.items():
        if d in review[c] or not any(v for v in m.values()):
            continue
        out[c][d] = _day(m)
    out["_review"] = {c: len(v) for c, v in review.items()}
    out["_review_days"] = {c: set(v) for c, v in review.items()}
    return out


def total(days: dict[date, dict]) -> dict:
    t = {"receipt": 0.0, "main": {k: 0.0 for k in MAIN}, "other": 0.0, "apps": {a: 0.0 for a in APPS}, "days": len(days)}
    for b in days.values():
        t["receipt"] += b["receipt"]
        t["other"] += b["other"]
        for k in MAIN:
            t["main"][k] += b["main"][k]
        for a in APPS:
            t["apps"][a] += b["apps"][a]
    t["card"] = t["main"]["クレジット・アプリ決済"] - sum(t["apps"].values())
    return t


def summarize(data: dict, codes: list[str]) -> dict[str, dict]:
    """院ごとと全院（ALL）の合計。"""
    res = {c: total(data.get(c, {})) for c in codes}
    allm: dict = {}
    for c in codes:
        for d, b in data.get(c, {}).items():
            allm[(c, d)] = b
    res["ALL"] = total(allm)
    return res


def monthly(data: dict, codes: list[str]) -> dict[str, dict]:
    """{"YYYY-MM": 合計}（指定院の合計）。"""
    by: dict[str, dict] = defaultdict(dict)
    for c in codes:
        for d, b in data.get(c, {}).items():
            by[d.strftime("%Y-%m")][(c, d)] = b
    return {k: total(v) for k, v in sorted(by.items())}


def daily_rows(data: dict, codes: list[str]) -> list[tuple[date, dict]]:
    by: dict[date, dict] = defaultdict(dict)
    for c in codes:
        for d, b in data.get(c, {}).items():
            by[d][(c, d)] = b
    return [(d, total(v)) for d, v in sorted(by.items())]


def bio_segments(s, start: date, end: date, skip_days: set | None = None) -> dict[str, dict[str, float]]:
    """BiO本文の区分（一般・インバウンド・AIMS/ITEC・BTS・再生美容）×決済方法。本文の値なので参考。
    上の表と同じ日だけを使う（入金が要確認の日は除く）。"""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for f in s.scalars(select(DailyFact).where(
            DailyFact.clinic_code == "bio", DailyFact.metric == "payment", DailyFact.market != "",
            DailyFact.target_date >= start, DailyFact.target_date <= end,
            DailyFact.value.isnot(None), DailyFact.status != "review")):
        if skip_days and f.target_date in skip_days:
            continue
        out[f.market][f.method] += f.value
    return {k: {m: v for m, v in d.items() if v} for k, d in out.items() if any(d.values())}
