"""Web画面（FastAPI + Jinja2）。

権限：admin（管理者）／viewer_all（全院閲覧者）／viewer_clinic（院別閲覧者）
- すべての画面にログインが必要。POST は CSRF トークンを検証する
- 操作は監査ログ（audit_log）に残す
"""
from __future__ import annotations

import csv
from collections import defaultdict
import io
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .. import aggregate as ag
from ..bootstrap import active_rule, get_setting, put_setting, save_rule, seed
from ..config import JST, settings
from ..db import SessionLocal
from ..jobs import enqueue, load_credentials
from ..models import (
    DIM_COLS, Aggregate, AppUser, AuditLog, Clinic, DailyFact, DayStatus, Issue, JobRun, JobStep, ManualAdjustment, MetricDef,
    Notification, Observation, ParseRule, Report, SourceComment, SourceFile, SourcePost, Target,
)
from ..pipeline import reconcile_day, refresh_days
from ..backup import cleanup_backups, storage_usage
from ..security import compact_vault, decrypt_text, get_secret, hash_password, mask, put_secret, verify_password
from . import queries as Q

log = logging.getLogger("web")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed()
    s0 = SessionLocal()
    try:
        setup_token(s0)  # 利用者がいなければトークンを起動ログに出す
    finally:
        s0.close()
    # 旧形式の原本を圧縮形式に置き換え、途中で止まったバックアップを片付ける（起動を待たせないよう別スレッド）
    threading.Thread(target=_maintenance, name="maintenance", daemon=True).start()
    if settings.run_scheduler:
        from .. import scheduler
        scheduler.start()
    yield
    if settings.run_scheduler:
        from .. import scheduler
        scheduler.shutdown()


def _maintenance() -> None:
    try:
        cleanup_backups(7)
        r = compact_vault()
        if r["converted"]:
            logging.getLogger("maintenance").info("原本を圧縮形式に変換: %s", r)
    except Exception:  # noqa: BLE001
        logging.getLogger("maintenance").exception("maintenance failed")


app = FastAPI(title="4院 日報集計", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key or "unset", session_cookie="clinic_session",
                   max_age=60 * 60 * 12, same_site="lax", https_only=False)


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def get_db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


class Login(Exception):
    pass


@app.exception_handler(Login)
async def _to_login(request: Request, exc: Login):
    return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)


def instance_id(s) -> str:
    """DBごとの識別子。別のDB（サンプル等）で発行されたログイン状態を受け付けないために使う。"""
    v = get_setting(s, "instance").get("id")
    if not v:
        v = secrets.token_hex(8)
        put_setting(s, "instance", {"id": v})
        s.flush()
    return v


def current_user(request: Request, s=Depends(get_db)) -> AppUser:
    uid = request.session.get("uid")
    if not uid or request.session.get("iid") != instance_id(s):
        raise Login()
    u = s.get(AppUser, uid)
    if not u or not u.active:
        request.session.clear()
        raise Login()
    return u


ADMIN_PATHS = ("/ops", "/settings", "/raw", "/adjust")  # 管理者だけが開ける画面


def require_admin(u: AppUser = Depends(current_user)) -> AppUser:
    if u.role != "admin":
        raise HTTPException(403, "管理者のみ操作できます")
    return u


def _after_login_path(next_path: str, u: AppUser) -> str:
    """ログイン後の移動先。外部URLは受け付けず、管理者以外が管理者専用画面を指定していればダッシュボードへ。"""
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    if u.role != "admin" and next_path.startswith(ADMIN_PATHS):
        return "/"
    return next_path


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    """画面の操作で起きた権限なし・見つからないは、JSONではなく案内つきの画面で返す。"""
    if request.method != "GET" or "text/html" not in request.headers.get("accept", "") or exc.status_code not in (403, 404):
        return await http_exception_handler(request, exc)
    s = SessionLocal()
    try:
        uid = request.session.get("uid")
        u = s.get(AppUser, uid) if uid and request.session.get("iid") == instance_id(s) else None
        msg = {403: "この画面を開く権限がありません。", 404: "ページが見つかりません。"}[exc.status_code]
        if exc.status_code == 403 and exc.detail == "管理者のみ操作できます":
            msg = "この画面は管理者のみ開けます。"
        resp = render(request, "error.html", {"message": msg, "status": exc.status_code}, s, u)
        resp.status_code = exc.status_code
        return resp
    finally:
        s.close()


def allowed_codes(s, u: AppUser) -> list[str]:
    codes = [c.code for c in Q.clinics(s)]
    if u.role == "viewer_clinic":
        return [c for c in codes if c in (u.clinic_codes or [])]
    return codes


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str) -> None:
    if not token or token != request.session.get("csrf"):
        raise HTTPException(400, "画面の有効期限が切れました。再読み込みしてください（CSRF）")


def audit(s, request: Request, user: str, action: str, target: str = "", detail: dict | None = None) -> None:
    s.add(AuditLog(user=user, action=action, target=target[:255], detail=detail or {},
                   ip=request.client.host if request.client else ""))


def render(request: Request, name: str, ctx: dict, s=None, user: AppUser | None = None):
    base = {"request": request, "csrf": csrf_token(request), "user": user, "today": ag.today_jst(),
            "STATUS_LABEL": Q.STATUS_LABEL, "STATE_LABEL": Q.STATE_LABEL, "FACT_STATUS": Q.FACT_STATUS,
            "is_sample": settings.data_dir.name == "sample" or (s is not None and _is_sample(s))}
    if s is not None:
        base["metrics"] = Q.metric_defs(s)
        base["open_issue_count"] = s.scalar(select(func.count()).select_from(Issue).where(
            Issue.status == "open", Issue.severity != "info")) or 0
        base["auth_state"] = get_setting(s, "auth_state")
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


def _is_sample(s) -> bool:
    return bool(get_setting(s, "data_mode").get("sample"))


def _d(v: str | None, default: date) -> date:
    try:
        return date.fromisoformat(v) if v else default
    except ValueError:
        return default


def _ym(v: str | None, default: date) -> tuple[int, int]:
    try:
        y, m = (v or "").split("-")
        return int(y), int(m)
    except ValueError:
        return default.year, default.month


def fmt(v, unit: str = "yen") -> str:
    if v is None:
        return "—"
    if unit == "yen":
        return f"{v:,.0f}"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def fact_text(f, unit="yen") -> str:
    if f is None:
        return "—"
    if f.value is None:
        return Q.STATE_LABEL.get(f.state, "—")
    return fmt(f.value, unit)


