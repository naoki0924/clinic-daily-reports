"""暗号化・パスワードハッシュ・秘密情報の保存。

- APP_SECRET_KEY（環境変数）から暗号鍵を作る。鍵がない状態では起動しない。
- Talknoteのパスワードは暗号化してDBに保存し、画面・ログには出さない。
- 日報の原文と添付Excelは暗号化して保護領域（APP_DATA_DIR/vault）に保存する。
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not settings.secret_key or len(settings.secret_key) < 32:
        raise SecretKeyMissing(
            "APP_SECRET_KEY が未設定、または短すぎます（32文字以上）。.env に設定してください。"
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(token)
    except InvalidToken as e:  # 鍵の変更など
        raise RuntimeError("暗号化データを復号できません（APP_SECRET_KEY が変わった可能性）") from e


def encrypt_text(text: str) -> bytes:
    return encrypt_bytes(text.encode("utf-8"))


def decrypt_text(token: bytes) -> str:
    return decrypt_bytes(token).decode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 保護領域のファイル ------------------------------------------------------

def vault_dir() -> Path:
    d = settings.data_dir / "vault"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_file(name: str, data: bytes) -> str:
    path = vault_dir() / f"{name}.bin"
    path.write_bytes(encrypt_bytes(data))
    return str(path.relative_to(settings.data_dir))


def load_file(rel_path: str) -> bytes:
    return decrypt_bytes((settings.data_dir / rel_path).read_bytes())


# ---- 秘密情報（DB） ---------------------------------------------------------

def put_secret(session, key: str, value: dict) -> None:
    from .models import SecretStore

    row = session.get(SecretStore, key)
    blob = encrypt_text(json.dumps(value, ensure_ascii=False))
    if row:
        row.ciphertext = blob
    else:
        session.add(SecretStore(key=key, ciphertext=blob))


def get_secret(session, key: str) -> dict | None:
    from .models import SecretStore

    row = session.get(SecretStore, key)
    if not row:
        return None
    return json.loads(decrypt_text(row.ciphertext))


def delete_secret(session, key: str) -> None:
    from .models import SecretStore

    row = session.get(SecretStore, key)
    if row:
        session.delete(row)


# ---- 利用者のパスワード ------------------------------------------------------

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def mask(value: str | None) -> str:
    """画面表示用。登録済みかどうかだけを示す。"""
    return "登録済み（非表示）" if value else "未登録"
