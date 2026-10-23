"""画面の動作確認（サンプルデータ）。全画面が表示でき、権限が効き、操作が履歴に残ること。"""
from __future__ import annotations

import re
from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.models import AppUser, AuditLog, DailyFact, Issue, ManualAdjustment, Observation
from app.sample import body_9ru, workbook_9ru
from app.security import hash_password
from tests.helpers import add_post, fact, run

PW = "test-password-123"


def _client(role="admin", clinics=None):
    from app.web.main import app
    with session_scope() as s:
        s.add(AppUser(username=f"u_{role}", password_hash=hash_password(PW), role=role, clinic_codes=clinics or []))
    c = TestClient(app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": f"u_{role}", "password": PW, "csrf": tok, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _csrf(c, path="/settings"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def _data():
    d = date(2026, 10, 16)
    with session_scope() as s:
        p = add_post(s, "9ru", 1, datetime(2026, 10, 16, 18, 43), body_9ru(d, 170000, 230000),
                     workbook_9ru({d: {"gross": 242500, "net": 160000, "receipt": 242500}}, [(2026, 10)]))
        run(s, [p], [("9ru", d)])
    return d


def test_all_pages_render_for_admin():
    d = _data()
    c = _client()
    for path in ["/", "/clinic/9ru?month=2026-10", "/clinic/9ru?view=day&day=2026-10-16", "/clinic/9ru?view=year&year=2026",
                 "/menu?clinic=9ru&month=2026-10", "/menu?clinic=9ru&view=year&year=2026", "/menu?clinic=bio", "/menu?clinic=osaka&market=国内", "/market", "/market?view=month&month=2026-10", "/payments", "/payments?clinic=9ru&month=2026-10", "/payments?clinic=bio&view=year&year=2026", "/reports/daily?day=2026-10-16", "/reports/monthly?month=2026-10",
                 "/reports/monthly?month=2026-10&metric=payment&method=クレジット", "/reports/yearly?year=2026",
                 "/posts?month=2026-10", "/days?month=2026-10", "/issues", "/history", "/ops", "/settings",
                 f"/evidence?clinic=9ru&day={d}&metric=sales_net", "/adjust?clinic=9ru&day=2026-10-16", "/raw/1",
                 "/export?kind=daily&start=2026-10-01&end=2026-10-31", "/export?kind=monthly&fmt_=xlsx"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    with session_scope() as s:
        iid = s.scalar(select(Issue.id).where(Issue.kind == "body_excel_mismatch"))
    assert c.get(f"/issues/{iid}").status_code == 200


def test_adopt_value_resolves_conflict_and_records_history():
    d = _data()
    c = _client()
    with session_scope() as s:
        iss = s.scalar(select(Issue).where(Issue.kind == "body_excel_mismatch", Issue.metric == "sales_net"))
        excel_obs = iss.detail["excel"]["obs_id"]
    tok = _csrf(c, f"/issues/{iss.id}")
    r = c.post("/adopt", data={"obs_id": excel_obs, "reason": "Excelが正（インフルワクチン分を含む）", "issue_id": iss.id, "csrf": tok},
               follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        f = fact(s, "9ru", d, "sales_net")
        assert f.value == 160000 and f.status == "confirmed" and f.adopted_by.startswith("u_admin")
        assert s.scalar(select(ManualAdjustment).where(ManualAdjustment.metric == "sales_net")).before_value is None
        assert s.get(Issue, iss.id).status == "resolved"
        assert s.scalar(select(AuditLog).where(AuditLog.action == "adjust")) is not None


def test_clinic_viewer_cannot_see_other_clinic_or_admin_pages():
    _data()
    c = _client("viewer_clinic", ["prevention"])
    assert c.get("/clinic/9ru").status_code == 403
    assert c.get("/settings").status_code == 403
    assert c.get("/raw/1").status_code == 403
    assert "CLINIC 9ru" not in c.get("/").text


def test_password_never_rendered_and_csrf_required():
    c = _client()
    tok = _csrf(c)
    secret_pw = "not-a-real-password-xyz"
    c.post("/settings/credentials", data={"login_id": "someone@example.test", "password": secret_pw, "csrf": tok})
    page = c.get("/settings").text
    assert secret_pw not in page and "登録済み（非表示）" in page
    assert c.post("/settings/credentials", data={"login_id": "x", "password": "y", "csrf": "bad"}).status_code == 400
    with session_scope() as s:
        logs = s.scalars(select(AuditLog)).all()
        assert all(secret_pw not in str(l.detail) for l in logs)


def test_login_required():
    from app.web.main import app
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_viewer_login_with_admin_next_goes_to_dashboard_and_403_is_a_page():
    """管理者以外が「/login?next=/ops」からログインしても、管理者専用画面ではなくダッシュボードへ移る。"""
    from app.web.main import app
    with session_scope() as s:
        s.add(AppUser(username="viewer1", password_hash=hash_password(PW), role="viewer_all", clinic_codes=[]))
    c = TestClient(app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login?next=/ops").text).group(1)
    r = c.post("/login", data={"username": "viewer1", "password": PW, "csrf": tok, "next": "/ops"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": "viewer1", "password": PW, "csrf": tok, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    r = c.get("/ops", headers={"accept": "text/html"})
    assert r.status_code == 403 and "管理者のみ" in r.text and "ダッシュボードへ" in r.text and '"detail"' not in r.text
    assert c.get("/", headers={"accept": "text/html"}).status_code == 200
