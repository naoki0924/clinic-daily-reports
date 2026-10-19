"""日報の対象日の判定。

投稿日時と対象日は別に管理する。本文の日付・曜日、添付ファイル名の日付、Excelの入力済み最終日、
投稿日時を照合し、食い違いや年の判定があいまいな場合は「要確認」とする（候補日は示す）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .normalize import nfkc
from .parsing.body import BodyDate

_W = "月火水木金土日"


@dataclass
class DateDecision:
    target: date | None
    status: str                     # confirmed / review
    evidence: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def date_from_filename(name: str) -> tuple[date | None, str]:
    s = nfkc(name)
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})T\d{4}", s)       # 保存日時（BiO）
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))), "保存日時"
    m = re.search(r"(20\d{2})(\d{2})(\d{2})(?![\d版])", s)    # 20261019（PREVENTION）。「20251205版」は書式の版なので除外
    if m:
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d, "日付"
    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", s)       # 2026年10月16日★…
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))), "日付"
    return None, ""


def _safe(y, m, d) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _infer_year(month: int, day: int, posted: date) -> tuple[date | None, bool]:
    """年がない日付に年を補う。投稿日より後になる場合は前年とし、あいまいとして返す。"""
    cand = _safe(posted.year, month, day)
    if cand and cand <= posted + timedelta(days=1):
        return cand, False
    prev = _safe(posted.year - 1, month, day)
    return prev, True


def decide(posted_at: datetime, body_date: BodyDate | None, file_names: list[str],
           excel_last_input: date | None) -> DateDecision:
    posted = posted_at.date()
    ev: dict = {"posted": posted.isoformat()}
    problems: list[str] = []
    votes: dict[date, float] = {}

    def vote(d: date | None, w: float, label: str):
        if d is not None:
            votes[d] = votes.get(d, 0) + w
            ev.setdefault("votes", []).append({"source": label, "date": d.isoformat(), "weight": w})

    body_d = None
    if body_date:
        if body_date.year:
            body_d = _safe(body_date.year, body_date.month, body_date.day)
        else:
            body_d, ambiguous = _infer_year(body_date.month, body_date.day, posted)
            if ambiguous:
                problems.append("本文の日付に年がなく、投稿日より後の日付になるため前年と判断した（年があいまい）")
        if body_d is None:
            problems.append(f"本文の日付「{body_date.raw}」が暦に存在しない")
        ev["body"] = {"raw": body_date.raw, "date": body_d.isoformat() if body_d else None, "weekday": body_date.weekday}
        vote(body_d, 2.0, "本文の日付")
        if body_d and body_date.weekday:
            if _W[body_d.weekday()] != body_date.weekday:
                problems.append(f"本文の日付 {body_d:%m/%d} は{_W[body_d.weekday()]}曜日だが、本文の曜日は「{body_date.weekday}」")
                # 曜日に合う直近の日（投稿日以前）を候補にする
                for k in range(1, 4):
                    for d2 in (body_d - timedelta(days=k), body_d + timedelta(days=k)):
                        if _W[d2.weekday()] == body_date.weekday and d2 <= posted:
                            vote(d2, 1.5, "本文の曜日")
                            break
                    else:
                        continue
                    break

    for fn in file_names:
        fd, kind = date_from_filename(fn)
        if fd and abs((fd - posted).days) > 60:
            ev.setdefault("ignored_files", []).append({"name": fn, "date": fd.isoformat(), "reason": "投稿日から60日以上離れている"})
            fd = None
        if fd:
            vote(fd, 1.5 if kind == "日付" else 1.0, f"ファイル名の{kind}")
            ev.setdefault("files", []).append({"name": fn, "date": fd.isoformat(), "kind": kind})

    if excel_last_input:
        ev["excel_last_input"] = excel_last_input.isoformat()
        vote(excel_last_input, 1.0, "Excelの入力済み最終日")

    vote(posted, 0.75, "投稿日")
    if posted_at.hour < 6:
        vote(posted - timedelta(days=1), 0.5, "深夜投稿（前日分の可能性）")

    if not votes:
        return DateDecision(None, "review", ev, problems + ["対象日を判定できない"])

    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], -kv[0].toordinal()))
    target = ranked[0][0]
    ev["candidates"] = [{"date": d.isoformat(), "score": s} for d, s in ranked[:4]]

    strong = {v["date"] for v in ev.get("votes", []) if v["source"] in ("本文の日付", "本文の曜日") or v["source"].startswith("ファイル名")}
    if len(strong) > 1:
        problems.append("本文・ファイル名の日付が一致しない: " + ", ".join(sorted(strong)))
    if body_d is None and not any(v["source"].startswith("ファイル名") for v in ev.get("votes", [])):
        if excel_last_input and excel_last_input != target:
            problems.append(f"本文に日付がなく、Excelの入力済み最終日（{excel_last_input}）と投稿日が異なる")
    if body_d and excel_last_input and excel_last_input != target and abs((excel_last_input - target).days) <= 7:
        problems.append(f"Excelの入力済み最終日（{excel_last_input}）が対象日と異なる（参考）")
    if target > posted + timedelta(days=1):
        problems.append("対象日が投稿日より後")
    if (posted - target).days > 45:
        problems.append("対象日が投稿日より45日以上前")

    # 参考扱いの指摘だけなら確定
    blocking = [p for p in problems if not p.endswith("（参考）")]
    return DateDecision(target, "review" if blocking else "confirmed", ev, problems)
