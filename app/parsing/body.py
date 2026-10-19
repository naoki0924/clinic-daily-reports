"""日報本文の読み取り（院別ルールで行単位に抽出）。

- 記載がある項目だけを抽出し、欠けている数値は推測しない。
- 必須項目（expected）が無ければ「未記載」として記録する。
- 抜粋（excerpt）は一致した集計行だけを残す。患者名などを含む明細行は保存しない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..normalize import NumValue, nfkc, parse_number, squash
from ..rules import METHOD_ALIASES

_SECTION_RE = re.compile(r"^\s*★?\s*【([^】\]]+)[】\]]")
_SECTION_NOOPEN_RE = re.compile(r"^\s*([^\s【】\]・]{2,12})[】\]]\s*$")  # 「売上入金】」のように【が抜けた見出し
_SUB_RE = re.compile(r"^\s*[〈<]\s*([^〉>]+?)\s*[〉>]")
_WEEKDAYS = "月火水木金土日"


@dataclass
class BodyObs:
    metric: str
    dims: dict
    num: NumValue
    line_no: int | None
    excerpt: str
    definition_confirmed: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class BodyDate:
    year: int | None
    month: int
    day: int
    weekday: str | None
    line_no: int
    raw: str


@dataclass
class BodyResult:
    observations: list[BodyObs]
    date: BodyDate | None
    lines_total: int


def _method(label: str) -> str:
    key = re.sub(r"\s+", " ", nfkc(label)).strip().lower()
    return METHOD_ALIASES.get(key, METHOD_ALIASES.get(key.replace(" ", ""), nfkc(label).strip()))


def extract_date(text: str, rule: dict) -> BodyDate | None:
    lines = text.splitlines()
    limit = rule.get("line_limit", 2)
    seen = 0
    for i, raw in enumerate(lines, start=1):
        line = nfkc(raw).strip()
        if not line:
            continue
        seen += 1
        if seen > limit:
            break
        for pat in rule.get("patterns", []):
            m = re.search(pat, line)
            if m:
                gd = m.groupdict()
                return BodyDate(
                    year=int(gd["y"]) if gd.get("y") else None,
                    month=int(gd["m"]), day=int(gd["d"]),
                    weekday=gd.get("w") or _find_weekday(line[m.end():m.end() + 6]),
                    line_no=i, raw=m.group(0),
                )
    return None


def _find_weekday(s: str) -> str | None:
    m = re.match(r"\s*([月火水木金土日])曜", s)
    return m.group(1) if m else None


def parse_body(text: str, rule: dict) -> BodyResult:
    lines = text.splitlines()
    section = ""
    subsection = ""
    found: dict[tuple, BodyObs] = {}
    order: list[tuple] = []
    exclude_after = [squash(x) for x in rule.get("classify", {}).get("exclude_after", [])]

    for i, raw in enumerate(lines, start=1):
        line = re.sub(r"[ \t]+", " ", nfkc(raw)).strip()
        if not line:
            continue
        sm = _SECTION_RE.match(line) or _SECTION_NOOPEN_RE.match(line)
        if sm:
            section = squash(sm.group(1))
            if any(section == squash(x.strip("【】")) for x in exclude_after):
                break  # 月間総集計など、日次でない部分は読まない
        sub = _SUB_RE.match(line)
        if sub:
            subsection = squash(sub.group(1))

        for lr in rule.get("lines", []):
            if "section" in lr and not re.search(lr["section"], section or ""):
                continue
            if "section_not" in lr and re.search(lr["section_not"], section or ""):
                continue
            if "subsection" in lr and not re.search(lr["subsection"], subsection or ""):
                continue
            m = re.search(lr["re"], line, flags=re.IGNORECASE)
            if not m:
                continue
            gd = m.groupdict()
            note = (gd.get("note") or "").strip()
            for out in lr["out"]:
                grp = out["group"]
                if grp not in gd:
                    continue
                num = parse_number(gd.get(grp))
                dims = dict(out.get("dims", {}))
                for dim, g in out.get("dims_group", {}).items():
                    val = (gd.get(g) or "").strip()
                    dims[dim] = _method(val) if (dim == "method" and out.get("method_alias")) else val
                if out.get("dims_subsection"):
                    dims[out["dims_subsection"]] = subsection
                notes = []
                for key, msg in lr.get("note_flags", {}).items():
                    if key in note:
                        notes.append(msg)
                if note:
                    notes.append(f"注記: {note[:40]}")
                key = (out["metric"], tuple(sorted(dims.items())))
                obs = BodyObs(out["metric"], dims, num, i, m.group(0)[:80],
                              out.get("definition_confirmed", True), notes)
                if key in found:
                    prev = found[key]
                    if prev.num.value != num.value or prev.num.state != num.state:
                        prev.notes.append(f"本文内に同じ項目が複数あり値が異なる（{i}行目: {num.raw}）")
                    continue
                found[key] = obs
                order.append(key)
            break  # 1行は最初に一致したルールだけで読む

    # 必須項目が無い → 未記載
    for lr in rule.get("lines", []):
        if not lr.get("expected"):
            continue
        for out in lr["out"]:
            if out.get("dims_group") or out.get("dims_subsection"):
                continue
            key = (out["metric"], tuple(sorted(out.get("dims", {}).items())))
            if key not in found:
                found[key] = BodyObs(out["metric"], dict(out.get("dims", {})), NumValue("absent", None, ""), None,
                                     "", out.get("definition_confirmed", True), ["本文に記載なし"])
                order.append(key)

    return BodyResult([found[k] for k in order], extract_date(text, rule.get("date", {})), len(lines))


def weekday_jp(d: date) -> str:
    return _WEEKDAYS[d.weekday()]
