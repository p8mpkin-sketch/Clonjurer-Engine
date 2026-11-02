"""
平台安全基座: 口令散列(werkzeug PBKDF2) + 敏感配置对称加密(Fernet)。

密钥管理: BASE_DIR/.secret_key 首次访问自动生成(0600), 同时派生
Flask session 签名密钥与 Fernet 密钥 —— 删除该文件将使所有已加密
API Key 与登录会话失效。
"""
import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KEY_FILE = Path(os.environ.get("CLONER_SECRET_FILE") or _PROJECT_ROOT / ".secret_key")
_fernet = None


def _load_master_key() -> bytes:
    """主密钥: 64 位十六进制, 落盘一次, 进程内缓存。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    raw = os.environ.get("CLONER_SECRET_KEY") or None
    if raw and len(raw) >= 32:
        master = raw.encode("utf-8")
    else:
        try:
            master = bytes.fromhex(_KEY_FILE.read_text(encoding="utf-8").strip())
            if len(master) != 32:
                raise ValueError
        except (OSError, ValueError):
            master = secrets.token_bytes(32)
            _KEY_FILE.write_text(master.hex(), encoding="utf-8")
            try:
                os.chmod(_KEY_FILE, 0o600)
            except OSError:
                pass
    _fernet = master
    return master


def session_secret() -> str:
    """Flask SECRET_KEY: 主密钥域分离派生。"""
    return hashlib.sha256(b"flask-session:" + _load_master_key()).hexdigest()


def _fernet_cipher() -> Fernet:
    digest = hashlib.sha256(b"fernet:" + _load_master_key()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(plain: str) -> str:
    if not plain:
        return ""
    return _fernet_cipher().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def hash_password(plain: str) -> str:
    return generate_password_hash(plain, method="pbkdf2:sha256", salt_length=16)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return check_password_hash(hashed, plain)
    except (ValueError, TypeError):
        return False
