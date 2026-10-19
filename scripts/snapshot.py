import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
"""集計の状態を数値で記録する（再実行の前後比較用）。"""
import json
from sqlalchemy import select, func
from app.db import session_scope
from app.models import *
with session_scope() as s:
    out = {
        "posts": s.scalar(select(func.count()).select_from(SourcePost)),
        "files": s.scalar(select(func.count()).select_from(SourceFile)),
        "observations": s.scalar(select(func.count()).select_from(Observation)),
        "facts": s.scalar(select(func.count()).select_from(DailyFact)),
        "facts_sum": round(s.scalar(select(func.sum(DailyFact.value))) or 0, 2),
        "issues_open": s.scalar(select(func.count()).select_from(Issue).where(Issue.status == "open")),
        "agg": {f"{a.clinic_code}:{a.period}": a.value for a in s.scalars(select(Aggregate).where(Aggregate.metric == "sales_net", Aggregate.dims_key == ""))},
    }
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
