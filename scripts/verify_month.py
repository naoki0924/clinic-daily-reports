import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from datetime import date
from sqlalchemy import select, func
from app.db import session_scope
from app.models import *
from app.bootstrap import active_rule
from app.parsing.excel import parse_workbook
from app.security import load_file
from app import aggregate as ag
with session_scope() as s:
    for c in ("9ru","bio","prevention","osaka"):
        f = s.scalar(select(SourceFile).join(SourcePost,SourceFile.post_pk==SourcePost.id).where(SourcePost.clinic_code==c,SourceFile.status=="parsed").order_by(SourceFile.attached_at.desc()))
        ver, rule = active_rule(s,c,"excel")
        wb = parse_workbook(load_file(f.storage_path), rule, {(2026,m) for m in range(1,11)})
        for ym in [(2026,m) for m in range(1,11)]:
            xs = 0.0; xn=0
            for d,row in wb.days.items():
                if (d.year,d.month)!=ym: continue
                for o in row.obs:
                    if o.metric=="sales_net" and not o.dims and o.num.value: xs+=o.num.value; xn+=1
            start,end = ag.month_range(*ym); end=min(end,date(2026,10,19))
            base = [DailyFact.clinic_code==c,DailyFact.metric=="sales_net",DailyFact.market=="",DailyFact.category=="",DailyFact.method=="",DailyFact.cust=="",DailyFact.channel=="",DailyFact.target_date>=start,DailyFact.target_date<=end]
            facts = s.execute(select(func.sum(DailyFact.value),func.count()).where(*base,DailyFact.value.isnot(None),DailyFact.status!="review")).one()
            rev = s.scalar(select(func.count()).select_from(DailyFact).where(*base,DailyFact.status=="review"))
            agg = s.scalar(select(Aggregate).where(Aggregate.period_type=="M",Aggregate.period==f"{ym[0]}-{ym[1]:02d}",Aggregate.clinic_code==c,Aggregate.metric=="sales_net",Aggregate.dims_key==""))
            tot = wb.month_totals.get(ym,{}).get('sales_net{}')
            print(c, ym, f"file={f.file_name[:24]} rows={xn} excel_sum={xs:,.0f} total_row={tot} | adopted={facts[0] or 0:,.0f} days={facts[1]} review={rev} | agg={(agg.value if agg else 0) or 0:,.0f} prov={agg.provisional if agg else None}")
