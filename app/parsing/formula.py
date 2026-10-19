"""保存済み計算結果の検証用に、単純な数式（四則演算・SUM）だけを評価する。

評価できない数式は None を返し、呼び出し側は「検証不能」として扱う。
"""
from __future__ import annotations

import re

from openpyxl.utils.cell import column_index_from_string, range_boundaries

_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|(SUM)\(([^)]*)\)|(\$?[A-Z]{1,3}\$?\d+)|([-+*/()]))", re.IGNORECASE)


class Unsupported(Exception):
    pass


def _cell_value(ws_values, ref: str):
    ref = ref.replace("$", "")
    m = re.match(r"([A-Z]+)(\d+)", ref.upper())
    col, row = column_index_from_string(m.group(1)), int(m.group(2))
    v = ws_values.cell(row=row, column=col).value
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    raise Unsupported(f"非数値の参照 {ref}")


def _sum_range(ws_values, arg: str) -> float:
    total = 0.0
    for part in arg.split(","):
        part = part.strip().replace("$", "")
        if not part:
            continue
        if "!" in part:
            raise Unsupported("他シート参照")
        if ":" in part:
            c1, r1, c2, r2 = range_boundaries(part)
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    v = ws_values.cell(row=r, column=c).value
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        total += float(v)
        else:
            total += _cell_value(ws_values, part)
    return total


def evaluate(formula: str, ws_values) -> float | None:
    """'=+A1+B2', '=SUM(A1:C1)', '=13992+3630' 程度を評価。対象外は None。"""
    if not formula or not formula.startswith("="):
        return None
    expr = formula[1:]
    if "!" in expr or '"' in expr:
        return None
    out: list[str] = []
    pos = 0
    try:
        while pos < len(expr):
            m = _TOKEN.match(expr, pos)
            if not m or m.end() == pos:
                return None
            num, fn, arg, ref, op = m.groups()
            if num:
                out.append(num)
            elif fn:
                out.append(repr(_sum_range(ws_values, arg)))
            elif ref:
                out.append(repr(_cell_value(ws_values, ref)))
            elif op:
                out.append(op)
            pos = m.end()
        code = "".join(out)
        if not re.fullmatch(r"[\d.eE+\-*/() ]+", code):
            return None
        return float(eval(code, {"__builtins__": {}}, {}))  # noqa: S307 数字と演算子のみに限定済み
    except (Unsupported, ZeroDivisionError, SyntaxError, ValueError):
        return None
