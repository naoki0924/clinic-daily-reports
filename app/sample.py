"""サンプルデータ（架空）。実データの書式（2026-10-19確認）に合わせて作る。

サンプルで動かした結果は画面に「サンプル」と表示し、実データの検証結果と区別する。
患者名・スタッフ名は含めない。
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

from openpyxl import Workbook

_W = "月火水木金土日"


def _month_days(y: int, m: int) -> list[date]:
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += timedelta(days=1)
    return out


def _sheet_header(ws, title: str, y: int, m: int, groups: dict, labels4: dict, labels5: dict, header_label="月日"):
    ws["A1"] = title
    ws["A2"] = f"{y}年{m}月"
    for cell, v in groups.items():
        ws[cell] = v
    for cell, v in labels4.items():
        ws[cell] = v
    ws["A5"], ws["B5"] = header_label, "曜日"
    for cell, v in labels5.items():
        ws[cell] = v


def workbook_9ru(values: dict[date, dict], months: list[tuple[int, int]], formula_no_cache: date | None = None) -> bytes:
    """9ru形式。values[日付] = {gross, net, cash, credit, transfer, deposit, cat_sales, cat_count}"""
    wb = Workbook()
    wb.remove(wb.active)
    for (y, m) in months:
        ws = wb.create_sheet(f"{y}年{m}月")
        _sheet_header(ws, "CLINIC 9ru", y, m,
                      {"Q3": "クリニック売上", "T3": "入金", "AH3": "国内売上", "BJ3": "国内売上計", "BM3": "インバウンド売上", "CJ3": "インバウンド計"},
                      {"T4": "現金", "U4": "クレジット\nアプリ決済　計", "AC4": "振込", "AD4": "デポジット\n金券等", "AE4": "相殺",
                       "AF4": "売掛", "AH4": "美肌治療", "AJ4": "脱毛", "BM4": "美肌治療"},
                      {"Q5": "売上計\n※手数料込", "R5": "売上計\n※手数料抜", "S5": "入金計", "Z5": "フィンテック\nWechatPay",
                       "AH5": "売上", "AI5": "件数", "AJ5": "売上", "AK5": "件数", "BM5": "売上", "BN5": "件数"})
        for i, d in enumerate(_month_days(y, m)):
            r = 6 + i
            ws.cell(row=r, column=1, value=datetime(d.year, d.month, d.day))
            ws.cell(row=r, column=2, value=_W[d.weekday()])
            v = values.get(d)
            if not v:
                continue
            ws[f"T{r}"] = v.get("cash")
            ws[f"U{r}"] = v.get("credit")
            ws[f"AC{r}"] = v.get("transfer")
            ws[f"AD{r}"] = v.get("deposit")
            ws[f"AH{r}"] = v.get("cat_sales")
            ws[f"AI{r}"] = v.get("cat_count")
            ws[f"Q{r}"] = v["gross"]
            ws[f"R{r}"] = v["net"]
            ws[f"S{r}"] = v["receipt"]
        ws["A37"] = "予定日数"
    data = io.BytesIO()
    wb.save(data)
    raw = data.getvalue()
    if formula_no_cache:
        raw = _inject_uncached_formula(raw, months, formula_no_cache)
    return raw


def _inject_uncached_formula(raw: bytes, months, d: date) -> bytes:
    """openpyxl は計算結果を保存しないため、数式セル（結果なし）を作って検知を試せる。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw))
    ws = wb[f"{d.year}年{d.month}月"]
    r = 6 + d.day - 1
    ws[f"S{r}"] = f"=T{r}+U{r}+AC{r}"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def body_9ru(d: date, net: int, receipt: int, deposit: int | None = None, credit: int | None = None,
             visitors: int = 3, new: int = 1, contracts: int = 0, segments: dict | None = None,
             note: str = "※手数料抜き") -> str:
    seg = segments or {"年パスのお客様": visitors - new, "インバウンド新規のお客様": new}
    lines = [f"{d.month}月{d.day}日　{_W[d.weekday()]}曜日　担当医", "", "【売上入金】", f"売上　￥{net:,}　　　{note}",
             f"入金　￥{receipt:,}"]
    if deposit is not None:
        lines.append(f"デポジット　￥{deposit:,}")
    lines += ["", "【入金内訳】", f"クレジット　　1件　￥{credit if credit is not None else receipt:,}", "",
              f"【新規来院/契約数】新規{new}件/契約{contracts}件　", f"・来客者合計人数 　　　 　　　　　　　{visitors}名"]
    lines += [f"　　{k}　　　　　　{v}名" for k, v in seg.items()]
    lines += ["", "・年パスのお客様", "施術A×1　　　　　￥10,000（クレジット）", "", "【物販】0件", "", "【問合せ】0件", "", "記入　担当"]
    return "\n".join(lines)


def body_prevention(d: date, net: int | None, receipt: int, weekday_override: str | None = None,
                    day_override: int | None = None, visitors: int = 3, new: int = 1) -> str:
    w = weekday_override or _W[d.weekday()]
    dd = day_override or d.day
    return "\n".join([
        f"{d.month}月{dd}日  {w}曜日　担当医", "", "【売上入金】",
        f"　売上（手数料抜き）￥{net:,}" if net is not None else "　売上（手数料抜き）￥-",
        f"　入金　￥{receipt:,}", "　売掛　￥-", "", "【入金内訳】　", "　現金　￥-", f"　クレジット　￥{receipt:,}",
        "　相殺　¥-", "　デポジット　￥-", "", f"【新規来院】 　{new}名", f"　来院者合計人数　　　　　　  {visitors}名",
        f"　　ビジター　　　　　　　　  {new}名", "　　ゲスト　　　　　　　　　   -名", f"　　メンバー　　　　　　　　  {visitors - new}名",
        "", "【問い合わせ]　　-件", "　★【保険診療　来院】　-名"])


def body_osaka(d: date | None, sales: int, receipt: int) -> str:
    head = ["BSC大阪"] + ([f"{d.year}年{d.month}月{d.day}日"] if d else [])
    return "\n".join(head + ["", "【売上入金】", f"売上：￥{sales:,}", f"入金：￥{receipt:,}", "", "＜インバウンド＞",
                             "　　　　　　　【新規来院/契約数】　【既存来院/契約数】　",
                             "細胞治療　　　　1件（契約　1件）　　0件（契約　0件）", "", "＜国内＞",
                             "　　　　　　　【新規来院/契約数】　【既存来院/契約数】　",
                             "点滴　　　　　　0件（契約　0件）　　2件（契約　0件）", "", "【問合せ】", "合計　1件（確定　0件）",
                             "　　　　　　　＜インバウンド＞　　＜国内＞", "細胞治療　　　　1件　　　　　　　　0件", "", "担当"])


def body_bio(sales_cpc: int, actual: int, ar: int, receipt: int) -> str:
    return "\n".join([f"●売上￥{sales_cpc:,}(CPC受託+手数料込)", f"●実売￥{actual:,}(手数料差引+グループ内CPC受託を除く）",
                      f"●売掛￥{ar:,}", f"●入金￥{receipt:,}", "", "〈グループ内CPC受託売掛〉", "NK ￥171,600/1件",
                      "MSC採取+投与￥/件", "", "〈一般〉", "クレジット円", f"現金 {receipt:,}円", "振込 円", f"売掛 {ar:,}円", "",
                      "【概要】2件", "・保険診療(一般)　×2件", "", "＜一般＞", "（TEL)件()", "（メール)1件(予約1)", "（来院）2件(予約2)"])
