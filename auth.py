"""登录认证：PBKDF2 密码哈希 + HMAC 签名会话令牌，仅用标准库。"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime

import config

ROUNDS = 120000


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), ROUNDS).hex()


def _now():
    return datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


class Auth:
    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self._secret = self._load_secret()
        self.data = self._load_account()

    # ---------- 密钥 ----------
    def _load_secret(self):
        if not os.path.isfile(config.SECRET_FILE):
            raw = secrets.token_bytes(32)
            self._write_private(config.SECRET_FILE, raw.hex().encode("ascii"))
        with open(config.SECRET_FILE, "rb") as f:
            return bytes.fromhex(f.read().decode("ascii").strip())

    @staticmethod
    def _write_private(path, data):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ---------- 账号 ----------
    def _load_account(self):
        if os.path.isfile(config.AUTH_FILE):
            try:
                with open(config.AUTH_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if all(k in d for k in ("username", "salt", "hash", "version")):
                    return d
            except Exception:
                pass
        salt = secrets.token_hex(16)
        d = {
            "username": config.DEFAULT_USERNAME,
            "salt": salt,
            "hash": _hash(config.DEFAULT_PASSWORD, salt),
            "version": 1,
            "created_at": _now(),
            "updated_at": "",
        }
        self._save(d)
        return d

    def _save(self, d=None):
        d = d or self.data
        self._write_private(config.AUTH_FILE,
                            json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8"))

    @property
    def username(self):
        return self.data["username"]

    def verify(self, username, password):
        ok_user = hmac.compare_digest(str(username or ""), self.data["username"])
        ok_pwd = hmac.compare_digest(_hash(str(password or ""), self.data["salt"]),
                                     self.data["hash"])
        return ok_user and ok_pwd

    def change_password(self, old, new, confirm):
        if not self.verify(self.data["username"], old):
            raise ValueError("当前密码不正确")
        if new != confirm:
            raise ValueError("两次输入的新密码不一致")
        if len(new or "") < 8:
            raise ValueError("新密码至少 8 位")
        if new == old:
            raise ValueError("新密码不能与当前密码相同")
        if not re.search(r"[A-Za-z]", new) or not re.search(r"\d", new):
            raise ValueError("新密码需同时包含字母和数字（建议再加符号）")
        salt = secrets.token_hex(16)
        self.data.update({
            "salt": salt,
            "hash": _hash(new, salt),
            "version": int(self.data.get("version", 1)) + 1,
            "updated_at": _now(),
        })
        self._save()
        return self.issue_token()  # 旧令牌因 version 变化立即失效，返回新令牌避免掉线

    # ---------- 令牌 ----------
    def issue_token(self):
        payload = {
            "u": self.data["username"],
            "v": int(self.data["version"]),
            "exp": int(time.time()) + config.SESSION_DAYS * 86400,
            "n": secrets.token_hex(6),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return _b64(raw) + "." + _b64(sig)

    def verify_token(self, token):
        try:
            a, b = str(token or "").split(".", 1)
            raw = _unb64(a)
            sig = _unb64(b)
            if not hmac.compare_digest(hmac.new(self._secret, raw, hashlib.sha256).digest(), sig):
                return False
            p = json.loads(raw.decode("utf-8"))
            return (p.get("u") == self.data["username"]
                    and int(p.get("v", -1)) == int(self.data["version"])
                    and int(p.get("exp", 0)) > int(time.time()))
        except Exception:
            return False

    def status(self, token):
        logged = self.verify_token(token)
        return {
            "ok": True,
            "auth_required": True,
            "logged_in": logged,
            "username": self.data["username"] if logged else "",
            "app_version": config.APP_VERSION,
        }
