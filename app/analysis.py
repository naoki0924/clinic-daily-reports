"""分析コメントの生成。

- 数値の計算はすべてプログラムで行い、「事実」として文章にする。
- AI（Claude API）は任意。使う場合は院名と集計値だけを送り（患者名・本文は送らない）、
  返答は「仮説」として事実と分けて表示する。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

from sqlalchemy import func, select

from .aggregate import Total, clinic_codes, month_range, shift_year, today_jst, total
from .bootstrap import get_setting
from .config import settings
from .models import AnalysisNote, Clinic, DayStatus, Issue

log = logging.getLogger("analysis")
KEY_METRICS = [("sales_net", "売上（手数料抜き）", "円"), ("receipt", "入金", "円"),
               ("visitors_new", "新規来院", "名"), ("contracts", "契約", "件"), ("inquiries", "問い合わせ", "件")]


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}{unit}"


def _pct(a: float | None, b: float | None) -> str | None:
    if a is None or b in (None, 0):
        return None
    return f"{(a - b) / b * 100:+.1f}%"


def build_daily_facts(s, d: date) -> dict:
    names = {c.code: c.name for c in s.scalars(select(Clinic))}
    facts = {"date": d.isoformat(), "clinics": {}}
    for code in clinic_codes(s):
        st = s.scalar(select(DayStatus.status).where(DayStatus.clinic_code == code, DayStatus.target_date == d))
        row = {"name": names[code], "status": st or "unknown", "metrics": {}}
        for metric, label, unit in KEY_METRICS:
            cur = total(s, [code], metric, d, d)
            prev = total(s, [code], metric, d - timedelta(days=1), d - timedelta(days=1))
            wk = total(s, [code], metric, d - timedelta(days=7), d - timedelta(days=7))
            base = [total(s, [code], metric, d - timedelta(days=7 * k), d - timedelta(days=7 * k)).value for k in range(1, 5)]
            base = [b for b in base if b is not None]
            avg = sum(base) / len(base) if len(base) >= 3 else None
            row["metrics"][metric] = {"label": label, "unit": unit, "value": cur.value, "provisional": cur.provisional,
                                      "prev_day": prev.value, "same_weekday_last_week": wk.value,
                                      "same_weekday_avg4": avg}
        facts["clinics"][code] = row
    ms, _ = month_range(d.year, d.month)
    ly_s, ly_e = shift_year(ms, -1), shift_year(d, -1)
    mtd = total(s, clinic_codes(s), "sales_net", ms, d)
    ly = total(s, clinic_codes(s), "sales_net", ly_s, ly_e)
    facts["all_sales_net_mtd"] = {"value": mtd.value, "provisional": mtd.provisional, "note": mtd.coverage_note,
                                  "last_year_same_elapsed": ly.value, "last_year_days": ly.days_with_value}
    facts["open_issues"] = s.scalar(select(func.count()).select_from(Issue).where(Issue.status == "open",
                                                                                  Issue.severity != "info")) or 0
    return facts


def facts_to_text(f: dict) -> str:
    lines = [f"■ {f['date']} の実績（事実）"]
    status_label = {"submitted": "提出済み", "review": "要確認あり", "missing": "未提出", "closed": "休診", "preopen": "開院前",
                    "failed": "取得失敗", "future": "未到来", "unknown": "未処理"}
    for code, c in f["clinics"].items():
        st = status_label.get(c["status"], c["status"])
        if c["status"] in ("missing", "closed", "failed", "preopen"):
            lines.append(f"・{c['name']}：{st}")
            continue
        m = c["metrics"]["sales_net"]
        parts = [f"売上（手数料抜き）{_fmt(m['value'], '円')}" + ("（暫定）" if m["provisional"] else "")]
        if (p := _pct(m["value"], m["prev_day"])):
            parts.append(f"前日比 {p}")
        if (p := _pct(m["value"], m["same_weekday_last_week"])):
            parts.append(f"前週同曜日比 {p}")
        r = c["metrics"]["receipt"]
        parts.append(f"入金 {_fmt(r['value'], '円')}")
        for k in ("visitors_new", "contracts", "inquiries"):
            v = c["metrics"][k]
            if v["value"] is not None:
                parts.append(f"{v['label']} {_fmt(v['value'], v['unit'])}")
        lines.append(f"・{c['name']}（{st}）：" + "、".join(parts))
        avg = m["same_weekday_avg4"]
        if m["value"] is not None and avg and abs(m["value"] - avg) / avg >= 0.5:
            lines.append(f"　→ 直近4週の同曜日平均（{_fmt(avg, '円')}）から {_pct(m['value'], avg)} の大きな変動")
    a = f["all_sales_net_mtd"]
    t = f"■ 全院の売上（手数料抜き）月累計：{_fmt(a['value'], '円')}"
    if a["provisional"]:
        t += f"（暫定：{a['note']}）" if a["note"] else "（暫定）"
    if a["last_year_same_elapsed"] is not None:
        t += f"、前年同期間 {_fmt(a['last_year_same_elapsed'], '円')}（{_pct(a['value'], a['last_year_same_elapsed']) or '比較不可'}）"
    else:
        t += "、前年同期間のデータなし（比較不可）"
    lines.append(t)
    if f["open_issues"]:
        lines.append(f"■ 確認すべき点：未対応の要確認事項が {f['open_issues']} 件あります（確認キューを参照）")
    return "\n".join(lines)


def _ai_hypotheses(facts: dict) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "以下はクリニック4院の日次の集計値（JSON）です。数値はすでに計算済みで正確です。"
            "数値を再計算・創作せず、増減や構成の変化について考えられる要因の仮説と、経営者が確認すべき点を、"
            "日本語の箇条書きで最大5点挙げてください。各項目の先頭に「仮説:」または「確認:」を付けてください。\n\n"
            + json.dumps(facts, ensure_ascii=False)
        )
        resp = client.beta.messages.create(
            model=settings.anthropic_model, max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return ""
        return "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:  # noqa: BLE001  AIは任意機能。失敗しても事実の要約は出す
        log.warning("AI要約に失敗: %s", type(e).__name__)
        return ""


def generate_daily(s, d: date | None = None) -> AnalysisNote:
    d = d or (today_jst() - timedelta(days=1))
    facts = build_daily_facts(s, d)
    text = facts_to_text(facts)
    hyp = _ai_hypotheses(facts) if get_setting(s, "ai").get("enabled") else ""
    row = s.scalar(select(AnalysisNote).where(AnalysisNote.scope == "daily", AnalysisNote.period == d.isoformat()))
    if row is None:
        row = AnalysisNote(scope="daily", period=d.isoformat(), facts=facts, text_facts=text, generator="rule")
        s.add(row)
    row.facts, row.text_facts, row.text_hypotheses = facts, text, hyp
    row.generator = "rule+claude" if hyp else "rule"
    return row