templates.env.filters["fmt"] = fmt
templates.env.filters["man"] = lambda v: "—" if v is None else (f"{v / 100000000:,.2f}億" if abs(v) >= 100000000 else f"{v / 10000:,.0f}万")
templates.env.globals["fact_text"] = fact_text
templates.env.globals["compare"] = Q.compare
templates.env.globals["DIM_COLS"] = DIM_COLS
templates.env.globals["timedelta"] = timedelta
templates.env.filters["jst"] = lambda dt: (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") if dt else "—"
templates.env.filters["dt"] = lambda dt: dt.strftime("%Y-%m-%d %H:%M") if dt else "—"
templates.env.filters["wd"] = lambda d: "月火水木金土日"[d.weekday()]
templates.env.filters["tojson_ja"] = lambda o: json.dumps(o, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# ログイン
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---- 初回セットアップ（利用者が1人もいないときだけ） ----------------------------
_SETUP_TOKEN: str | None = None


def setup_token(s) -> str | None:
    """利用者がいない間だけ有効な一回限りのトークン。起動ログに出力し、それを知る人だけが管理者を作れる。"""
    global _SETUP_TOKEN
    if s.scalar(select(func.count()).select_from(AppUser)):
        _SETUP_TOKEN = None
        return None
    if _SETUP_TOKEN is None:
        _SETUP_TOKEN = secrets.token_hex(6)
        log.warning("初回セットアップ用トークン: %s （/setup で管理者を作成してください）", _SETUP_TOKEN)
        print(f"[SETUP] 初回セットアップ用トークン: {_SETUP_TOKEN}  （/setup で管理者を作成）", flush=True)
    return _SETUP_TOKEN


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, s=Depends(get_db)):
    if not setup_token(s):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", {"error": ""})


@app.post("/setup")
def setup(request: Request, token: str = Form(...), username: str = Form(...), password: str = Form(...),
          csrf: str = Form(""), s=Depends(get_db)):
    check_csrf(request, csrf)
    tok = setup_token(s)
    if not tok:
        return RedirectResponse("/login", status_code=303)
    if not secrets.compare_digest(token.strip(), tok):
        return render(request, "setup.html", {"error": "セットアップ用トークンが違います（サーバーの起動ログを確認）"})
    if len(password) < 10:
        return render(request, "setup.html", {"error": "パスワードは10文字以上にしてください"})
    s.add(AppUser(username=username.strip(), password_hash=hash_password(password), role="admin"))
    audit(s, request, username.strip(), "setup_admin")
    s.flush()
    setup_token(s)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", s=Depends(get_db)):
    if setup_token(s):
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", {"next": next, "error": ""})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          csrf: str = Form(""), s=Depends(get_db)):
    check_csrf(request, csrf)
    u = s.scalar(select(AppUser).where(AppUser.username == username, AppUser.active.is_(True)))
    if not u or not verify_password(password, u.password_hash):
        audit(s, request, username, "login_failed")
        return render(request, "login.html", {"next": next, "error": "ユーザー名またはパスワードが違います"})
    request.session.clear()
    request.session["uid"] = u.id
    request.session["iid"] = instance_id(s)
    u.last_login = datetime.utcnow()
    audit(s, request, u.username, "login")
    return RedirectResponse(_after_login_path(next, u), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# ダッシュボード・詳細・レポート
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s=Depends(get_db), u=Depends(current_user)):
    codes = allowed_codes(s, u)
    data = Q.dashboard(s, codes, ag.today_jst())
    from ..issues import KIND_LABELS
    return render(request, "dashboard.html", {"d": data, "clinics": Q.clinics(s, codes), "KIND": KIND_LABELS}, s, u)


CLINIC_KPIS = ["sales_net", "sales_gross", "sales_incl_cpc", "sales_actual", "receipt", "receipt_excl_deposit",
               "visitors_total", "visitors_new", "contracts", "inquiries"]


@app.get("/clinic/{code}", response_class=HTMLResponse)
def clinic_detail(request: Request, code: str, view: str = "month", month: str | None = None, day: str | None = None,
                  year: int | None = None, s=Depends(get_db), u=Depends(current_user)):
    """クリニック別ダッシュボード（日次・月次・年次を切り替え）。"""
    from .. import menu as M

    if code not in allowed_codes(s, u):
        raise HTTPException(403)
    today = ag.today_jst()
    yesterday = today - timedelta(days=1)
    clinic = s.scalar(select(Clinic).where(Clinic.code == code))
    defs = Q.metric_defs(s)
    have = set(s.scalars(select(func.distinct(DailyFact.metric)).where(DailyFact.clinic_code == code)))
    kpis = [k for k in CLINIC_KPIS if k in have]
    ctx = dict(clinic=clinic, view=view, kpis=kpis)

    if view == "day":
        d = _d(day, yesterday)
        f = Q.facts_for(s, [code], d - timedelta(days=28), d, kpis)
        rows = []
        for k in kpis:
            cur, prev, wk = f.get((code, d, k)), f.get((code, d - timedelta(days=1), k)), f.get((code, d - timedelta(days=7), k))
            base = [f.get((code, d - timedelta(days=7 * i), k)) for i in range(1, 5)]
            base = [b.value for b in base if b and b.value is not None and b.status != "review"]
            rows.append(dict(metric=k, fact=cur, prev=prev.value if prev else None, wk=wk.value if wk else None,
                             avg4=(sum(base) / len(base)) if len(base) >= 3 else None))
        breakdown = defaultdict(list)
        for fa in s.scalars(select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.target_date == d,
                                                    DailyFact.metric.in_(("payment", "visitors_segment", "inquiries",
                                                                          "category_visits", "category_contracts")))):
            if any(getattr(fa, c) for c in DIM_COLS):
                breakdown[fa.metric].append(fa)
        menus = M.menu_table(s, code, d, d)
        st = Q.statuses(s, [code], d, d).get((code, d))
        rep = Q.reports_list(s, [code], d, d)
        ms = d.replace(day=1)
        breakdown = {k: sorted(v, key=lambda f: -(f.value if f.value is not None else -1)) for k, v in breakdown.items()}
        ctx.update(d=d, rows=rows, breakdown=breakdown, menus=menus, st=st, rep=rep,
                   mtd=ag.total(s, [code], "sales_net", ms, d, today=today),
                   target=ag.target_value(s, code, d.year, d.month, "sales_net"))
        return render(request, "clinic.html", ctx, s, u)

    if view == "year":
        y = year or yesterday.year
        periods = M.months_of(y)
        cur = {k: Q.agg_rows(s, "M", periods, [code], k) for k in kpis}
        last = {k: Q.agg_rows(s, "M", M.months_of(y - 1), [code], k) for k in kpis}
        cmp_end, running = ag.elapsed_end(date(y, 1, 1), date(y, 12, 31), today)
        ytd = {k: ag.total(s, [code], k, date(y, 1, 1), cmp_end, today=today) for k in kpis if defs[k].kind != "balance"}
        lytd = {k: ag.total(s, [code], k, date(y - 1, 1, 1), ag.shift_year(cmp_end, -1), today=today) for k in ytd}
        menus = M.menu_table(s, code, date(y, 1, 1), cmp_end, prev=(date(y - 1, 1, 1), ag.shift_year(cmp_end, -1)))
        top = menus[:8]
        sales_key = "sales_net" if "sales_net" in cur else (kpis[0] if kpis else None)
        chart = {"labels": [f"{i}月" for i in range(1, 13)],
                 "sales": [cur[sales_key].get((code, p)).value if sales_key and cur[sales_key].get((code, p)) else None
                           for p in periods],
                 "menus": {f"{r.market} {r.name}".strip(): [r.monthly.get(p) for p in periods] for r in top}}
        status_by_month = {}
        for p in periods:
            yy, mm = map(int, p.split("-"))
            a, b = ag.month_range(yy, mm)
            cnt = defaultdict(int)
            if a <= yesterday:
                for x in Q.statuses(s, [code], a, min(b, yesterday)).values():
                    cnt[x.status] += 1
            status_by_month[p] = dict(cnt)
        mshare = M.market_share(s, [code], date(y, 1, 1), cmp_end).get(code, {})
        ctx.update(mshare=mshare, y=y, periods=periods, cur=cur, last=last, ytd=ytd, lytd=lytd, running=running, cmp_end=cmp_end,
                   menus=menus, chart=chart, status_by_month=status_by_month)
        return render(request, "clinic.html", ctx, s, u)

    # 月次
    y, m = _ym(month, yesterday)
    start, end = ag.month_range(y, m)
    shown = [k for k in defs if k in have and not k.startswith(("category_", "payment", "visitors_segment", "cpc_"))]
    facts = {}
    for fa in s.scalars(select(DailyFact).where(DailyFact.clinic_code == code, DailyFact.target_date >= start,
                                                DailyFact.target_date <= end, DailyFact.market == "", DailyFact.cust == "",
                                                DailyFact.category == "", DailyFact.channel == "", DailyFact.method == "")):
        facts[(fa.target_date, fa.metric)] = fa
    st = Q.statuses(s, [code], start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    totals = {k: ag.total(s, [code], k, start, end, today=today) for k in shown if defs[k].kind != "balance"}
    rates = {"unit_price": Q.paired_rate(s, code, "sales_net", "visitors_total", start, end),
             "new_contract": Q.paired_rate(s, code, "contracts", "visitors_new", start, end)}
    ps, pe_, prev, label, running = M.period_range("month", y, m, today)
    menus = M.menu_table(s, code, start, pe_, prev=prev)
    reports = {r.target_date: (r, p) for r, p in Q.reports_list(s, [code], start, end)}
    chart = {"labels": [d.strftime("%m/%d") for d in days],
             "sales": [facts.get((d, "sales_net")).value if facts.get((d, "sales_net")) else None for d in days],
             "receipt": [facts.get((d, "receipt")).value if facts.get((d, "receipt")) else None for d in days]}
    mshare = M.market_share(s, [code], start, pe_).get(code, {})
    ctx.update(mshare=mshare, y=y, m=m, days=days, facts=facts, st=st, shown=shown, totals=totals, rates=rates, menus=menus,
               reports=reports, chart=chart, target=ag.target_value(s, code, y, m, "sales_net"), running=running)
    return render(request, "clinic.html", ctx, s, u)


@app.get("/menu", response_class=HTMLResponse)
def menu_analysis(request: Request, clinic: str = "", view: str = "month", month: str | None = None,
                  year: int | None = None, market: str = "", s=Depends(get_db), u=Depends(current_user)):
    """メニュー（施術カテゴリ）分析。院ごとに区分が違うため、院をまたいで合算しない。"""
    from .. import menu as M

    today = ag.today_jst()
    yesterday = today - timedelta(days=1)
    codes = allowed_codes(s, u)
    clinic = clinic if clinic in codes else (codes[0] if codes else "")
    if view == "year":
        y, m = (year or yesterday.year), 1
    else:
        view = "month"
        y, m = _ym(month, yesterday)
    start, end, prev, label, running = M.period_range(view, y, m, today)
    prev_label = "前年同期間" if view == "year" else ("前月同期間" if running else "前月")
    rows = M.menu_table(s, clinic, start, end, market=market, prev=prev)
    total = sum(r.sales for r in rows)
    ty = start.year
    months = M.months_of(ty)
    trend = M.menu_table(s, clinic, date(ty, 1, 1), min(date(ty, 12, 31), yesterday), market=market)
    cpc = M.menu_table(s, clinic, start, end, sales_metric="cpc_amount", count_metric="cpc_count", excel_only=True,
                       prev=prev) if clinic == "bio" else []
    vc = M.visits_contracts(s, clinic, start, end)
    markets = sorted({r.market for r in M.menu_table(s, clinic, start, end) if r.market})
    return render(request, "menu.html", dict(
        clinics=Q.clinics(s, codes), clinic=clinic, view=view, y=y, m=m, label=label, prev=prev, prev_label=prev_label,
        rows=rows, total=total, split=M.market_split(rows), insights=M.insights(rows, prev_label), cpc=cpc, vc=vc,
        market=market, markets=markets, months=months, trend=trend[:10], running=running, end=end), s, u)


@app.get("/market", response_class=HTMLResponse)
def market_view(request: Request, view: str = "year", month: str | None = None, year: int | None = None,
                s=Depends(get_db), u=Depends(current_user)):
    """国内／インバウンド別の構成（円グラフ）。全院と各院。"""
    from .. import menu as M

    today = ag.today_jst()
    yesterday = today - timedelta(days=1)
    codes = allowed_codes(s, u)
    if view == "month":
        y, m = _ym(month, yesterday)
    else:
        view, y, m = "year", (year or yesterday.year), 1
    start, end, prev, label, running = M.period_range(view, y, m, today)
    share = M.market_share(s, codes, start, end)
    prev_share = M.market_share(s, codes, prev[0], prev[1])
    monthly = M.market_monthly(s, codes, start.year)
    monthly_by = {c: M.market_monthly(s, [c], start.year) for c in codes}
    return render(request, "market.html", dict(
        pies=[("ALL", "全院合計")] + [(c.code, c.name) for c in Q.clinics(s, codes)],
        clinics=Q.clinics(s, codes), view=view, y=y, m=m, label=label, prev=prev, running=running,
        share=share, prev_share=prev_share, months=M.months_of(start.year), monthly=monthly, monthly_by=monthly_by,
        review=M.review_days(s, codes, start, end), order=M.MARKET_ORDER), s, u)


def _filters(request: Request):
    qp = request.query_params
    return {c: qp.get(c, "") for c in DIM_COLS if qp.get(c)}


@app.get("/reports/daily", response_class=HTMLResponse)
def report_daily(request: Request, day: str | None = None, s=Depends(get_db), u=Depends(current_user)):
    d = _d(day, ag.today_jst() - timedelta(days=1))
    codes = allowed_codes(s, u)
    metrics = Q.KEY_METRICS
    f = Q.facts_for(s, codes, d - timedelta(days=7), d, metrics)
    st = Q.statuses(s, codes, d, d)
    open_issues = s.scalars(select(Issue).where(Issue.status == "open", Issue.target_date == d,
                                                Issue.clinic_code.in_(codes))).all()
    all_rows = {}
    defs = Q.metric_defs(s)
    for k in metrics:
        if defs[k].cross_clinic_sum:
            all_rows[k] = {"cur": ag.total(s, codes, k, d, d), "prev": ag.total(s, codes, k, d - timedelta(days=1), d - timedelta(days=1)),
                           "wk": ag.total(s, codes, k, d - timedelta(days=7), d - timedelta(days=7))}
    return render(request, "report_daily.html", dict(d=d, codes=codes, clinics=Q.clinics(s, codes), metric_list=metrics,
                                                     f=f, st=st, issues=open_issues, all_rows=all_rows, KIND=__import__("app.issues", fromlist=["x"]).KIND_LABELS), s, u)


@app.get("/reports/monthly", response_class=HTMLResponse)
def report_monthly(request: Request, month: str | None = None, metric: str = "sales_net", clinic: str = "",
                   s=Depends(get_db), u=Depends(current_user)):
    today = ag.today_jst()
    y, m = _ym(month, today - timedelta(days=1))
    codes = allowed_codes(s, u)
    if clinic:
        codes = [c for c in codes if c == clinic]
    dims = _filters(request)
    tbl = Q.month_table(s, codes, y, m, metric, dims)
    start, end = tbl["start"], tbl["end"]
    cmp_end, in_progress = ag.elapsed_end(start, end, today)
    elapsed = (cmp_end - start).days
    py, pm = ag.prev_month(y, m)
    p_start, p_end_full = ag.month_range(py, pm)
    p_end = min(p_end_full, p_start + timedelta(days=elapsed)) if in_progress else p_end_full
    ly_start, ly_end = ag.shift_year(start, -1), ag.shift_year(cmp_end, -1)
    defs = Q.metric_defs(s)
    rows = []
    for c in Q.clinics(s, codes):
        cur = ag.total(s, [c.code], metric, start, cmp_end, dims, today=today)
        full = ag.total(s, [c.code], metric, start, end, dims, today=today)
        prev = ag.total(s, [c.code], metric, p_start, p_end, dims, today=today)
        ly = ag.total(s, [c.code], metric, ly_start, ly_end, dims, today=today)
        tgt = ag.target_value(s, c.code, y, m, metric)
        rows.append(dict(clinic=c, cur=cur, full=full, prev=prev, ly=ly, target=tgt,
                         ly_cov=ag.coverage(s, [c.code], ly_start, ly_end),
                         p_cov=ag.coverage(s, [c.code], p_start, p_end)))
    all_row = None
    if defs[metric].cross_clinic_sum and not dims and len(codes) > 1:
        all_row = dict(cur=ag.total(s, codes, metric, start, cmp_end, today=today),
                       full=ag.total(s, codes, metric, start, end, today=today),
                       prev=ag.total(s, codes, metric, p_start, p_end, today=today),
                       ly=ag.total(s, codes, metric, ly_start, ly_end, today=today),
                       target=ag.target_value(s, "ALL", y, m, metric),
                       ly_cov=ag.coverage(s, codes, ly_start, ly_end),
                       p_cov=ag.coverage(s, codes, p_start, p_end))
    cats = Q.category_breakdown(s, codes, start, end)
    chart = {"labels": [d.strftime("%m/%d") for d in tbl["days"]],
             "series": {c.name: [tbl["facts"].get((c.code, d, metric)).value if tbl["facts"].get((c.code, d, metric)) else None
                                 for d in tbl["days"]] for c in Q.clinics(s, codes)}}
    return render(request, "report_monthly.html", dict(y=y, m=m, metric=metric, tbl=tbl, rows=rows, all_row=all_row,
                                                       codes=codes, clinics=Q.clinics(s, allowed_codes(s, u)), clinic=clinic,
                                                       in_progress=in_progress, cmp_end=cmp_end, p_range=(p_start, p_end),
                                                       ly_range=(ly_start, ly_end), cats=cats, chart=chart, dims=dims,
                                                       dim_opts=Q.dims_options(s, metric)), s, u)


@app.get("/reports/yearly", response_class=HTMLResponse)
def report_yearly(request: Request, year: int | None = None, metric: str = "sales_net", s=Depends(get_db),
                  u=Depends(current_user)):
    today = ag.today_jst()
    y = year or (today - timedelta(days=1)).year
    codes = allowed_codes(s, u)
    periods = [f"{y:04d}-{m:02d}" for m in range(1, 13)]
    lperiods = [f"{y - 1:04d}-{m:02d}" for m in range(1, 13)]
    cur = Q.agg_rows(s, "M", periods, codes, metric)
    last = Q.agg_rows(s, "M", lperiods, codes, metric)
    cmp_end, in_progress = ag.elapsed_end(date(y, 1, 1), date(y, 12, 31), today)
    ytd = {c: ag.total(s, [c], metric, date(y, 1, 1), cmp_end, today=today) for c in codes}
    lytd = {c: ag.total(s, [c], metric, date(y - 1, 1, 1), ag.shift_year(cmp_end, -1), today=today) for c in codes}
    defs = Q.metric_defs(s)
    all_ytd = ag.total(s, codes, metric, date(y, 1, 1), cmp_end, today=today) if defs[metric].cross_clinic_sum else None
    all_lytd = ag.total(s, codes, metric, date(y - 1, 1, 1), ag.shift_year(cmp_end, -1), today=today) if defs[metric].cross_clinic_sum else None
    chart = {"labels": [f"{m}月" for m in range(1, 13)],
             "series": {c.name: [cur.get((c.code, p)).value if cur.get((c.code, p)) else None for p in periods]
                        for c in Q.clinics(s, codes)}}
    return render(request, "report_yearly.html", dict(y=y, metric=metric, periods=periods, lperiods=lperiods, cur=cur,
                                                      last=last, ytd=ytd, lytd=lytd, all_ytd=all_ytd, all_lytd=all_lytd,
                                                      clinics=Q.clinics(s, codes), in_progress=in_progress,
                                                      cmp_end=cmp_end, chart=chart), s, u)


# ---------------------------------------------------------------------------
# 日報一覧・日別状態・根拠
# ---------------------------------------------------------------------------

@app.get("/posts", response_class=HTMLResponse)
def posts(request: Request, month: str | None = None, clinic: str = "", s=Depends(get_db), u=Depends(current_user)):
    y, m = _ym(month, ag.today_jst() - timedelta(days=1))
    start, end = ag.month_range(y, m)
    codes = [c for c in allowed_codes(s, u) if not clinic or c == clinic]
    rows = Q.reports_list(s, codes, start, end)
    files = {f.file_id: f for f in s.scalars(select(SourceFile).where(SourceFile.file_id.in_(
        [r.primary_file_id for r, _ in rows if r.primary_file_id])))}
    others = Q.other_posts(s, codes, start, end + timedelta(days=10))
    return render(request, "posts.html", dict(rows=rows, files=files, others=others, y=y, m=m, clinic=clinic,
                                              clinics=Q.clinics(s, allowed_codes(s, u))), s, u)


@app.get("/days", response_class=HTMLResponse)
def days(request: Request, month: str | None = None, status: str = "", s=Depends(get_db), u=Depends(current_user)):
    y, m = _ym(month, ag.today_jst() - timedelta(days=1))
    start, end = ag.month_range(y, m)
    codes = allowed_codes(s, u)
    st = Q.statuses(s, codes, start, end)
    dlist = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    listing = [r for r in st.values() if not status or r.status == status]
    listing.sort(key=lambda r: (r.target_date, r.clinic_code))
    return render(request, "days.html", dict(y=y, m=m, st=st, days=dlist, clinics=Q.clinics(s, codes),
                                             listing=listing, status=status), s, u)


@app.get("/evidence", response_class=HTMLResponse)
def evidence(request: Request, clinic: str, day: str, metric: str, s=Depends(get_db), u=Depends(current_user)):
    if clinic not in allowed_codes(s, u):
        raise HTTPException(403)
    d = date.fromisoformat(day)
    ev = Q.evidence(s, clinic, d, metric, _filters(request))
    adj = s.scalars(select(ManualAdjustment).where(ManualAdjustment.clinic_code == clinic, ManualAdjustment.target_date == d,
                                                   ManualAdjustment.metric == metric).order_by(ManualAdjustment.created_at.desc())).all()
    return render(request, "evidence.html", dict(clinic=clinic, d=d, metric=metric, ev=ev, adj=adj, dims=_filters(request)), s, u)


@app.get("/raw/{post_id}", response_class=HTMLResponse)
def raw_post(request: Request, post_id: int, s=Depends(get_db), u=Depends(require_admin)):
    """原文の閲覧（管理者のみ・操作履歴に残す）。"""
    p = s.scalar(select(SourcePost).where(SourcePost.post_id == post_id))
    if not p:
        raise HTTPException(404)
    audit(s, request, u.username, "view_raw_post", str(post_id))
    comments = [(c, decrypt_text(c.body_enc)) for c in s.scalars(select(SourceComment).where(SourceComment.post_id == post_id))]
    files = s.scalars(select(SourceFile).where(SourceFile.post_pk == p.id)).all()
    return render(request, "raw.html", dict(p=p, text=decrypt_text(p.body_enc), comments=comments, files=files), s, u)


# ---------------------------------------------------------------------------
# 確認キュー・差異・採用値・手動補正
# ---------------------------------------------------------------------------

@app.get("/issues", response_class=HTMLResponse)
def issues(request: Request, status: str = "open", kind: str = "", clinic: str = "", s=Depends(get_db),
           u=Depends(current_user)):
    codes = [c for c in allowed_codes(s, u) if not clinic or c == clinic]
    q = select(Issue).where(Issue.clinic_code.in_(codes))
    if status:
        q = q.where(Issue.status == status)
    if kind:
        q = q.where(Issue.kind == kind)
    rows = s.scalars(q.order_by(Issue.target_date.desc().nullslast(), Issue.severity.desc()).limit(500)).all()
    from ..issues import KIND_LABELS
    counts = dict(s.execute(select(Issue.kind, func.count()).where(Issue.status == "open", Issue.clinic_code.in_(codes))
                            .group_by(Issue.kind)).all())
    return render(request, "issues.html", dict(rows=rows, status=status, kind=kind, clinic=clinic, KIND=KIND_LABELS,
                                               counts=counts, clinics=Q.clinics(s, allowed_codes(s, u))), s, u)


@app.get("/issues/{iid}", response_class=HTMLResponse)
def issue_detail(request: Request, iid: int, s=Depends(get_db), u=Depends(current_user)):
    i = s.get(Issue, iid)
    if not i or i.clinic_code not in allowed_codes(s, u):
        raise HTTPException(404)
    ev = None
    if i.metric and i.target_date:
        dims = {}
        if i.kind == "body_excel_mismatch":
            oid = (i.detail.get("body") or {}).get("obs_id")
            o = s.get(Observation, oid) if oid else None
            if o:
                dims = {c: getattr(o, c) for c in DIM_COLS if getattr(o, c)}
        ev = Q.evidence(s, i.clinic_code, i.target_date, i.metric, dims)
    post = s.scalar(select(SourcePost).where(SourcePost.post_id == i.post_id)) if i.post_id else None
    from ..issues import KIND_LABELS
    return render(request, "issue.html", dict(i=i, ev=ev, post=post, KIND=KIND_LABELS), s, u)


@app.post("/issues/{iid}/resolve")
def issue_resolve(request: Request, iid: int, action: str = Form(...), note: str = Form(""), csrf: str = Form(""),
                  s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    i = s.get(Issue, iid)
    i.status = "resolved" if action == "resolve" else "ignored" if action == "ignore" else "open"
    i.resolution, i.resolved_by, i.resolved_at = note, u.username, datetime.utcnow()
    audit(s, request, u.username, f"issue_{action}", str(iid), {"note": note})
    if i.target_date:
        refresh_days(s, i.clinic_code, i.target_date, i.target_date, ag.today_jst())
    return RedirectResponse(f"/issues/{iid}", status_code=303)


@app.post("/adopt")
def adopt(request: Request, obs_id: int = Form(...), reason: str = Form(...), issue_id: int = Form(0),
          csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    """本文とExcelで差異があるとき、採用する値（出典）を選ぶ。"""
    check_csrf(request, csrf)
    o = s.get(Observation, obs_id)
    if not o:
        raise HTTPException(404)
    _apply_adjustment(s, request, u, o.clinic_code, o.target_date, o.metric,
                      {c: getattr(o, c) for c in DIM_COLS if getattr(o, c)}, o.value, o.state, reason or "採用値の選択", o.id)
    if issue_id:
        i = s.get(Issue, issue_id)
        i.status, i.resolution, i.resolved_by, i.resolved_at = "resolved", f"採用: {o.source_kind} {o.value} / {reason}", u.username, datetime.utcnow()
    return RedirectResponse(f"/issues/{issue_id}" if issue_id else "/issues", status_code=303)


@app.get("/adjust", response_class=HTMLResponse)
def adjust_form(request: Request, clinic: str = "", day: str = "", metric: str = "sales_net", s=Depends(get_db),
                u=Depends(require_admin)):
    return render(request, "adjust.html", dict(clinic=clinic, day=day, metric=metric, clinics=Q.clinics(s),
                                               dims=_filters(request)), s, u)


@app.post("/adjust")
def adjust(request: Request, clinic: str = Form(...), day: str = Form(...), metric: str = Form(...),
           value: str = Form(""), state: str = Form("value"), reason: str = Form(...), market: str = Form(""),
           cust: str = Form(""), category: str = Form(""), channel: str = Form(""), method: str = Form(""),
           csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    if not reason.strip():
        raise HTTPException(400, "理由を入力してください")
    d = date.fromisoformat(day)
    dims = {k: v for k, v in dict(market=market, cust=cust, category=category, channel=channel, method=method).items() if v}
    val = float(value.replace(",", "")) if value.strip() else None
    st = "zero" if val == 0 else ("value" if val is not None else state)
    _apply_adjustment(s, request, u, clinic, d, metric, dims, val, st, reason, None)
    return RedirectResponse(f"/evidence?clinic={clinic}&day={day}&metric={metric}" +
                            "".join(f"&{k}={quote(v)}" for k, v in dims.items()), status_code=303)


@app.post("/adjust/{aid}/revoke")
def adjust_revoke(request: Request, aid: int, csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    a = s.get(ManualAdjustment, aid)
    a.active = False
    audit(s, request, u.username, "adjust_revoke", str(aid))
    reconcile_day(s, a.clinic_code, a.target_date)
    refresh_days(s, a.clinic_code, a.target_date, a.target_date, ag.today_jst())
    ag.recompute(s, {(a.target_date.year, a.target_date.month)})
    return RedirectResponse(f"/evidence?clinic={a.clinic_code}&day={a.target_date}&metric={a.metric}", status_code=303)


def _apply_adjustment(s, request, u, clinic, d, metric, dims, value, state, reason, obs_id):
    fq = select(DailyFact).where(DailyFact.clinic_code == clinic, DailyFact.target_date == d, DailyFact.metric == metric)
    for c in DIM_COLS:
        fq = fq.where(getattr(DailyFact, c) == (dims.get(c) or ""))
    before = s.scalar(fq)
    for old in s.scalars(select(ManualAdjustment).where(ManualAdjustment.clinic_code == clinic, ManualAdjustment.target_date == d,
                                                        ManualAdjustment.metric == metric, ManualAdjustment.active.is_(True))):
        if (old.dims or {}) == dims:
            old.active = False
    s.add(ManualAdjustment(clinic_code=clinic, target_date=d, metric=metric, dims=dims,
                           before_value=before.value if before else None, after_value=value, after_state=state,
                           source_obs_id=obs_id, reason=reason, user=u.username))
    s.flush()
    audit(s, request, u.username, "adjust", f"{clinic} {d} {metric}", {"dims": dims, "value": value, "reason": reason})
    reconcile_day(s, clinic, d)
    refresh_days(s, clinic, d, d, ag.today_jst())
    ag.recompute(s, {(d.year, d.month)})


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, s=Depends(get_db), u=Depends(current_user)):
    adjs = s.scalars(select(ManualAdjustment).order_by(ManualAdjustment.created_at.desc()).limit(300)).all()
    logs = s.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(300)).all() if u.role == "admin" else []
    return render(request, "history.html", dict(adjs=adjs, logs=logs), s, u)


# ---------------------------------------------------------------------------
# 出力（CSV / Excel）
# ---------------------------------------------------------------------------

@app.get("/export")
def export(request: Request, kind: str = "daily", start: str = "", end: str = "", fmt_: str = "csv",
           s=Depends(get_db), u=Depends(current_user)):
    today = ag.today_jst()
    a = _d(start, today.replace(day=1))
    b = _d(end, today - timedelta(days=1))
    codes = allowed_codes(s, u)
    defs = Q.metric_defs(s)
    names = {c.code: c.name for c in Q.clinics(s)}
    if kind == "monthly":
        header = ["期間", "クリニック", "指標", "区分", "値", "値のある日数", "未提出・失敗日数", "要確認日数", "暫定", "備考"]
        rows = [[r.period, names.get(r.clinic_code, "全院合計"), defs[r.metric].name, r.dims_key, r.value, r.days_with_value,
                 r.days_missing, r.days_review, "暫定" if r.provisional else "", r.note]
                for r in s.scalars(select(Aggregate).where(Aggregate.period_type.in_(("M", "Y")),
                                                           Aggregate.clinic_code.in_(codes + ["ALL"]))
                                   .order_by(Aggregate.period_type, Aggregate.period, Aggregate.clinic_code, Aggregate.metric))]
    else:
        header = ["対象日", "クリニック", "指標", "国内/インバウンド", "顧客区分", "カテゴリ", "経路", "決済方法", "値", "状態",
                  "採用状態", "備考"]
        rows = [[f.target_date.isoformat(), names.get(f.clinic_code, f.clinic_code), defs[f.metric].name, f.market, f.cust,
                 f.category, f.channel, f.method, f.value, Q.STATE_LABEL.get(f.state, f.state), Q.FACT_STATUS.get(f.status, f.status), f.note]
                for f in s.scalars(select(DailyFact).where(DailyFact.clinic_code.in_(codes), DailyFact.target_date >= a,
                                                           DailyFact.target_date <= b)
                                   .order_by(DailyFact.target_date, DailyFact.clinic_code, DailyFact.metric))]
    audit(s, request, u.username, "export", f"{kind} {a}..{b} {fmt_}")
    fname = f"clinic_{kind}_{a:%Y%m%d}_{b:%Y%m%d}"
    if fmt_ == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "集計"
        ws.append(header)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={fname}.xlsx"})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}.csv"})


# ---------------------------------------------------------------------------
# 稼働状況・操作
# ---------------------------------------------------------------------------

@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request, s=Depends(get_db), u=Depends(require_admin)):
    from .. import scheduler
    from ..collector.talknote import reauth_manager
    runs = s.scalars(select(JobRun).order_by(JobRun.queued_at.desc()).limit(20)).all()
    last = runs[0] if runs else None
    steps = s.scalars(select(JobStep).where(JobStep.run_id == last.id).order_by(JobStep.id)).all() if last else []
    per_clinic = []
    for c in Q.clinics(s):
        lp = s.scalar(select(func.max(SourcePost.fetched_at)).where(SourcePost.clinic_code == c.code))
        cnt = s.scalar(select(func.count()).select_from(SourcePost).where(SourcePost.clinic_code == c.code)) or 0
        last_report = s.scalar(select(func.max(Report.target_date)).where(Report.clinic_code == c.code))
        fs = dict(s.execute(select(SourceFile.status, func.count()).join(SourcePost, SourceFile.post_pk == SourcePost.id)
                            .where(SourcePost.clinic_code == c.code).group_by(SourceFile.status)).all())
        ds = dict(s.execute(select(DayStatus.status, func.count()).where(DayStatus.clinic_code == c.code)
                            .group_by(DayStatus.status)).all())
        per_clinic.append(dict(c=c, last_fetch=lp, posts=cnt, last_report=last_report, files=fs, days=ds))
    notes = s.scalars(select(Notification).order_by(Notification.created_at.desc()).limit(20)).all()
    sched = get_setting(s, "schedule")
    creds = get_secret(s, "talknote_credentials")
    return render(request, "ops.html", dict(runs=runs, last=last, steps=steps, per_clinic=per_clinic, notes=notes,
                                            sched=sched, next_run=scheduler.next_run(), scheduler_on=settings.run_scheduler,
                                            creds_registered=bool(creds or load_credentials(s)),
                                            reauth=reauth_manager.state, storage=storage_usage()), s, u)


