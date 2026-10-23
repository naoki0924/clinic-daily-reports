"""客単価と、お客様（来院）ごとの支払い。

- 客単価 ＝ 売上（手数料抜き）÷ 来院者数（同じ日どうしの合計で計算。要確認の日は除く）
  来院者数：9ru・PREVENTION は本文の「来客者／来院者合計人数」、BiO は本文【概要】の件数（来院件数）。
  大阪は本文に来院者数がないため算出しない。
- お客様ごとの支払い：9ru・PREVENTION の日報本文にある来院ごとの記載（施術・金額・支払い方法）を、
  表示のたびに読み取る（保存しない）。氏名・敬称つきの語は伏せる。
  日報に患者IDはないため、同じ人の別日の来院はつながらない（1行＝1回の来院）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from .models import DailyFact, Report, SourcePost
from .normalize import nfkc
from .parsing.body import _method
from .security import decrypt_text

VISIT_CLINICS = ("9ru", "prevention")
VISITOR_METRIC = {"9ru": "visitors_total", "prevention": "visitors_total", "bio": "treatment_count"}

_AMOUNT = re.compile(r"[¥￥\\]\s*(-?[\d,]+)|(-?[\d,]+)\s*円")
_METHOD = re.compile(r"[（(]\s*([^()（）]+?)\s*[)）]")
_NAME = re.compile(r"[^\s、,，。・()（）/／:：]{1,12}(?:様|さま|さん|ちゃん|くん|君|氏|先生|Dr\.?)")
# 支払い方法として表示してよい語（これ以外の括弧書きは、人名・メモの可能性があるので表示しない）
KNOWN_METHODS = {"現金", "クレジット", "振込", "デポジット", "売掛", "相殺", "WeChatPay", "Alipay", "銀聯", "ポイント",
                 "トリチケ", "オンライン決済", "クレジットカード"}


def mask_names(text: str) -> str:
    """氏名らしい語（〜様・〜さん・〜氏）を伏せる。区分名の「お客様」は残す。"""
    t = text.replace("お客様", "\0")
    t = _NAME.sub("（氏名）", t)
    return t.replace("\0", "お客様")


@dataclass
class Visit:
    clinic: str
    day: date
    kind: str            # 来院 / 物販
    segment: str         # お客様の区分（9ru）／インバウンド等（PREVENTION）
    items: list[str] = field(default_factory=list)
    amount: float | None = None
    method: str = ""
    method_raw: str = ""

    @property
    def text(self) -> str:
        return "、".join(self.items)


def _amount(line: str):
    m = None
    for m in _AMOUNT.finditer(line):
        pass
    if not m:
        return None, line
    v = (m.group(1) or m.group(2)).replace(",", "")
    try:
        val = float(v)
    except ValueError:
        return None, line
    return val, line[:m.start()].strip()


def _method_of(line: str) -> tuple[str, str]:
    ms = _METHOD.findall(line)
    if not ms:
        return "", ""
    raw = ms[-1].strip()
    head = re.split(r"[/／・]", raw)[0].strip()
    head = re.sub(r"^後日", "", head)
    m = _method(head)
    m = "クレジット" if m == "クレジットカード" else m
    return (m, raw) if m in KNOWN_METHODS else ("", "")


def _clean_item(s: str) -> str:
    s = re.sub(r"^[・\s]+", "", s)
    s = _METHOD.sub("", s)
    s = re.sub(r"※.*$", "", s)
    return mask_names(s.strip(" 　\t:："))


def _segment(seg: str) -> str:
    """「既存インバウンドのお客様」と「インバウンド既存のお客様」のような語順の違いをそろえる。"""
    m = re.match(r"^(新規|既存)(.+?)のお客様$", seg)
    return f"{m.group(2)}{m.group(1)}のお客様" if m else seg


def parse_9ru(text: str, clinic: str, day: date) -> list[Visit]:
    out: list[Visit] = []
    mode = ""
    seg = ""
    pending: list[str] = []
    for raw in text.splitlines():
        line = nfkc(raw).strip()
        if "【新規来院" in line:
            mode, seg, pending = "来院", "", []
            continue
        if line.startswith("【物販"):
            mode, seg, pending = "物販", "", []
            continue
        if line.startswith("【") or line.startswith("記入") or line.startswith("【問合せ"):
            mode = "" if not line.startswith("【新規来院") else mode
            pending = []
            continue
        if not mode:
            continue
        if not line:
            pending = []
            continue
        body = line.lstrip("・ ").strip()
        if "来客者合計人数" in body or re.search(r"(のお客様|社販)\s*\d+\s*名$", body):
            continue
        amount, rest = _amount(body)
        if amount is None and "×" not in body and (body.endswith("のお客様") or body.startswith("社販")):
            seg = _segment(re.sub(r"\d+\s*件$", "", body).strip())
            pending = []
            continue
        if amount is None:
            pending.append(_clean_item(body))
            continue
        method, mraw = _method_of(body)
        items = pending + ([_clean_item(rest)] if _clean_item(rest) else [])
        out.append(Visit(clinic, day, mode, seg or "（区分の記載なし）", [i for i in items if i], amount, method, mask_names(mraw)))
        pending = []
    return out


def parse_prevention(text: str, clinic: str, day: date) -> list[Visit]:
    out: list[Visit] = []
    stop = ("【予約", "【問い合わせ", "【内覧", "★【保険", "【入金内訳", "【売上", "【問合せ")
    active = True
    for raw in text.splitlines():
        line = nfkc(raw).strip()
        if line.startswith(stop) or any(line.startswith(s) for s in stop):
            active = line.startswith("【来院内訳")
            continue
        if line.startswith("【来院内訳") or line.startswith("【新規来院") or line.startswith("【来院"):
            active = True
            continue
        if not active or not line.startswith("・"):
            continue
        amount, rest = _amount(line)
        if amount is None:
            continue
        method, mraw = _method_of(line)
        seg = "インバウンド" if "インバウンド" in line else "国内"
        out.append(Visit(clinic, day, "来院", seg, [_clean_item(rest)], amount, method, mask_names(mraw)))
    return out


PARSERS = {"9ru": parse_9ru, "prevention": parse_prevention}


def _reports(s, codes, start: date, end: date):
    q = (select(Report, SourcePost).join(SourcePost, SourcePost.post_id == Report.post_id)
         .where(Report.clinic_code.in_(codes), Report.target_date >= start, Report.target_date <= end,
                Report.superseded_by.is_(None), Report.relation != "review", SourcePost.deleted.is_(False))
         .order_by(Report.target_date, SourcePost.posted_at))
    return s.execute(q).all()


def visits(s, codes: list[str], start: date, end: date) -> list[Visit]:
    """お客様（来院）ごとの支払い。同じ院・同じ日に日報が複数あれば新しい投稿を使う。"""
    latest: dict[tuple, SourcePost] = {}
    for r, p in _reports(s, [c for c in codes if c in PARSERS], start, end):
        latest[(r.clinic_code, r.target_date)] = p
    out: list[Visit] = []
    for (c, d), p in sorted(latest.items()):
        try:
            out.extend(PARSERS[c](decrypt_text(p.body_enc), c, d))
        except Exception:  # noqa: BLE001 - 1件の読み取り失敗で画面全体を止めない
            continue
    return out


def unit_prices(s, codes: list[str], start: date, end: date) -> dict[str, dict]:
    """院ごとの 売上・来院者数・客単価（両方が揃っていて要確認でない日だけで計算）。"""
    res = {}
    for c in codes:
        vm = VISITOR_METRIC.get(c)
        if not vm:
            res[c] = {"sales": None, "visitors": None, "price": None, "days": 0, "metric": None}
            continue
        vals: dict[date, dict] = {}
        for f in s.scalars(select(DailyFact).where(
                DailyFact.clinic_code == c, DailyFact.metric.in_(("sales_net", vm)),
                DailyFact.target_date >= start, DailyFact.target_date <= end,
                DailyFact.market == "", DailyFact.cust == "", DailyFact.category == "",
                DailyFact.channel == "", DailyFact.method == "",
                DailyFact.value.isnot(None), DailyFact.status != "review")):
            vals.setdefault(f.target_date, {})[f.metric] = f.value
        days = [v for v in vals.values() if "sales_net" in v and v.get(vm)]
        sales = sum(v["sales_net"] for v in days)
        people = sum(v[vm] for v in days)
        res[c] = {"sales": sales, "visitors": people, "price": (sales / people) if people else None,
                  "days": len(days), "metric": vm}
    return res
