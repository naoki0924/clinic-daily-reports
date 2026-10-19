"""通知。管理画面への記録は常に行い、メール（SMTP）・Webhook は設定されていれば送る。"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.request
from email.message import EmailMessage

from .bootstrap import get_setting
from .models import Notification

log = logging.getLogger("notify")


def notify(s, level: str, title: str, body: str = "") -> Notification:
    cfg = get_setting(s, "notify")
    result = {}
    if cfg.get("emails") and os.environ.get("SMTP_HOST"):
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[日報集計] {title}"
            msg["From"] = os.environ.get("SMTP_FROM", "noreply@localhost")
            msg["To"] = ", ".join(cfg["emails"])
            msg.set_content(body or title)
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as smtp:
                if os.environ.get("SMTP_STARTTLS", "1") == "1":
                    smtp.starttls()
                if os.environ.get("SMTP_USER"):
                    smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
            result["email"] = "sent"
        except Exception as e:  # noqa: BLE001
            result["email"] = f"failed: {type(e).__name__}"
    if cfg.get("webhook_url"):
        try:
            req = urllib.request.Request(cfg["webhook_url"], data=json.dumps({"text": f"{title}\n{body}"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            result["webhook"] = "sent"
        except Exception as e:  # noqa: BLE001
            result["webhook"] = f"failed: {type(e).__name__}"
    row = Notification(level=level, title=title[:255], body=body[:4000], channels=result)
    s.add(row)
    return row
