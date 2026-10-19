"""サンプルデータ（架空）による動作確認。実データでの検証は docs/03_検証結果.md に別記する。"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from app import aggregate
from app.bootstrap import BODY_RULES, EXCEL_RULES
from app.db import session_scope
from app.models import Aggregate, Clinic, DailyFact, DayStatus, Issue, Observation, Report
from app.normalize import parse_number
from app.parsing.body import parse_body
from app.parsing.excel import ExcelFormatError, parse_workbook
from app.sample import body_9ru, body_bio, body_osaka, body_prevention, workbook_9ru
from tests.helpers import add_post, fact, run

OCT = (2026, 10)
SEP = (2026, 9)


def v(gross, net=None, receipt=None, **kw):
    return {"gross": gross, "net": net if net is not None else gross, "receipt": receipt if receipt is not None else gross, **kw}


# ---- 正規化 ---------------------------------------------------------------

def test_normalize_distinguishes_zero_blank_dash():
    assert parse_number("￥0").state == "zero"
    assert parse_number("").state == "blank"
    assert parse_number("￥-").state == "dash"
    assert parse_number("-名").state == "dash"
    n = parse_number("１，２３４円")
    assert n.state == "value" and n.value == 1234
    s = parse_number("￥3,430,0000")
    assert s.value == 34300000 and "桁区切り" in s.suspicious  # 原表記を保持し、勝手に直さない
    assert parse_number("abc").state == "error"


# ---- 本文 -----------------------------------------------------------------

def test_body_9ru_extracts_summary_lines_only():
    text = body_9ru(date(2026, 10, 19), 1000000, 950000, deposit=50000, credit=950000, visitors=6, new=1, contracts=1,
                    segments={"年パスのお客様": 2, "インバウンド新規のお客様": 1, "インバウンド既存のお客様": 1, "ゲスト既存のお客様": 2})
    r = parse_body(text, BODY_RULES["9ru"])
    got = {(o.metric, tuple(sorted((k, v) for k, v in o.dims.items() if v))): o.num for o in r.observations}
    assert got[("sales_net", ())].value == 1000000
    assert got[("receipt_excl_deposit", ())].value == 950000  # 9ru本文の入金はデポジットを含まない
    assert got[("deposit", ())].value == 50000
    assert got[("visitors_total", ())].value == 6
    assert got[("visitors_new", ())].value == 1 and got[("contracts", ())].value == 1
    assert got[("visitors_segment", (("cust", "年パスのお客様"),))].value == 2
    assert got[("payment", (("method", "クレジット"),))].value == 950000
    assert got[("retail_count", ())].state == "zero"
    assert r.date.month == 10 and r.date.day == 19 and r.date.weekday == "月"
    # 明細行（施術A×1 ￥10,000）は抽出しない
    assert all(o.num.value != 10000 for o in r.observations)


def test_body_prevention_dash_and_osaka_categories_and_bio():
    r = parse_body(body_prevention(date(2026, 10, 19), None, 250000), BODY_RULES["prevention"])
    m = {o.metric: o for o in r.observations if not any(o.dims.values())}
    assert m["sales_net"].num.state == "dash"
    assert m["ar_new"].num.state == "dash"
    assert m["inquiries"].num.state == "dash"
    r = parse_body(body_osaka(date(2026, 10, 16), 10000000, 10000000), BODY_RULES["osaka"])
    cats = [o for o in r.observations if o.metric == "category_visits" and o.dims.get("category") == "細胞治療"]
    assert {(o.dims["market"], o.dims["cust"], o.num.value) for o in cats} == {("インバウンド", "新規", 1), ("インバウンド", "既存", 0)}
    assert r.date.year == 2026 and r.date.day == 16
    r = parse_body(body_bio(400000, 5000, 4000, 1000), BODY_RULES["bio"])
    m = {(o.metric, o.dims.get("category", "") + o.dims.get("method", "") + o.dims.get("channel", "")): o.num for o in r.observations}
    assert m[("sales_incl_cpc", "")].value == 400000 and m[("sales_actual", "")].value == 5000
    assert m[("cpc_amount", "NK")].value == 171600 and m[("cpc_count", "NK")].value == 1
    assert m[("payment", "現金")].value == 1000
    assert m[("inquiries", "メール")].value == 1
    assert r.date is None


# ---- Excel ----------------------------------------------------------------

def test_excel_layout_found_by_labels_with_location():
    d = date(2026, 10, 16)
    data = workbook_9ru({d: v(242500, 160000, 242500, cash=30000, credit=210000, cat_sales=12500, cat_count=1)}, [OCT])
    wb = parse_workbook(data, EXCEL_RULES["9ru"], {OCT})
    row = wb.days[d]
    got = {(o.metric, o.dims.get("method", "") + o.dims.get("category", "") + o.dims.get("market", "")): o for o in row.obs}
    assert got[("sales_net", "")].num.value == 160000 and got[("sales_net", "")].cell == "R21"
    assert got[("receipt", "")].num.value == 242500
    assert got[("payment", "現金")].num.value == 30000
    assert got[("category_sales", "美肌治療国内")].num.value == 12500
    assert got[("category_count", "美肌治療国内")].num.value == 1
    assert wb.last_input_day(*OCT) == d


def test_excel_format_change_is_detected_not_misread():
    d = date(2026, 10, 16)
    from openpyxl import load_workbook
    import io
    data = workbook_9ru({d: v(100)}, [OCT])
    wb = load_workbook(io.BytesIO(data))
    wb["2026年10月"]["R5"] = "売上（変更後の見出し）"
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(ExcelFormatError):
        parse_workbook(out.getvalue(), EXCEL_RULES["9ru"], {OCT})


# ---- 照合・二重計上防止 ----------------------------------------------------

def test_body_and_excel_counted_once_and_rerun_idempotent():
    d = date(2026, 10, 15)
    with session_scope() as s:
        p = add_post(s, "9ru", 1, datetime(2026, 10, 15, 18, 45),
                     body_9ru(d, 500000, 500000), workbook_9ru({d: v(500000)}, [OCT]))
        run(s, [p], [("9ru", d)])
        f = fact(s, "9ru", d, "sales_net")
        assert f.value == 500000 and f.status == "confirmed"
        agg = s.scalar(select(Aggregate).where(Aggregate.period == "2026-10", Aggregate.clinic_code == "9ru",
                                               Aggregate.metric == "sales_net", Aggregate.dims_key == ""))
        assert agg.value == 500000  # 本文＋Excelで 1,000,000 にならない
        n_obs = s.scalar(select(func.count()).select_from(Observation))
        # 再実行
        run(s, [p], [("9ru", d)])
        run(s, [p], [("9ru", d)])
        assert s.scalar(select(func.count()).select_from(Observation)) == n_obs
        agg = s.scalar(select(Aggregate).where(Aggregate.period == "2026-10", Aggregate.clinic_code == "9ru",
                                               Aggregate.metric == "sales_net", Aggregate.dims_key == ""))
        assert agg.value == 500000
        assert s.scalar(select(func.count()).select_from(DailyFact).where(DailyFact.metric == "sales_net")) == 1


def test_body_excel_mismatch_goes_to_review_with_both_sources():
    d = date(2026, 10, 14)
    with session_scope() as s:
        # 実データで見つかった事例（数値は架空）：本文は振込を除いた売上、Excelは振込を含む売上
        p = add_post(s, "9ru", 2, datetime(2026, 10, 14, 18, 50), body_9ru(d, 1400000, 1400000),
                     workbook_9ru({d: v(24000000, 24000000, 24000000)}, [OCT]))
        run(s, [p], [("9ru", d)])
        f = fact(s, "9ru", d, "sales_net")
        assert f.status == "review" and f.value is None and f.state == "conflict"
        iss = s.scalar(select(Issue).where(Issue.kind == "body_excel_mismatch", Issue.metric == "sales_net"))
        assert iss.detail["body"]["value"] == 1400000 and iss.detail["excel"]["value"] == 24000000
        assert "R19" in iss.detail["excel"]["location"]
        assert fact(s, "9ru", d, "receipt").status == "confirmed"           # Excelの入金合計
        assert fact(s, "9ru", d, "receipt_excl_deposit").value == 1400000   # 本文の入金（別指標）
        st = s.scalar(select(DayStatus).where(DayStatus.clinic_code == "9ru", DayStatus.target_date == d))
        assert st.status == "review"
        agg = s.scalar(select(Aggregate).where(Aggregate.period == "2026-10", Aggregate.clinic_code == "9ru",
                                               Aggregate.metric == "sales_net"))
        assert agg is None or agg.provisional


def test_delayed_post_counts_on_target_date():
    with session_scope() as s:
        p = add_post(s, "osaka", 3, datetime(2026, 10, 19, 11, 31), body_osaka(date(2026, 10, 16), 10000000, 10000000))
        run(s, [p], [("osaka", date(2026, 10, 16)), ("osaka", date(2026, 10, 19))])
        r = s.scalar(select(Report).where(Report.post_id == 3))
        assert r.target_date == date(2026, 10, 16)
        # 大阪本文の「売上」は実データ照合で手数料込みと確認済み
        assert fact(s, "osaka", date(2026, 10, 16), "sales_gross").value == 10000000
        assert fact(s, "osaka", date(2026, 10, 16), "sales_gross").status == "confirmed"
        assert fact(s, "osaka", date(2026, 10, 19), "sales_gross") is None


def test_date_conflict_body_weekday_and_filename():
    # 10/8（木）の投稿で、本文が「10月9日 木曜日」、ファイル名は 20261008（実データで見つかった書式の事例。数値は架空）
    d = date(2026, 10, 8)
    with session_scope() as s:
        p = add_post(s, "prevention", 4, datetime(2026, 10, 8, 18, 3),
                     body_prevention(d, 100000, 100000, day_override=9, weekday_override="木"),
                     None, "★PREVENTION日報_ 20261008.xlsx")
        run(s, [p], [("prevention", d)])
        r = s.scalar(select(Report).where(Report.post_id == 4))
        assert r.target_date == d and r.date_status == "review"
        assert s.scalar(select(Issue).where(Issue.kind == "date_review", Issue.post_id == 4)) is not None


def test_repost_correction_and_ambiguous_duplicates():
    d = date(2026, 10, 13)
    with session_scope() as s:
        a = add_post(s, "9ru", 10, datetime(2026, 10, 13, 18, 0), body_9ru(d, 100000, 100000))
        b = add_post(s, "9ru", 11, datetime(2026, 10, 13, 18, 5), body_9ru(d, 100000, 100000))  # 再投稿（同じ値）
        run(s, [a, b], [("9ru", d)])
        assert s.scalar(select(Report.relation).where(Report.post_id == 11)) == "repost"
        assert fact(s, "9ru", d, "sales_net").value == 100000
        c = add_post(s, "9ru", 12, datetime(2026, 10, 13, 20, 0), "【訂正】\n" + body_9ru(d, 120000, 120000))
        run(s, [c], [("9ru", d)])
        assert fact(s, "9ru", d, "sales_net").value == 120000  # 訂正を反映（合算しない）
        e = add_post(s, "9ru", 13, datetime(2026, 10, 13, 21, 0), body_9ru(d, 5000, 5000))  # 訂正表記のない別の値
        run(s, [e], [("9ru", d)])
        assert fact(s, "9ru", d, "sales_net").status == "review"
        assert s.scalar(select(Issue).where(Issue.kind == "duplicate_report", Issue.status == "open")) is not None
        agg = s.scalar(select(Aggregate).where(Aggregate.period == "2026-10", Aggregate.clinic_code == "9ru",
                                               Aggregate.metric == "sales_net"))
        assert agg is None or agg.value != 225000


def test_post_edit_updates_month_and_year():
    d = date(2026, 10, 2)
    with session_scope() as s:
        p = add_post(s, "9ru", 20, datetime(2026, 10, 2, 18, 0), body_9ru(d, 100000, 100000))
        run(s, [p], [("9ru", d)])
        p = add_post(s, "9ru", 20, datetime(2026, 10, 2, 19, 0), body_9ru(d, 150000, 150000))  # 編集
        run(s, [p], [("9ru", d)])
        m = s.scalar(select(Aggregate.value).where(Aggregate.period_type == "M", Aggregate.period == "2026-10",
                                                   Aggregate.clinic_code == "9ru", Aggregate.metric == "sales_net"))
        y = s.scalar(select(Aggregate.value).where(Aggregate.period_type == "Y", Aggregate.period == "2026",
                                                   Aggregate.clinic_code == "9ru", Aggregate.metric == "sales_net"))
        assert m == 150000 and y == 150000


def test_comment_excel_correction_uses_newest_file():
    d = date(2026, 9, 30)
    with session_scope() as s:
        p = add_post(s, "9ru", 30, datetime(2026, 9, 30, 18, 41), body_9ru(d, 200000, 200000),
                     workbook_9ru({d: v(999999)}, [SEP]),
                     comment="この数字もおかしいです。修正してください。",
                     comment_excel=workbook_9ru({d: v(200000)}, [SEP]))
        run(s, [p], [("9ru", d)])
        assert fact(s, "9ru", d, "sales_net").value == 200000
        assert fact(s, "9ru", d, "sales_net").status == "confirmed"
        assert s.scalar(select(Issue).where(Issue.kind == "comment_correction")) is not None


def test_missing_closed_and_explicit_zero_are_distinct():
    with session_scope() as s:
        s.scalar(select(Clinic).where(Clinic.code == "prevention")).closed_weekdays = [6]  # 日曜定休
        z = add_post(s, "prevention", 40, datetime(2026, 10, 3, 18, 0), body_prevention(date(2026, 10, 3), 0, 0))
        run(s, [z], [("prevention", date(2026, 10, 3)), ("prevention", date(2026, 10, 4)), ("prevention", date(2026, 10, 5))])
        st = dict(s.execute(select(DayStatus.target_date, DayStatus.status).where(DayStatus.clinic_code == "prevention")).all())
        assert st[date(2026, 10, 3)] == "submitted"
        assert st[date(2026, 10, 4)] == "closed"
        assert st[date(2026, 10, 5)] == "missing"
        assert fact(s, "prevention", date(2026, 10, 3), "sales_net").state == "zero"
        assert fact(s, "prevention", date(2026, 10, 5), "sales_net") is None  # 未提出を売上0にしない
        dash = fact(s, "prevention", date(2026, 10, 3), "ar_new")
        assert dash.state == "dash" and dash.value is None


def test_uncached_formula_is_flagged():
    d = date(2026, 10, 6)
    with session_scope() as s:
        p = add_post(s, "9ru", 50, datetime(2026, 10, 6, 18, 0), body_9ru(d, 300000, 300000),
                     workbook_9ru({d: v(300000, cash=100000, credit=200000)}, [OCT], formula_no_cache=d))
        run(s, [p], [("9ru", d)])
        assert s.scalar(select(Issue).where(Issue.kind == "excel_formula")) is not None
        assert fact(s, "9ru", d, "receipt").status == "review"


def test_later_excel_change_is_detected():
    d1, d2 = date(2026, 10, 6), date(2026, 10, 7)
    with session_scope() as s:
        a = add_post(s, "9ru", 60, datetime(2026, 10, 6, 18, 0), body_9ru(d1, 100000, 100000), workbook_9ru({d1: v(100000)}, [OCT]))
        b = add_post(s, "9ru", 61, datetime(2026, 10, 7, 18, 0), body_9ru(d2, 50000, 50000),
                     workbook_9ru({d1: v(130000), d2: v(50000)}, [OCT]))
        run(s, [a, b], [("9ru", d1), ("9ru", d2)])
        assert fact(s, "9ru", d1, "sales_net").status == "review"
        iss = s.scalar(select(Issue).where(Issue.kind == "excel_later_change", Issue.target_date == d1))
        assert iss.detail["later"] == 130000
        assert fact(s, "9ru", d2, "sales_net").value == 50000


def test_cross_clinic_total_only_for_unified_metrics():
    d = date(2026, 10, 15)
    with session_scope() as s:
        a = add_post(s, "9ru", 70, datetime(2026, 10, 15, 18, 0), body_9ru(d, 100000, 100000, deposit=5000))
        b = add_post(s, "prevention", 71, datetime(2026, 10, 15, 18, 0), body_prevention(d, 200000, 200000))
        run(s, [a, b], [("9ru", d), ("prevention", d)])
        all_sales = s.scalar(select(Aggregate).where(Aggregate.clinic_code == "ALL", Aggregate.period == "2026-10",
                                                     Aggregate.metric == "sales_net"))
        assert all_sales.value == 300000 and all_sales.provisional  # 他院の値がないので暫定
        assert s.scalar(select(Aggregate).where(Aggregate.clinic_code == "ALL", Aggregate.metric == "deposit")) is None
        assert s.scalar(select(Aggregate).where(Aggregate.clinic_code == "ALL", Aggregate.metric == "visitors_total")) is None


def test_inconsistency_detected_without_changing_values():
    d = date(2026, 10, 19)
    with session_scope() as s:
        p = add_post(s, "prevention", 80, datetime(2026, 10, 19, 18, 15), body_prevention(d, 500000, 250000, visitors=3, new=4))
        run(s, [p], [("prevention", d)])
        kinds = {i.title for i in s.scalars(select(Issue).where(Issue.kind == "inconsistency"))}
        assert any("新規来院 4" in t for t in kinds)
        assert fact(s, "prevention", d, "visitors_total").value == 3  # 原文の値のまま


def test_other_clinic_post_excluded():
    with session_scope() as s:
        p = add_post(s, "osaka", 90, datetime(2026, 6, 3, 18, 34), "ＢiOスタイル 神戸\n【売上入金】\n売上：￥100,000\n入金：￥100,000")
        run(s, [p], [])
        assert s.scalar(select(Report).where(Report.post_id == 90)) is None
        assert s.scalar(select(Issue).where(Issue.kind == "other_clinic")) is not None


def test_rate_uses_sums_and_zero_denominator():
    t = aggregate.Total(value=10)
    assert aggregate.rate(t, aggregate.Total(value=0)) is None
    assert aggregate.rate(aggregate.Total(value=3), aggregate.Total(value=12)) == 0.25


# ---- 実データで見つかった旧書式（2026年7〜8月） ------------------------------

def test_prevention_old_format_is_read():
    text = "\n".join(["7月15日　水曜日　担当医", "", "【来院】", "　ドック　2名", "　内覧　3名", "", "【売上】",
                      "　売上（手数料抜き）¥120,000", "　入金　　¥200,000（内¥80,000は先入金）", "", "【入金内訳】",
                      "【問い合わせ]　電話1件　直来2件"])
    r = parse_body(text, BODY_RULES["prevention"])
    m = {(o.metric, o.dims.get("cust", "") + o.dims.get("channel", "")): o.num for o in r.observations}
    assert m[("sales_net", "")].value == 120000
    assert m[("receipt", "")].value == 200000
    assert m[("visitors_segment", "ドック")].value == 2 and m[("visitors_segment", "内覧")].value == 3
    assert m[("inquiries", "電話")].value == 1 and m[("inquiries", "直来")].value == 2
    from app.pipeline import classify
    assert classify(text, BODY_RULES["prevention"])[0] == "daily"


def test_osaka_heading_without_open_bracket_and_attachment_only_post():
    d = date(2026, 7, 29)
    body = body_osaka(d, 800000, 800000).replace("【売上入金】", "売上入金】")
    with session_scope() as s:
        f_only = add_post(s, "osaka", 95, datetime(2026, 7, 29, 18, 56), "", None)
        from tests.helpers import _add_file
        from app.sample import workbook_9ru  # 形式は問わない：結び付けの確認
        _add_file(s, f_only, b"PK-not-a-real-xlsx", "★BiO STYLE CLINIC大阪_日報 (2).xlsx", datetime(2026, 7, 29, 18, 56), None)
        rep = add_post(s, "osaka", 96, datetime(2026, 7, 30, 11, 15), body)
        run(s, [f_only, rep], [("osaka", d)])
        from app.models import SourcePost
        assert s.scalar(select(SourcePost.category).where(SourcePost.post_id == 95)) == "attachment_only"
        r = s.scalar(select(Report).where(Report.post_id == 96))
        assert r.target_date == d and r.primary_file_id is not None  # 添付だけの投稿のExcelを日報に結び付け
        assert fact(s, "osaka", d, "sales_gross").value == 800000


def test_preopen_days_are_not_missing_and_not_counted_as_lacking():
    with session_scope() as s:
        s.scalar(select(Clinic).where(Clinic.code == "osaka")).open_from = date(2026, 7, 28)
        p = add_post(s, "9ru", 300, datetime(2026, 7, 20, 18, 0), body_9ru(date(2026, 7, 20), 100000, 100000))
        q = add_post(s, "osaka", 301, datetime(2026, 7, 30, 18, 0), body_osaka(date(2026, 7, 30), 50000, 50000))
        run(s, [p, q], [("osaka", date(2026, 7, 27)), ("osaka", date(2026, 7, 28)), ("osaka", date(2026, 7, 30)),
                        ("9ru", date(2026, 7, 20))])
        st = dict(s.execute(select(DayStatus.target_date, DayStatus.status).where(DayStatus.clinic_code == "osaka")).all())
        assert st[date(2026, 7, 27)] == "preopen"
        assert st[date(2026, 7, 28)] == "missing"
        assert st[date(2026, 7, 30)] == "submitted"
        t = aggregate.total(s, ["osaka"], "sales_gross", date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 8, 1))
        assert t.days_missing == 2  # 7/28・7/29。開院前の7/27は未提出に数えない
