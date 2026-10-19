"""数値表記の正規化。

明示的な「0」、空欄、「-」、未記載、読取失敗を区別する。
原表記は必ず残し、誤記の疑いがあれば suspicious に理由を入れる（値は勝手に直さない）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

DASHES = {"-", "－", "ー", "―", "‐", "−", "─", "–", "—"}
_UNIT_RE = re.compile(r"[￥¥\\円名件人本回枚個]")
_STRICT_COMMA = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_PLAIN = re.compile(r"^-?\d+(\.\d+)?$")
_LOOSE = re.compile(r"^-?[\d,]+(\.\d+)?$")


@dataclass
class NumValue:
    state: str            # value / zero / blank / dash / absent / error
    value: float | None
    raw: str
    suspicious: str = ""

    @property
    def has_number(self) -> bool:
        return self.state in ("value", "zero")


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def squash(s: str) -> str:
    """見出し比較用：NFKC＋空白・改行除去。"""
    return re.sub(r"\s+", "", nfkc(str(s)))


def parse_number(raw: str | None) -> NumValue:
    if raw is None:
        return NumValue("blank", None, "")
    original = str(raw)
    s = nfkc(original).strip()
    s = s.replace("，", ",").replace("、", ",")
    s = _UNIT_RE.sub("", s).strip()
    s = re.sub(r"\s+", "", s)
    if s == "":
        return NumValue("blank", None, original)
    if s in DASHES or set(s) <= DASHES:
        return NumValue("dash", None, original)
    if s.startswith("+"):
        s = s[1:]
    if _PLAIN.match(s) or _STRICT_COMMA.match(s):
        v = float(s.replace(",", ""))
        return NumValue("zero" if v == 0 else "value", v, original)
    if _LOOSE.match(s):
        v = float(s.replace(",", ""))
        return NumValue("zero" if v == 0 else "value", v, original, "桁区切りの位置が不正（誤記の疑い）")
    return NumValue("error", None, original, "数値として読めない表記")


def from_cell(value) -> NumValue:
    """Excelセルの値（計算済みの値）を NumValue に変換する。"""
    if value is None:
        return NumValue("blank", None, "")
    if isinstance(value, bool):
        return NumValue("error", None, str(value), "真偽値")
    if isinstance(value, (int, float)):
        v = float(value)
        return NumValue("zero" if v == 0 else "value", v, str(value))
    s = str(value)
    if s.startswith("#"):  # #REF! #DIV/0! など
        return NumValue("error", None, s, "Excelのエラー値")
    return parse_number(s)
