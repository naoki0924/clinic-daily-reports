"""指標の定義と、院別・書式別の読み取りルール（初版）。

ここにある内容は初期値で、起動時に DB（metric_def / parse_rule）へ登録される。
以後は管理画面から項目名・表記ゆれを変更でき、変更は新しい版として保存される。
いずれも 2026-10-19 に4院の実データ（本文・添付Excel）で確認した書式に基づく。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 指標の定義
#   kind: flow=日別に合算できる / balance=残高（日別に合算しない）
#   cross_clinic_sum: 定義が統一できており全院合計を出してよいか
# ---------------------------------------------------------------------------
METRICS: list[dict] = [
    dict(code="sales_net", name="売上（手数料抜き）", unit="yen", fee_basis="excl", internal_tx="incl",
         cross_clinic_sum=True, description="各院の報告する売上から決済手数料を差し引いた額。全院合計の基本指標。"),
    dict(code="sales_gross", name="売上（手数料込み）", unit="yen", fee_basis="incl", internal_tx="incl",
         cross_clinic_sum=True, description="Excelの『売上計 ※手数料込』。"),
    dict(code="sales_incl_cpc", name="売上（CPC受託・手数料込）", unit="yen", fee_basis="incl", internal_tx="incl",
         cross_clinic_sum=False, description="BiO STYLE CLINIC本文の『●売上』。グループ内CPC受託を含むため全院合計に使わない。"),
    dict(code="sales_actual", name="実売（手数料差引・グループ内CPC除く）", unit="yen", fee_basis="excl", internal_tx="excl",
         cross_clinic_sum=False, description="BiO STYLE CLINIC本文の『●実売』。"),
    dict(code="receipt", name="入金", unit="yen", cross_clinic_sum=True,
         description="当日の入金合計。各院Excelの『入金合計』の数式で確認：現金＋クレジット等＋振込＋デポジット（大阪はクーポン手数料を控除）。"),
    dict(code="receipt_excl_deposit", name="入金（デポジット除く）", unit="yen", cross_clinic_sum=False,
         description="9ru本文の『入金』。デポジットを含まないため、Excelの入金合計とは別の指標として保存する。"),
    dict(code="transfer", name="振込（入金）", unit="yen", cross_clinic_sum=True,
         description="振込による入金。後払い・事前入金を含む場合があり、売上とは別に管理する。"),
    dict(code="ar_new", name="売掛（当日発生額）", unit="yen", cross_clinic_sum=True,
         description="当日に発生した売掛。残高ではない（残高は日別に合算しない）。"),
    dict(code="deposit", name="デポジット", unit="yen", cross_clinic_sum=False,
         description="新規預りか利用額かが院ごとに未確定のため、合算せず要確認として扱う。"),
    dict(code="offset", name="相殺", unit="yen", cross_clinic_sum=True),
    dict(code="payment", name="決済方法別金額", unit="yen", cross_clinic_sum=False,
         description="区分（決済方法・市場）ごとの金額。院により区分が異なる。"),
    dict(code="payment_count", name="決済方法別件数", unit="count", cross_clinic_sum=False),
    dict(code="cpc_amount", name="CPC受託金額", unit="yen", cross_clinic_sum=False, internal_tx="incl",
         description="グループ内取引を含む。内部取引の消去情報がないため連結売上には使わない。"),
    dict(code="cpc_count", name="CPC受託件数", unit="count", cross_clinic_sum=False),
    dict(code="insurance_sales", name="保険診療 売上", unit="yen", cross_clinic_sum=False),
    dict(code="insurance_receipt", name="保険診療 入金", unit="yen", cross_clinic_sum=False),
    dict(code="insurance_ar", name="保険診療 売掛", unit="yen", cross_clinic_sum=False),
    dict(code="insurance_visitors", name="保険診療 来院", unit="person", cross_clinic_sum=False),
    dict(code="visitors_total", name="来院者合計", unit="person", cross_clinic_sum=False,
         description="院の報告値。人数の数え方が院ごとに異なる可能性があるため全院合計しない。"),
    dict(code="visitors_new", name="新規来院", unit="person", cross_clinic_sum=False),
    dict(code="visitors_existing", name="既存来院", unit="person", cross_clinic_sum=False),
    dict(code="visitors_segment", name="来院者（顧客区分別）", unit="person", cross_clinic_sum=False),
    dict(code="contracts", name="契約", unit="count", cross_clinic_sum=False),
    dict(code="inquiries", name="問い合わせ", unit="count", cross_clinic_sum=False),
    dict(code="inquiries_confirmed", name="問い合わせ確定", unit="count", cross_clinic_sum=False),
    dict(code="retail_count", name="物販", unit="count", cross_clinic_sum=False),
    dict(code="treatment_count", name="施術・診療件数（概要）", unit="count", cross_clinic_sum=False,
         description="件数であり実人数ではない。"),
    dict(code="category_sales", name="施術カテゴリ別売上", unit="yen", cross_clinic_sum=False),
    dict(code="category_count", name="施術カテゴリ別件数", unit="count", cross_clinic_sum=False,
         description="施術件数。来院者の実人数とはみなさない。"),
    dict(code="category_visits", name="カテゴリ別来院件数", unit="count", cross_clinic_sum=False),
    dict(code="category_contracts", name="カテゴリ別契約件数", unit="count", cross_clinic_sum=False),
]

# 本文とExcelの両方にある指標（照合対象）
RECONCILE_METRICS = {"sales_net", "sales_gross", "receipt", "transfer", "ar_new", "deposit", "offset"}

# ---------------------------------------------------------------------------
# 対象院
# ---------------------------------------------------------------------------
CLINICS: list[dict] = [
    dict(code="9ru", name="CLINIC 9ru", group_id=391011, sort_order=1),
    dict(code="bio", name="BiO STYLE CLINIC", group_id=300380, sort_order=2),
    dict(code="prevention", name="PREVENTION CLINIC", group_id=474299, sort_order=3),
    dict(code="osaka", name="BiO STYLE CLINIC大阪", group_id=364176, sort_order=4),
]

# 決済方法の表記ゆれ → 正規名
METHOD_ALIASES = {
    "現金": "現金", "クレジット": "クレジット", "カード": "クレジット",
    "wechatpay": "WeChatPay", "wechat": "WeChatPay", "wechat pay": "WeChatPay",
    "alipay": "Alipay", "aripay": "Alipay", "アリペイ": "Alipay",
    "銀聯": "銀聯", "振込": "振込", "デポジット": "デポジット", "売掛": "売掛", "ポイント": "ポイント",
}

_V = r"(?P<v>[¥\\]?\s*-?[\d,]*|[¥\\]?\s*[-ー－]+)"  # 金額（空欄・「-」も受ける）

# ---------------------------------------------------------------------------
# 本文の読み取りルール
#   section / subsection: 現在の【見出し】/〈小見出し〉に対する正規表現（squash後）
#   re: 行（NFKC・前後空白除去後）に対する正規表現。名前付きグループ v, c 等を値に使う
#   out: 出力。metric, group（値のグループ名）, dims（固定）, dims_group（グループ名から）,
#        dims_subsection（小見出しを入れる区分名）, method_alias（決済方法の正規化）
#   expected: 投稿に必ずある項目。無ければ「未記載」として記録する
# ---------------------------------------------------------------------------
BODY_RULES: dict[str, dict] = {
    "9ru": {
        "classify": {"must": ["【売上入金】"], "monthly": ["月次集計", "月度"], "exclude_after": ["【月間総集計】"]},
        # 本文の「入金」＝【入金内訳】の決済方法別（デポジットを除く）
        "consistency": {"receipt_metric": "receipt_excl_deposit", "receipt_components": ["payment"],
                        "exclude_methods": ["デポジット", "売掛", "相殺"]},
        "date": {"patterns": [r"^(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日\s*(?P<w>[月火水木金土日])?"], "line_limit": 2},
        "lines": [
            {"section": "売上入金", "re": r"^売上\s*" + _V + r"\s*(?:※(?P<note>.*))?$",
             "out": [{"metric": "sales_net", "group": "v"}], "expected": True,
             "note_flags": {"確認中": "手数料の扱いが確認中"}},
            {"section": "売上入金", "re": r"^入金\s*" + _V + r"\s*$", "out": [{"metric": "receipt_excl_deposit", "group": "v"}], "expected": True},
            {"section": "売上入金", "re": r"^デポジット\s*" + _V + r"\s*$", "out": [{"metric": "deposit", "group": "v"}]},
            {"section": "売上入金", "re": r"^振込\s*" + _V + r"\s*$", "out": [{"metric": "transfer", "group": "v"}]},
            {"section": "入金内訳",
             "re": r"^(?P<label>現金|クレジット|カード|Wechat\s*pay|WeChat\s*Pay|Wechat|Alipay|アリペイ|銀聯|振込|デポジット|ポイント)\s*(?P<c>\d+)\s*件\s*" + _V + r"?\s*$",
             "out": [{"metric": "payment", "group": "v", "dims_group": {"method": "label"}, "method_alias": True},
                     {"metric": "payment_count", "group": "c", "dims_group": {"method": "label"}, "method_alias": True}]},
            {"section": "入金内訳", "re": r"^売掛\s*(?P<c>\d+)\s*件\s*" + _V + r"\s*$",
             "out": [{"metric": "ar_new", "group": "v"}]},
            {"re": r"新規\s*(?P<v>\d+|-)\s*件\s*/\s*契約\s*(?P<c>\d+|-)\s*件",
             "out": [{"metric": "visitors_new", "group": "v"}, {"metric": "contracts", "group": "c"}], "expected": True},
            {"re": r"来客者合計人数\s*(?P<v>\d+|-)\s*名", "out": [{"metric": "visitors_total", "group": "v"}], "expected": True},
            {"section": "新規来院/契約数", "re": r"^(?P<label>[^・\s][^\s]*?(?:のお客様|社販))\s*(?P<v>\d+)\s*名$",
             "out": [{"metric": "visitors_segment", "group": "v", "dims_group": {"cust": "label"}}]},
            {"re": r"^【物販】\s*(?P<v>\d+|-)\s*件", "out": [{"metric": "retail_count", "group": "v"}], "expected": True},
            {"re": r"^【問合せ】\s*(?P<v>\d+|-)\s*件", "out": [{"metric": "inquiries", "group": "v"}], "expected": True},
        ],
    },
    "bio": {
        "classify": {"must": ["●売上"], "monthly": ["月次集計", "月度"]},
        # 本文の「●入金」＝各区分の決済方法別の合計（売掛を除く）
        "consistency": {"receipt_components": ["payment"], "exclude_methods": ["売掛", "デポジット"]},
        "date": {"patterns": [r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日",
                              r"^(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"], "line_limit": 2},
        "lines": [
            {"re": r"^●売上\s*" + _V + r"\s*(?:\((?P<note>.*)\))?", "out": [{"metric": "sales_incl_cpc", "group": "v"}], "expected": True},
            {"re": r"^●実売\s*" + _V + r"\s*(?:\((?P<note>.*)\)?)?", "out": [{"metric": "sales_actual", "group": "v"}], "expected": True},
            {"re": r"^●売掛\s*" + _V, "out": [{"metric": "ar_new", "group": "v"}], "expected": True},
            {"re": r"^●入金\s*" + _V, "out": [{"metric": "receipt", "group": "v"}], "expected": True},
            {"subsection": "グループ内CPC受託", "re": r"^(?P<label>.+?)\s*¥\s*(?P<v>[\d,]*)\s*/\s*(?P<c>\d*)\s*件",
             "out": [{"metric": "cpc_amount", "group": "v", "dims_group": {"category": "label"}},
                     {"metric": "cpc_count", "group": "c", "dims_group": {"category": "label"}}]},
            {"subsection": "^(一般|AIMS/ITEC|BTS|再生美容|インバウンド)$",
             "re": r"^(?P<label>クレジット|現金|振込|売掛|ポイント|銀聯|We\s*chat\s*pay|Ari\s*pay|Ali\s*pay|デポジット)\s*(?P<v>[\d,]*|-)\s*円",
             "out": [{"metric": "payment", "group": "v", "dims_group": {"method": "label"}, "dims_subsection": "market",
                      "method_alias": True}]},
            {"re": r"^【概要】\s*(?P<v>\d+|-)\s*件", "out": [{"metric": "treatment_count", "group": "v"}]},
            {"subsection": "^(一般|AIMS|インバウンド|BTS|再生美容)$",
             "re": r"^\((?P<label>TEL|メール|来院|LINE/国際部|LINE)\)\s*(?P<v>\d*)\s*件",
             "out": [{"metric": "inquiries", "group": "v", "dims_group": {"channel": "label"}, "dims_subsection": "market"}]},
        ],
    },
    "prevention": {
        # 2026/7/20以前は【売上】、8/8前後は【売上/入金】の見出し（実データで確認）
        "classify": {"must": ["【売上入金】", "【売上/入金】", "【売上】"], "monthly": ["月次集計", "月度"]},
        # 実データで確認：本文の「入金」＝決済方法別（現金・クレジット等）＋振込＋デポジット
        "consistency": {"receipt_components": ["payment", "transfer", "deposit"]},
        "date": {"patterns": [r"^(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日\s*(?P<w>[月火水木金土日])?"], "line_limit": 2},
        "lines": [
            {"section": "^売上(入金|/入金)?$", "re": r"^売上\s*\(手数料抜き\)\s*" + _V, "out": [{"metric": "sales_net", "group": "v"}], "expected": True},
            {"section": "^売上(入金|/入金)?$", "re": r"^入金\s*" + _V + r"\s*(?:\((?P<note>.*)\)?)?$", "out": [{"metric": "receipt", "group": "v"}], "expected": True},
            {"section": "^売上(入金|/入金)?$", "re": r"^売掛\s*" + _V + r"\s*$", "out": [{"metric": "ar_new", "group": "v"}]},
            # 旧書式（〜2026/7/20）：【来院】ドック○名・内覧○名 等
            {"section": "^来院$", "re": r"^(?P<label>[^\d\s¥:：]{1,10}?)\s*(?P<v>\d+|-|ー)\s*名$",
             "out": [{"metric": "visitors_segment", "group": "v", "dims_group": {"cust": "label"}}]},
            {"re": r"^【問い合わせ[】\]]\s*電話\s*(?P<a>\d+|-)\s*件\s*直来\s*(?P<b>\d+|-)\s*件",
             "out": [{"metric": "inquiries", "group": "a", "dims": {"channel": "電話"}},
                     {"metric": "inquiries", "group": "b", "dims": {"channel": "直来"}}]},
            {"section": "^入金内訳$", "re": r"^(?P<label>現金|クレジット|アリペイ|Alipay|Wechat|WeChat)\s*" + _V + r"\s*$",
             "out": [{"metric": "payment", "group": "v", "dims_group": {"method": "label"}, "method_alias": True}]},
            {"section": "^入金内訳$", "re": r"^相殺\s*" + _V + r"\s*$", "out": [{"metric": "offset", "group": "v"}]},
            {"section": "^入金内訳$", "re": r"^デポジット\s*" + _V + r"\s*$", "out": [{"metric": "deposit", "group": "v"}]},
            {"section": "^入金内訳$", "re": r"^振込\s*" + _V + r"\s*$", "out": [{"metric": "transfer", "group": "v"}]},
            {"re": r"^【新規来院】\s*(?P<v>\d+|-)\s*名", "out": [{"metric": "visitors_new", "group": "v"}], "expected": True},
            {"re": r"来院者合計人数\s*(?P<v>\d+|-)\s*名", "out": [{"metric": "visitors_total", "group": "v"}], "expected": True},
            {"section": "^新規来院$", "re": r"^(?P<label>ビジター|ゲスト|メンバー|年間パス|企業ドック)\s*(?P<v>\d+|-)\s*名",
             "out": [{"metric": "visitors_segment", "group": "v", "dims_group": {"cust": "label"}}]},
            {"re": r"^【問い合わせ[】\]]\s*(?P<v>\d+|-)\s*件", "out": [{"metric": "inquiries", "group": "v"}]},
            {"re": r"^★?【保険診療\s*来院】\s*(?P<v>\d+|-)?\s*名", "out": [{"metric": "insurance_visitors", "group": "v"}]},
            {"section": "保険診療売上", "re": r"^売上\s*" + _V + r"\s*$", "out": [{"metric": "insurance_sales", "group": "v"}]},
            {"section": "保険診療売上", "re": r"^入金\s*" + _V + r"\s*$", "out": [{"metric": "insurance_receipt", "group": "v"}]},
            {"section": "保険診療売上", "re": r"^売掛\s*" + _V + r"\s*$", "out": [{"metric": "insurance_ar", "group": "v"}]},
        ],
    },
    "osaka": {
        # 2026/7末は「売上入金】」と【が抜けた見出しがある（実データで確認）
        "classify": {"must": ["売上入金】"], "monthly": ["月次集計", "月度"], "other_clinic": ["神戸"]},
        "date": {"patterns": [r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"], "line_limit": 3},
        "lines": [
            # 大阪の本文「売上」は手数料の記載がないが、実データ（2026/9〜10）でExcelの「国内+インバウンド（手込）」と
            # 全日一致したため「売上（手数料込み）」として照合する
            {"section": "^売上入金$", "re": r"^売上\s*:?\s*" + _V + r"\s*$",
             "out": [{"metric": "sales_gross", "group": "v"}], "expected": True},
            {"section": "^売上入金$", "re": r"^入金\s*:?\s*" + _V + r"\s*$", "out": [{"metric": "receipt", "group": "v"}], "expected": True},
            {"section_not": "問合せ", "subsection": "^(インバウンド|国内)$",
             "re": r"^(?P<label>診察料|細胞治療|血液浄化|免疫治療|上清液|物販|点滴|顔治療|その他)\s*(?P<n>\d+)\s*件\s*(?:\(\s*契約\s*(?P<nc>\d+)\s*件\s*\))?\s*(?P<e>\d+)\s*件\s*(?:\(\s*契約\s*(?P<ec>\d+)\s*件\s*\))?",
             "out": [{"metric": "category_visits", "group": "n", "dims": {"cust": "新規"}, "dims_group": {"category": "label"}, "dims_subsection": "market"},
                     {"metric": "category_contracts", "group": "nc", "dims": {"cust": "新規"}, "dims_group": {"category": "label"}, "dims_subsection": "market"},
                     {"metric": "category_visits", "group": "e", "dims": {"cust": "既存"}, "dims_group": {"category": "label"}, "dims_subsection": "market"},
                     {"metric": "category_contracts", "group": "ec", "dims": {"cust": "既存"}, "dims_group": {"category": "label"}, "dims_subsection": "market"}]},
            {"section": "^問合せ$", "re": r"^合計\s*(?P<v>\d+|-)\s*件\s*\(\s*確定\s*(?P<c>\d+|-)\s*件\s*\)",
             "out": [{"metric": "inquiries", "group": "v"}, {"metric": "inquiries_confirmed", "group": "c"}], "expected": True},
            {"section": "^問合せ$", "re": r"^(?P<label>細胞治療|血液浄化|免疫治療|上清液|物販|点滴|顔治療|その他)\s*(?P<a>\d+)\s*件\s*(?P<b>\d+)\s*件",
             "out": [{"metric": "inquiries", "group": "a", "dims": {"market": "インバウンド"}, "dims_group": {"category": "label"}},
                     {"metric": "inquiries", "group": "b", "dims": {"market": "国内"}, "dims_group": {"category": "label"}}]},
        ],
    },
}

# ---------------------------------------------------------------------------
# Excel の読み取りルール
#   見出し行：A列が「月日」「日付」の行（=header）。その1〜3行上を上位見出しとする。
#   cells: 指標→列。rows: {相対行: [含むべき文字…]}（-0=見出し行, -1=1行上, -2=2行上）
#   occurrence: 同じ見出しが複数あるとき何番目か
#   category: 上位見出し（グループ）×下位見出し（カテゴリ）×計測（売上/件数）から自動で列を作る
#   closed_col: 休診の記入列
# ---------------------------------------------------------------------------
def _c(metric, rows, occurrence=1, required=False, dims=None, confirmed=True):
    return {"metric": metric, "rows": rows, "occurrence": occurrence, "required": required,
            "dims": dims or {}, "definition_confirmed": confirmed}


_COMMON_PAY = [
    _c("payment", {"-1": ["現金"]}, dims={"method": "現金"}),
    _c("payment", {"-1": ["クレジット", "計"]}, dims={"method": "クレジット"}),
    _c("transfer", {"-1": ["振込"]}),
    _c("deposit", {"-1": ["デポジット"]}),
    _c("offset", {"-1": ["相殺"]}),
    _c("ar_new", {"-1": ["売掛"]}),
]

EXCEL_RULES: dict[str, dict] = {
    "9ru": {
        "file_name": "日報",
        "sheet_name": r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月",
        "skip_sheets": ["format", "forrmat", "入金予定"],
        "header_labels": ["月日", "日付"],
        "cells": [
            _c("sales_gross", {"0": ["売上計", "手数料込"]}, required=True),
            _c("sales_net", {"0": ["売上計", "手数料抜"]}, required=True),
            _c("receipt", {"0": ["入金計"]}, required=True),
            _c("payment", {"0": ["WechatPay"]}, dims={"method": "WeChatPay"}),
            _c("payment", {"0": ["Alipay"]}, dims={"method": "Alipay"}),
            *_COMMON_PAY,
        ],
        "category": [
            {"group_row": "-2", "cat_row": "-1", "measure_row": "0",
             "groups": [{"label": "国内売上", "exact": True, "market": "国内"},
                        {"label": "インバウンド売上", "exact": True, "market": "インバウンド"}],
             "measures": {"売上": "category_sales", "件数": "category_count"},
             "exclude_cat": ["合計", "計"]},
        ],
        "closed_col": None,
    },
    "bio": {
        "file_name": "日報",
        "sheet_name": r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月",
        "skip_sheets": ["format", "入金予定"],
        "header_labels": ["月日", "日付"],
        "cells": [
            _c("sales_gross", {"0": ["売上計", "手数料込"]}, required=True),
            _c("sales_net", {"0": ["売上合計", "手数料抜"]}, required=True),
            _c("receipt", {"0": ["入金合計"]}, required=True),
            *_COMMON_PAY,
        ],
        "category": [
            {"group_row": "-2", "cat_row": "-1", "measure_row": "0",
             "groups": [{"label": "売上(国内)", "market": "国内"}, {"label": "売上(インバウンド)", "market": "インバウンド"}],
             "measures": {"売上": "category_sales", "件数": "category_count"}, "exclude_cat": ["合計"]},
            {"group_row": "-2", "cat_row": "-1", "measure_row": "0",
             "groups": [{"label": "CPC受託", "market": ""}],
             "measures": {"売上": "cpc_amount", "件数": "cpc_count"}, "exclude_cat": ["合計"]},
        ],
        "closed_col": {"0": ["休診"]},
    },
    "prevention": {
        "file_name": "日報",
        "sheet_name": r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月",
        "skip_sheets": ["format", "入金予定"],
        "header_labels": ["月日", "日付"],
        "cells": [
            _c("sales_gross", {"0": ["国内+インバウンド", "手込"]}, required=True),
            _c("sales_net", {"0": ["国内+インバウンド", "手数料抜"]}, required=True),
            _c("receipt", {"0": ["入金合計"]}, required=True),
            *_COMMON_PAY,
        ],
        "category": [
            {"group_row": "-2", "cat_row": "-1", "measure_row": "0",
             "groups": [{"label": "売上人間ドック", "market": "国内", "occurrence": 1},
                        {"label": "売上オプション", "market": "国内", "occurrence": 1},
                        {"label": "売上人間ドック", "market": "インバウンド", "occurrence": 2},
                        {"label": "売上オプション", "market": "インバウンド", "occurrence": 2}],
             "measures": {"売上": "category_sales", "件数": "category_count"}, "exclude_cat": ["合計"]},
        ],
        "closed_col": None,
    },
    "osaka": {
        "file_name": "日報",
        "sheet_name": r"(?P<y>\d{4})\s*[.年]\s*(?P<m>\d{1,2})",
        "skip_sheets": ["format"],
        "header_labels": ["月日", "日付"],
        "cells": [
            _c("sales_gross", {"0": ["国内+インバウンド", "手込"]}, required=True),
            _c("sales_net", {"0": ["国内+インバウンド", "手数料抜"]}, required=True),
            _c("receipt", {"0": ["入金合計"]}, required=True),
            *_COMMON_PAY,
            _c("visitors_new", {"0": ["新規来院数"]}, occurrence=1, dims={"market": "国内"}),
            _c("contracts", {"0": ["新規成約数"]}, occurrence=1, dims={"market": "国内"}),
            _c("visitors_existing", {"0": ["既存人数"]}, occurrence=1, dims={"market": "国内"}),
            _c("visitors_new", {"0": ["新規来院数"]}, occurrence=2, dims={"market": "インバウンド"}),
            _c("contracts", {"0": ["新規成約数"]}, occurrence=2, dims={"market": "インバウンド"}),
            _c("visitors_existing", {"0": ["既存人数"]}, occurrence=2, dims={"market": "インバウンド"}),
        ],
        "category": [
            {"group_row": "-1", "cat_row": "0", "measure_row": None,
             "groups": [{"label": "再生医療売上内訳", "market": "国内", "occurrence": 1},
                        {"label": "美容医療売上内訳", "market": "国内", "occurrence": 1},
                        {"label": "再生医療売上内訳", "market": "インバウンド", "occurrence": 2},
                        {"label": "美容医療売上内訳", "market": "インバウンド", "occurrence": 2}],
             "measures": {}, "default_metric": "category_sales", "exclude_cat": ["合計", "新規", "既存"]},
        ],
        "closed_col": {"0": ["休診日"]},
    },
}

# 日次の値として読む行の範囲外（月合計・予測・目標）。照合用にだけ使う
EXCEL_SUMMARY_LABELS = ["予定日数", "診察経過日数", "着地予測", "目標売上", "合計", "累計"]
