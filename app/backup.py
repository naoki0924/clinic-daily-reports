"""バックアップ。SQLite はオンラインバックアップAPIで整合性を保ってコピーし、
保護領域（暗号化済みの原文・原本）と一緒に zip にする。PostgreSQL は pg_dump を使う。"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .config import settings


def backup_dir() -> Path:
    d = Path(os.environ.get("BACKUP_DIR", settings.data_dir / "backups"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_backup(keep: int = 14) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backup_dir() / f"backup-{stamp}.zip"
    tmp_db = backup_dir() / f"db-{stamp}.tmp"
    if settings.database_url.startswith("sqlite"):
        src_path = settings.database_url.replace("sqlite:///", "")
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        db_name = "app.db"
    else:
        subprocess.run(["pg_dump", "--no-owner", "-f", str(tmp_db), settings.database_url], check=True, timeout=1800)
        db_name = "app.sql"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, db_name)
        vault = settings.data_dir / "vault"
        if vault.exists():
            for p in vault.glob("*.bin"):
                z.write(p, f"vault/{p.name}")
    tmp_db.unlink(missing_ok=True)
    backups = sorted(backup_dir().glob("backup-*.zip"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return out