@app.post("/ops/run")
def ops_run(request: Request, action: str = Form(...), start: str = Form(""), end: str = Form(""),
            csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    if action == "collect":
        rid = enqueue("manual", {"start": start, "end": end} if start else {}, u.username)
    elif action == "reaggregate":
        if not start or not end:
            raise HTTPException(400, "期間を指定してください")
        rid = enqueue("reparse", {"start": start, "end": end}, u.username)
    elif action == "retry":
        rid = enqueue("retry_failed", {}, u.username)
    elif action == "test":
        rid = enqueue("connection_test", {}, u.username)
    elif action == "backup":
        rid = enqueue("backup", {}, u.username)
    else:
        raise HTTPException(400)
    audit(s, request, u.username, f"ops_{action}", str(rid), {"start": start, "end": end})
    s.commit()
    if not settings.run_scheduler:  # スケジューラーを別プロセスで動かしていない場合はその場で実行
        import threading
        from ..jobs import execute
        threading.Thread(target=execute, args=(rid,), daemon=True).start()
    return RedirectResponse(f"/ops#run{rid}", status_code=303)


@app.get("/ops/run/{rid}", response_class=HTMLResponse)
def ops_run_detail(request: Request, rid: int, s=Depends(get_db), u=Depends(require_admin)):
    run = s.get(JobRun, rid)
    steps = s.scalars(select(JobStep).where(JobStep.run_id == rid).order_by(JobStep.id)).all()
    return render(request, "run.html", dict(run=run, steps=steps), s, u)


@app.post("/ops/reauth")
def ops_reauth(request: Request, step: str = Form(...), code: str = Form(""), csrf: str = Form(""), s=Depends(get_db),
               u=Depends(require_admin)):
    check_csrf(request, csrf)
    from ..collector.talknote import reauth_manager
    if step == "start":
        creds = load_credentials(s)
        if not creds:
            raise HTTPException(400, "ログイン情報が未登録です")
        reauth_manager.begin(creds)
    elif step == "code":
        reauth_manager.submit(code.strip())
    if reauth_manager.state.get("status") == "done":
        put_setting(s, "auth_state", {"reauth_required": False, "message": "再認証済み", "at": datetime.utcnow().isoformat()})
    audit(s, request, u.username, f"reauth_{step}")
    return RedirectResponse("/ops#reauth", status_code=303)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s=Depends(get_db), u=Depends(require_admin)):
    creds = get_secret(s, "talknote_credentials")
    rules = {}
    for c in Q.clinics(s):
        for kind in ("body", "excel"):
            ver, rule = active_rule(s, c.code, kind)
            rules[(c.code, kind)] = (ver, json.dumps(rule, ensure_ascii=False, indent=1))
    rule_hist = s.scalars(select(ParseRule).order_by(ParseRule.clinic_code, ParseRule.kind, ParseRule.version.desc())).all()
    users = s.scalars(select(AppUser).order_by(AppUser.username)).all()
    return render(request, "settings.html", dict(
        login_id=(creds or {}).get("login_id", ""), pw_state=mask((creds or {}).get("password")),
        env_creds=bool(load_credentials(s)) and not creds, clinics=s.scalars(select(Clinic).order_by(Clinic.sort_order)).all(),
        sched=get_setting(s, "schedule"), coll=get_setting(s, "collection"), notify=get_setting(s, "notify"),
        aggcfg=get_setting(s, "aggregation"), ai=get_setting(s, "ai"), retention=get_setting(s, "retention"),
        targets=Q.targets(s), rules=rules, rule_hist=rule_hist, users=users), s, u)


