"""Talknote への接続（サーバー側のブラウザ自動操作）。

公式APIはこのネットワークで無効（permission.manage.external_api=off）のため、
Playwright でログインし、ログイン状態のまま Web画面が使う読み取り用の通信だけを呼ぶ。
- 読み取りのみ。投稿・既読操作・設定変更はしない
- リクエストの間隔をあける（TALKNOTE_REQUEST_INTERVAL 秒）
- パスワードはログに出さない
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger("talknote")

LOGIN_TIMEOUT_MS = 45_000


class TalknoteError(Exception):
    pass


class LoginFailed(TalknoteError):
    """ID/パスワードの誤りなど。"""


class ReauthRequired(TalknoteError):
    """追加認証（認証コード等）が必要。管理画面から認証を完了してもらう。"""


class SessionExpired(TalknoteError):
    pass


@dataclass
class Credentials:
    login_id: str
    password: str


def _form(d: dict) -> str:
    from urllib.parse import urlencode
    return urlencode(d)


class TalknoteClient:
    def __init__(self, creds: Credentials | None, storage_state: dict | None = None):
        self.creds = creds
        self.storage_state = storage_state
        self.net = settings.network_code
        self.app = settings.app_host.rstrip("/")
        self.login_url = f"{settings.login_host.rstrip('/')}/{self.net}/"
        self._pw = self._browser = self._ctx = None
        self._last = 0.0
        self.state_changed = False

    # ---- ライフサイクル ----
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(storage_state=self.storage_state, locale="ja-JP",
                                              timezone_id="Asia/Tokyo")
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _throttle(self):
        wait = settings.request_interval_sec - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # ---- ログイン ----
    def session_ok(self, probe_group_id: int) -> bool:
        """実際に投稿一覧を1件取得できるかで判定する。
        （実データで確認：ログイン切れでも HTTP 200・data=null が返ることがあるため、HTTPの状態だけでは判定しない）"""
        try:
            self.list_posts(probe_group_id, 0, 1)
            return True
        except (SessionExpired, TalknoteError):
            return False

    def ensure_login(self, probe_group_id: int) -> str:
        if self.storage_state and self.session_ok(probe_group_id):
            return "既存のログイン状態を利用"
        self.login()
        if not self.session_ok(probe_group_id):
            raise LoginFailed("ログイン後もノートを取得できない（権限またはURLを確認）")
        return "ログインしました"

    def login(self):
        if not self.creds or not self.creds.login_id or not self.creds.password:
            raise LoginFailed("Talknoteのログイン情報が未登録です（設定画面または環境変数）")
        page = self._ctx.new_page()
        try:
            page.goto(self.login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
            page.fill("#mail", self.creds.login_id)
            page.fill("#password", self.creds.password)
            try:
                if not page.is_checked("#keep_login"):
                    page.check("#keep_login")
            except Exception:
                pass
            page.click("#submit_login")
            try:
                page.wait_for_url(lambda u: "/group/" in u or "/feed" in u or "s01." in u, timeout=LOGIN_TIMEOUT_MS)
            except Exception:
                pass
            page.wait_for_load_state("domcontentloaded")
            if page.locator("#password").count() and page.locator("#password").is_visible():
                raise LoginFailed("ログインできませんでした（ID・パスワードを確認してください）")
            body = page.inner_text("body")[:2000]
            if any(k in body for k in ("認証コード", "ワンタイム", "二段階", "確認コード", "2段階")):
                raise ReauthRequired("追加認証が求められています。管理画面の「再認証」から認証を完了してください。")
            self.storage_state = self._ctx.storage_state()
            self.state_changed = True
        finally:
            page.close()

    # ---- 読み取り ----
    def _get_json(self, path: str) -> dict:
        self._throttle()
        r = self._ctx.request.get(self.app + path, headers={"X-Requested-With": "XMLHttpRequest"},
                                  max_redirects=0, timeout=60_000)
        return self._json(r, path)

    def _post_json(self, path: str, data: dict) -> dict:
        self._throttle()
        r = self._ctx.request.post(self.app + path, data=_form(data), max_redirects=0, timeout=60_000,
                                   headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                            "X-Requested-With": "XMLHttpRequest"})
        return self._json(r, path)

    @staticmethod
    def _json(r, path: str) -> dict:
        if r.status in (301, 302, 303, 401, 403):
            raise SessionExpired(f"ログイン切れ（{r.status} {path}）")
        if r.status >= 400:
            raise TalknoteError(f"HTTP {r.status} {path}")
        try:
            j = r.json()
        except Exception:
            raise SessionExpired(f"JSON以外の応答（ログイン画面の可能性） {path}")
        if isinstance(j, dict) and j.get("status") == 0 and j.get("errors"):
            raise TalknoteError(f"Talknoteがエラーを返しました: {j.get('errors')}")
        return j

    def list_posts(self, group_id: int, offset: int = 0, limit: int = 20, sort_key: str = "regist_date") -> list[dict]:
        j = self._post_json(f"/{self.net}/ajax/group/{group_id}/msg/list",
                            {"sort_key": sort_key, "sort_type": "desc", "limit": limit, "offset": offset})
        data = j.get("data", j) if isinstance(j, dict) else None
        if not isinstance(data, dict):
            raise SessionExpired("投稿一覧が空の応答（data=null）。ログイン切れの可能性")
        msgs = data.get("msg")
        if msgs is None:
            raise TalknoteError("投稿一覧の形式が想定と異なる（msg がない）")
        return msgs

    def list_comments(self, group_id: int, post_id: int) -> list[dict]:
        j = self._post_json(f"/{self.net}/ajax/group/{group_id}/msg/{post_id}/comment/list", {"limit": 200})
        data = j.get("data", j) if isinstance(j, dict) else None
        if not isinstance(data, dict):
            raise SessionExpired("コメント一覧が空の応答（data=null）。ログイン切れの可能性")
        return data.get("msg") or []

    def download(self, group_id: int, file_id: int) -> bytes:
        self._throttle()
        r = self._ctx.request.get(f"{self.app}/{self.net}/ajax/group/{group_id}/attachment/{file_id}/download",
                                  max_redirects=3, timeout=180_000)
        if r.status != 200:
            raise TalknoteError(f"添付の取得に失敗 HTTP {r.status}")
        body = r.body()
        if body[:2] != b"PK" and body.lstrip()[:1] in (b"<", b"{"):
            raise SessionExpired("添付の代わりにHTML/JSONが返った（ログイン切れの可能性）")
        return body

    def post_url(self, group_id: int, post_id: int) -> str:
        return f"{self.app}/{self.net}/group/{group_id}/msg/{post_id}/"


# ---------------------------------------------------------------------------
# 追加認証：管理者が画面からコードを入力して再開する
#   Playwright（sync）はスレッドに結び付くので、専用スレッドで操作する。
# ---------------------------------------------------------------------------

class ReauthManager:
    TIMEOUT_SEC = 600

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.state = {"status": "idle", "message": "", "screenshot": "", "started": 0.0}

    def begin(self, creds: Credentials) -> dict:
        if self._thread and self._thread.is_alive():
            return self.state
        self.state = {"status": "starting", "message": "ログイン画面を開いています…", "screenshot": "", "started": time.time()}
        self._thread = threading.Thread(target=self._run, args=(creds,), daemon=True)
        self._thread.start()
        for _ in range(60):
            if self.state["status"] != "starting":
                break
            time.sleep(0.5)
        return self.state

    def submit(self, code: str) -> dict:
        self._q.put(("code", code))
        for _ in range(60):
            time.sleep(0.5)
            if self.state["status"] in ("done", "failed", "waiting_code_again"):
                break
        return self.state

    def _run(self, creds: Credentials):
        from playwright.sync_api import sync_playwright

        from ..db import session_scope
        from ..security import put_secret

        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                ctx = b.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
                page = ctx.new_page()
                page.goto(f"{settings.login_host.rstrip('/')}/{settings.network_code}/", wait_until="domcontentloaded")
                page.fill("#mail", creds.login_id)
                page.fill("#password", creds.password)
                page.click("#submit_login")
                page.wait_for_load_state("networkidle", timeout=LOGIN_TIMEOUT_MS)
                while True:
                    text = page.inner_text("body")[:600]
                    shot = base64.b64encode(page.screenshot(full_page=False)).decode()
                    needs_code = any(k in text for k in ("認証コード", "ワンタイム", "二段階", "確認コード", "2段階"))
                    if not needs_code:
                        if page.locator("#password").count() and page.locator("#password").is_visible():
                            self.state.update(status="failed", message="ログインできませんでした（ID・パスワードを確認）", screenshot=shot)
                            return
                        with session_scope() as s:
                            put_secret(s, "talknote_session", ctx.storage_state())
                        self.state.update(status="done", message="認証が完了しました。収集を再開できます。", screenshot="")
                        return
                    self.state.update(status="waiting_code", message="認証コードを入力してください", screenshot=shot)
                    try:
                        kind, code = self._q.get(timeout=self.TIMEOUT_SEC)
                    except queue.Empty:
                        self.state.update(status="failed", message="時間切れのため中止しました")
                        return
                    inp = page.locator("input[type=text]:visible, input[type=tel]:visible, input[type=number]:visible").first
                    inp.fill(code)
                    inp.press("Enter")
                    page.wait_for_load_state("networkidle", timeout=LOGIN_TIMEOUT_MS)
                    self.state["status"] = "waiting_code_again"
        except Exception as e:  # noqa: BLE001
            log.exception("reauth failed")
            self.state.update(status="failed", message=f"再認証に失敗しました: {type(e).__name__}")


reauth_manager = ReauthManager()


def summarize_post(m: dict) -> dict:
    """ログ・画面用の安全な要約（本文は含めない）。"""
    return {"id": m.get("group_message_id"), "regist": m.get("regist_date"), "update": m.get("update_date"),
            "files": [f.get("file_file_name") for f in m.get("attach_file") or []],
            "comments": m.get("comment_count")}


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)
