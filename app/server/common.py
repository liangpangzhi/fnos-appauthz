#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用工具：配置读写、日志、口令散列、路径校验。

仅使用 Python 标准库，兼容 Python 3.6+（fnOS 自带 3.11）。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

APP_NAME = "appauthz"
APP_VERSION = "1.0.6"

DEFAULT_SETTINGS = {
    "version": 1,
    "port": 18080,
    "secret": "",
    "admin": {},
    "acl_mode": "auto",          # auto | trimacl | setfacl
    "perm_rw": 6,                # appcenter.authorization_path.perm 的读写取值
    "perm_ro": 4,                # 只读取值
    "roots": ["/vol"],           # 允许操作的目录根
    "recursive": False,          # 默认不递归套用 ACL
    "backup": True,              # 每次写入前备份相关数据表
    "no_login": False,           # 免登录：不校验管理密码（谨慎开启）
    "created_at": "",
    "updated_at": "",
}

SAFE_PATH_RE = re.compile(r"^/[\x20-\x7e\u4e00-\u9fff\w\-.\u0080-\uffff/@]*$")


# --------------------------------------------------------------------------
# 基础 IO
# --------------------------------------------------------------------------
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, data, mode=0o600):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class Settings(object):
    def __init__(self, etc_dir):
        self.etc_dir = etc_dir
        self.path = os.path.join(etc_dir, "settings.json")
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_SETTINGS)
        self._mtime = 0.0
        self.reload()

    def reload(self):
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = 0.0
        loaded = read_json(self.path, {}) or {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update(loaded or {})
        self.data = merged
        return self.data

    def save(self):
        with self.lock:
            self.data["updated_at"] = now_str()
            write_json(self.path, self.data)

    def _sync_from_disk(self):
        """命令行（manage.py）改过配置时，运行中的进程要能立刻看到。"""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            self.reload()

    def get(self, key, default=None):
        self._sync_from_disk()
        return self.data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                if key in DEFAULT_SETTINGS:
                    self.data[key] = value
            self.data["updated_at"] = now_str()
            write_json(self.path, self.data)

    # --- 口令 ---------------------------------------------------------
    def set_password(self, password):
        salt = secrets.token_hex(16)
        iterations = 200000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     salt.encode("ascii"), iterations)
        self.data["admin"] = {
            "algo": "pbkdf2_sha256",
            "iterations": iterations,
            "salt": salt,
            "hash": digest.hex(),
            "updated_at": now_str(),
        }
        self.save()

    def has_password(self):
        return bool((self.data.get("admin") or {}).get("hash"))

    def check_password(self, password):
        admin = self.data.get("admin") or {}
        if not admin.get("hash"):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     admin.get("salt", "").encode("ascii"),
                                     int(admin.get("iterations", 200000)))
        return hmac.compare_digest(digest.hex(), admin.get("hash", ""))

    # --- 会话 ---------------------------------------------------------
    def ensure_secret(self):
        if not self.data.get("secret"):
            self.data["secret"] = secrets.token_hex(32)
            self.save()
        return self.data["secret"]

    def sign_session(self, ttl_hours=12):
        secret = self.ensure_secret().encode("ascii")
        payload = {"u": "admin", "exp": int(time.time()) + ttl_hours * 3600}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
        return "%s.%s" % (body, mac)

    def verify_session(self, token):
        try:
            body, mac = token.split(".", 1)
            secret = (self.data.get("secret") or "").encode("ascii")
            if not secret:
                return False
            expect = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect, mac):
                return False
            pad = "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
            return int(payload.get("exp", 0)) > time.time()
        except Exception:
            return False


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
class AppLog(object):
    def __init__(self, var_dir, name=APP_NAME):
        self.var_dir = var_dir
        self.log_path = os.path.join(var_dir, "%s.log" % name)
        self.audit_path = os.path.join(var_dir, "audit.log")
        self.buf = []
        self.lock = threading.Lock()

    def _write(self, level, msg):
        line = "[%s] %-5s %s" % (now_str(), level, msg)
        with self.lock:
            self.buf.append(line)
            if len(self.buf) > 500:
                del self.buf[: len(self.buf) - 500]
        try:
            os.makedirs(self.var_dir, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        return line

    def info(self, msg):
        return self._write("INFO", msg)

    def warn(self, msg):
        return self._write("WARN", msg)

    def error(self, msg):
        return self._write("ERROR", msg)

    def audit(self, action, detail):
        rec = {"ts": now_str(), "action": action, "detail": detail}
        self.info("AUDIT %s %s" % (action, json.dumps(detail, ensure_ascii=False)))
        try:
            os.makedirs(self.var_dir, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    def tail(self, limit=200):
        lines = []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except Exception:
            lines = list(self.buf)
        return lines[-limit:]

    def tail_audit(self, limit=100):
        lines = []
        try:
            with open(self.audit_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out


# --------------------------------------------------------------------------
# 路径校验
# --------------------------------------------------------------------------
class PathError(Exception):
    pass


def posix_norm(path):
    """平台无关的 POSIX 路径归一（不用 os.path.normpath，避免 Windows 下反斜杠）。"""
    raw = str(path).replace("\\", "/")
    parts = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


def clean_path(raw):
    """把用户输入整理成规范绝对路径，并做基本安全检查。"""
    if raw is None:
        raise PathError("路径不能为空")
    path = str(raw).strip()
    if not path:
        raise PathError("路径不能为空")
    if "\x00" in path or "\n" in path or "\r" in path:
        raise PathError("路径包含非法字符")
    if "'" in path or '"' in path:
        # 数据库写入会用到字符串常量，直接拒绝含引号的路径
        raise PathError("路径不能包含引号")
    if not path.startswith("/"):
        raise PathError("请输入绝对路径（以 / 开头）")
    path = posix_norm(path)
    if path == "/":
        raise PathError("不能对根目录操作")
    return path


# fnOS 的存储卷是 /vol1、/vol2、/vol00 这种（不是 /vol 的子目录），
# 因此允许用 /vol 表示“所有卷”。
VOLUME_PATH_RE = re.compile(r"^/vol(?:ume)?\d+(?:/|$)")


def under_roots(path, roots):
    candidates = [posix_norm(path)]
    try:
        real = os.path.realpath(path)
        if real.startswith("/"):  # 仅在 POSIX 上做符号链接逃逸检查
            candidates.append(posix_norm(real))
    except Exception:
        pass
    for root in roots or []:
        base = posix_norm(root)
        for cand in candidates:
            if cand == base or cand.startswith(base + "/"):
                return True
            # /vol 作为“所有卷”的通配根：/vol1、/vol2、/vol00 …
            if base in ("/vol", "/volume") and VOLUME_PATH_RE.match(cand):
                return True
    return False


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return "%.1f%s" % (num, unit)
        num /= 1024.0
    return "%.1fPB" % num
