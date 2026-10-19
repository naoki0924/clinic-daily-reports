import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
"""採用値（日次）と、最新のExcelの日別行を突き合わせる。"""
from datetime import date
from sqlalchemy import select
from app.db import session_scope
from app.models import *
from app.bootstrap import active_rule
from app.parsing.excel import parse_workbook
from app.security import load_file
clinic = sys.argv[1]; metric = sys.argv[2] if len(sys.argv) > 2 else "sales_net"
with session_scope() as s:
    f = s.scalar(select(SourceFile).join(SourcePost,SourceFile.post_pk==SourcePost.id).where(SourcePost.clinic_code==clinic,SourceFile.status=="parsed").order_by(SourceFile.attached_at.desc()))
    wb = parse_workbook(load_file(f.storage_path), active_rule(s,clinic,"excel")[1], {(2026,m) for m in range(1,11)})
    for d in sorted(wb.days):
        xv = next((o.num.value for o in wb.days[d].obs if o.metric==metric and not o.dims), None)
        fa = s.scalar(select(DailyFact).where(DailyFact.clinic_code==clinic,DailyFact.target_date==d,DailyFact.metric==metric,DailyFact.market=="",DailyFact.cust=="",DailyFact.category=="",DailyFact.channel=="",DailyFact.method==""))
        st = s.scalar(select(DayStatus.status).where(DayStatus.clinic_code==clinic,DayStatus.target_date==d))
        fv = fa.value if fa else None
        mark = "" if (xv or 0) == (fv or 0) else "  <-- 差"
        print(d, f"latestExcel={xv}", f"adopted={fv}", fa.status if fa else "-", st, (fa.note[:70] if fa else ""), mark)
