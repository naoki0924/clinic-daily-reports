"""バックアップ。SQLite はオンラインバックアップAPIで整合性を保ってコピーし zip にする。PostgreSQL は pg_dump を使う。

保護領域（暗号化済みの原文・原本）は追記のみで書き換えないため、毎日のバックアップには含めない
（同じディスクに原本を丸ごと複製すると容量が尽きる。実際に Railway で発生）。
原本の保全はホスティング側のボリュームのバックアップ（Railway の Backups 等）で行う。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .config import settings


class BackupSkipped(RuntimeError):
    pass


def backup_dir() -> Path:
    d = Path(os.environ.get("BACKUP_DIR", settings.data_dir / "backups"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_backups(keep: int) -> None:
    """途中で止まったバックアップの一時ファイルと、古いバックアップを消す。"""
    for t in backup_dir().glob("db-*.tmp"):
        t.unlink(missing_ok=True)
    for old in sorted(backup_dir().glob("backup-*.zip"))[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)


def storage_usage() -> dict:
    """データ領域の使用量（稼働状況の画面に表示する）。"""
    def size(p: Path) -> int:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0

    d = settings.data_dir
    total, used, free = shutil.disk_usage(d)
    db = sum(size(p) for p in d.glob("*.db*"))
    return {"total": total, "used": used, "free": free, "db": db, "vault": size(d / "vault"),
            "backups": size(backup_dir())}


def run_backup(keep: int = 7) -> Path:
    cleanup_backups(keep)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backup_dir() / f"backup-{stamp}.zip"
    tmp_db = backup_dir() / f"db-{stamp}.tmp"
    try:
        if settings.database_url.startswith("sqlite"):
            src_path = settings.database_url.replace("sqlite:///", "")
            need = os.path.getsize(src_path) * 1.6  # 一時コピー＋圧縮後のzip
            free = shutil.disk_usage(backup_dir()).free
            if free < need:
                raise BackupSkipped(f"空き容量が足りないためバックアップを見送りました（空き {free / 1e6:,.0f}MB／必要 約{need / 1e6:,.0f}MB）")
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
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    cleanup_backups(keep)
    return out
