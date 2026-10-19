"""アプリ設定。秘密情報は環境変数（.env）から読み、ソースコードには書かない。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")


def _load_dotenv(path: Path) -> None:
    """依存を増やさない簡易 .env 読み込み（既に設定済みの環境変数は上書きしない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    secret_key: str
    network_code: str
    login_host: str
    app_host: str
    run_scheduler: bool
    request_interval_sec: float
    anthropic_model: str
    libreoffice_path: str | None


def _secret_key(data_dir: Path) -> str:
    """APP_SECRET_KEY が未設定なら、初回起動時にデータ領域へ生成して保存する（Railway 等で秘密情報の手入力を不要にする）。
    この鍵を失うと暗号化した保存データを復号できないため、データ領域ごとバックアップすること。"""
    key = os.environ.get("APP_SECRET_KEY", "")
    if key:
        return key
    path = data_dir / "secret.key"
    if not path.exists():
        import secrets
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip()


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("APP_DATA_DIR", BASE_DIR / "var"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{(data_dir / 'app.db').as_posix()}"),
        data_dir=data_dir,
        secret_key=_secret_key(data_dir),
        network_code=os.environ.get("TALKNOTE_NETWORK", "danae.jp"),
        login_host=os.environ.get("TALKNOTE_LOGIN_HOST", "https://company.talknote.com"),
        app_host=os.environ.get("TALKNOTE_APP_HOST", "https://s01.company.talknote.com"),
        run_scheduler=os.environ.get("RUN_SCHEDULER", "1") == "1",
        request_interval_sec=float(os.environ.get("TALKNOTE_REQUEST_INTERVAL", "0.7")),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        libreoffice_path=os.environ.get("LIBREOFFICE_PATH") or None,
    )


settings = get_settings()