@app.post("/settings/credentials")
def settings_credentials(request: Request, login_id: str = Form(...), password: str = Form(""), csrf: str = Form(""),
                         s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    old = get_secret(s, "talknote_credentials") or {}
    put_secret(s, "talknote_credentials", {"login_id": login_id.strip(), "password": password or old.get("password", "")})
    from ..security import delete_secret
    delete_secret(s, "talknote_session")
    audit(s, request, u.username, "set_credentials", detail={"login_id": login_id.strip(), "password_changed": bool(password)})
    return RedirectResponse("/settings#creds", status_code=303)


@app.post("/settings/schedule")
def settings_schedule(request: Request, enabled: str = Form(""), hour: int = Form(0), minute: int = Form(10),
                      recheck_days: int = Form(31), start_date: str = Form(...), fiscal_start: int = Form(1),
                      raw_days: int = Form(730), ai_enabled: str = Form(""), csrf: str = Form(""),
                      s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    put_setting(s, "schedule", {"enabled": bool(enabled), "hour": hour, "minute": minute, "recheck_days": recheck_days})
    put_setting(s, "collection", {"start_date": date.fromisoformat(start_date).isoformat()})
    put_setting(s, "aggregation", {"fiscal_year_start_month": fiscal_start, "month_start_day": 1})
    put_setting(s, "retention", {"raw_days": raw_days})
    put_setting(s, "ai", {"enabled": bool(ai_enabled)})
    audit(s, request, u.username, "set_schedule", detail={"hour": hour, "minute": minute, "enabled": bool(enabled)})
    s.commit()
    from .. import scheduler
    scheduler.reschedule()
    return RedirectResponse("/settings#schedule", status_code=303)


@app.post("/settings/notify")
def settings_notify(request: Request, emails: str = Form(""), webhook_url: str = Form(""), csrf: str = Form(""),
                    s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    put_setting(s, "notify", {"emails": [e.strip() for e in emails.replace("、", ",").split(",") if e.strip()],
                              "webhook_url": webhook_url.strip()})
    audit(s, request, u.username, "set_notify")
    return RedirectResponse("/settings#notify", status_code=303)


@app.post("/settings/clinic/{code}")
def settings_clinic(request: Request, code: str, name: str = Form(...), group_id: int = Form(...), enabled: str = Form(""),
                    closed_weekdays: list[str] = Form([]), closed_dates: str = Form(""), open_dates: str = Form(""),
                    open_from: str = Form(""), csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    c = s.scalar(select(Clinic).where(Clinic.code == code))
    c.name, c.group_id, c.enabled = name, group_id, bool(enabled)
    c.closed_weekdays = sorted(int(x) for x in closed_weekdays)
    c.closed_dates = sorted({x.strip() for x in closed_dates.replace("、", ",").replace("\n", ",").split(",") if x.strip()})
    c.open_dates = sorted({x.strip() for x in open_dates.replace("、", ",").replace("\n", ",").split(",") if x.strip()})
    new_open = date.fromisoformat(open_from) if open_from else None
    changed = new_open != c.open_from
    c.open_from = new_open
    audit(s, request, u.username, "set_clinic", code, {"open_from": open_from})
    if changed:  # 日別の状態と集計をすぐ反映
        today = ag.today_jst()
        floor = date.fromisoformat(get_setting(s, "collection").get("start_date", "2026-09-01"))
        s.flush()
        refresh_days(s, code, floor, today - timedelta(days=1), today)
        ag.recompute(s, {(d.year, d.month) for d in (floor + timedelta(days=i) for i in range((today - floor).days))})
    return RedirectResponse("/settings#clinics", status_code=303)


@app.post("/settings/target")
def settings_target(request: Request, clinic: str = Form(...), month: str = Form(...), metric: str = Form("sales_net"),
                    value: str = Form(...), csrf: str = Form(""), s=Depends(get_db), u=Depends(require_admin)):
    check_csrf(request, csrf)
    y, m = _ym(month, ag.today_jst())
    row = s.scalar(select(Target).where(Target.clinic_code == clinic, Target.year == y, Target.month == m, Target.metric == metric))
    if value.strip() == "":
        if row:
            s.delete(row)
    else:
        v = float(value.replace(",", ""))
        if row:
            row.value = v
        else:
            s.add(Target(clinic_code=clinic, year=y, month=m, metric=metric, value=v))
    audit(s, request, u.username, "set_target", f"{clinic} {y}-{m} {metric}", {"value": value})
    return RedirectResponse("/settings#targets", status_code=303)


@app.post("/settings/rule")
def settings_rule(request: Request, clinic: str = Form(...), kind: str = Form(...), rule_json: str = Form(...),
                  note: str = Form(""), reparse_from: str = Form(""), csrf: str = Form(""), s=Depends(get_db),
                  u=Depends(require_admin)):
    check_csrf(request, csrf)
    try:
        rule = json.loads(rule_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSONの形式が正しくありません: {e}")
    ver = save_rule(s, clinic, kind, rule, u.username, note or "管理画面で変更")
    audit(s, request, u.username, "set_rule", f"{clinic} {kind} v{ver}", {"note": note})
    if reparse_from:
        enqueue("reparse", {"start": reparse_from, "end": (ag.today_jst() - timedelta(days=1)).isoformat(), "clinics": [clinic]},
                u.username)
    return RedirectResponse("/settings#rules", status_code=303)


@app.post("/settings/user")
def settings_user(request: Request, username: str = Form(...), password: str = Form(""), role: str = Form(...),
                  clinic_codes: list[str] = Form([]), active: str = Form(""), csrf: str = Form(""), s=Depends(get_db),
                  u=Depends(require_admin)):
    check_csrf(request, csrf)
    if role not in ("admin", "viewer_all", "viewer_clinic"):
        raise HTTPException(400)
    row = s.scalar(select(AppUser).where(AppUser.username == username))
    if row is None:
        if len(password) < 10:
            raise HTTPException(400, "パスワードは10文字以上にしてください")
        row = AppUser(username=username, password_hash=hash_password(password), role=role)
        s.add(row)
    elif password:
        if len(password) < 10:
            raise HTTPException(400, "パスワードは10文字以上にしてください")
        row.password_hash = hash_password(password)
    row.role, row.clinic_codes, row.active = role, clinic_codes, bool(active)
    audit(s, request, u.username, "set_user", username, {"role": role, "clinics": clinic_codes})
    return RedirectResponse("/settings#users", status_code=303)
