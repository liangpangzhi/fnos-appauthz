#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用授权管家 —— HTTP 服务与 API。

以 root 身份运行（config/privilege: run-as root），通过本地端口提供管理界面。
所有写操作要求登录会话；登录态为 HMAC 签名 Cookie，密码用 PBKDF2 存储。
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import (APP_NAME, APP_VERSION, AppLog, PathError, Settings,
                    clean_path, now_str, under_roots)
from fnos import DbError, FnosBackend

COOKIE_NAME = "appauthz_sess"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


class Ctx(object):
    """全局上下文。"""

    def __init__(self, args):
        self.dest = os.path.abspath(args.dest)
        self.etc = os.path.abspath(args.etc)
        self.var = os.path.abspath(args.var)
        self.demo = bool(args.demo)
        # 仅用于本地预览界面：演示模式下可跳过登录
        self.no_auth = bool(getattr(args, "no_auth", False)) and self.demo
        self.web = os.path.join(self.dest, "web")
        os.makedirs(self.var, exist_ok=True)
        os.makedirs(self.etc, exist_ok=True)
        self.log = AppLog(self.var)
        self.settings = Settings(self.etc)
        if args.port:
            self.settings.data["port"] = int(args.port)
        if self.demo:
            from demo import DemoBackend
            self.backend = DemoBackend(self.settings, self.log, self.var)
        else:
            self.backend = FnosBackend(self.settings, self.log, self.var)
        self.fails = {}
        self.fail_lock = threading.Lock()

    def roots(self):
        """允许操作的根目录：配置值 + 展开后的实际卷（/vol -> /vol1,/vol2...）。"""
        configured = list(self.settings.get("roots") or ["/vol"])
        expanded = []
        try:
            for item in self.backend.roots():
                expanded.append(item["path"])
        except Exception:
            pass
        out = []
        for root in configured + expanded:
            if root not in out:
                out.append(root)
        return out

    def resolve_apps(self, names):
        out, missing = [], []
        for name in names or []:
            app = self.backend.app_by_name(name)
            if app:
                out.append(app)
            else:
                missing.append(name)
        return out, missing


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """浏览器提前断开连接是常态，不必打印堆栈。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        HTTPServer.handle_error(self, request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "appauthz/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---------------- 基础响应 ----------------
    def log_message(self, fmt, *args):
        try:
            self.ctx.log.info("HTTP %s %s" % (self.command, self.path))
        except Exception:
            pass

    @property
    def ctx(self):
        return self.server.ctx

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def json_ok(self, data=None, **kw):
        payload = {"ok": True}
        if data:
            payload.update(data)
        payload.update(kw)
        self._send(200, payload)

    def json_err(self, msg, code=400):
        self._send(code, {"ok": False, "error": str(msg)})

    # ---------------- 会话 ----------------
    def session_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() == COOKIE_NAME:
                    return value.strip()
        return ""

    def logged_in(self):
        # no_auth: 仅演示模式；no_login: 用户在设置里主动关闭了登录校验
        if self.ctx.no_auth or self.ctx.settings.get("no_login"):
            return True
        token = self.session_token()
        return bool(token) and self.ctx.settings.verify_session(token)

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            host = urlparse(origin).netloc
        except Exception:
            return False
        return host == (self.headers.get("Host") or "")

    # ---------------- 入口 ----------------
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) or {}
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if method == "POST" and not self.same_origin():
                    return self.json_err("跨站请求被拒绝", 403)
                return self.api(method, path, query)
            return self.static(path)
        except PathError as exc:
            self.json_err(exc)
        except DbError as exc:
            self.json_err("数据库错误：%s" % exc, 500)
        except ValueError as exc:
            self.json_err(exc)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:  # pragma: no cover
            self.ctx.log.error("处理 %s 失败：%s\n%s" % (path, exc, traceback.format_exc()))
            self.json_err("内部错误：%s" % exc, 500)

    # ---------------- 静态文件 ----------------
    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        if path.startswith("/images/"):
            base = os.path.join(self.ctx.dest, "ui", "images")
            rel = path[len("/images/"):]
        else:
            base = self.ctx.web
            rel = path.lstrip("/")
        full = os.path.realpath(os.path.join(base, rel))
        root = os.path.realpath(base)
        if not (full == root or full.startswith(root + os.sep)):
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        ctype = MIME.get(ext, "application/octet-stream")
        # 图标缓存一天，其余不缓存
        extra = {"Cache-Control": "public, max-age=86400"} if ext == ".png" else {}
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            self.send_header(key, value)
        if not extra:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------------- API 路由 ----------------
    def api(self, method, path, query):
        ctx = self.ctx
        if path == "/api/ping":
            return self.json_ok({
                "version": APP_VERSION,
                "demo": ctx.demo,
                "installed": ctx.settings.has_password(),
                "needs_setup": (not ctx.settings.has_password()
                                and not ctx.settings.get("no_login")),
                "no_login": bool(ctx.settings.get("no_login")),
                "has_password": ctx.settings.has_password(),
                "logged_in": self.logged_in(),
            })
        if path == "/api/login" and method == "POST":
            return self.api_login()
        if path == "/api/logout" and method == "POST":
            return self._send(200, {"ok": True}, extra={
                "Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE_NAME})

        if not self.logged_in():
            return self.json_err("未登录或会话已过期", 401)

        if path == "/api/state":
            return self.api_state()
        if path == "/api/browse":
            target = (query.get("path") or [""])[0]
            return self.json_ok({"data": ctx.backend.browse(target)})
        if path == "/api/grant" and method == "POST":
            return self.api_grant()
        if path == "/api/revoke" and method == "POST":
            return self.api_revoke()
        if path == "/api/copy" and method == "POST":
            return self.api_copy()
        if path == "/api/verify" and method == "POST":
            return self.api_verify()
        if path == "/api/orphans":
            return self.json_ok({"items": ctx.backend.orphans()})
        if path == "/api/orphans/purge" and method == "POST":
            return self.api_purge()
        if path == "/api/diagnose":
            data = ctx.backend.diagnose()
            return self.json_ok({"data": data})
        if path == "/api/log":
            limit = int((query.get("limit") or ["200"])[0])
            return self.json_ok({"lines": ctx.log.tail(limit)})
        if path == "/api/settings" and method == "POST":
            return self.api_settings()
        if path == "/api/password" and method == "POST":
            return self.api_password()
        return self.json_err("接口不存在", 404)

    # ---------------- 各接口实现 ----------------
    def api_login(self):
        body = self.read_body()
        ctx = self.ctx
        ip = self.client_address[0]
        with ctx.fail_lock:
            rec = ctx.fails.get(ip) or {"count": 0, "until": 0}
            if rec["until"] > time.time():
                wait = int(rec["until"] - time.time())
                return self.json_err("尝试次数过多，请 %d 秒后再试" % max(wait, 1), 429)
        password = str(body.get("password") or "")
        if not ctx.settings.has_password():
            return self.json_err("尚未初始化管理密码，请重新安装或在命令行执行 manage.py bootstrap", 409)
        if not ctx.settings.check_password(password):
            with ctx.fail_lock:
                rec = ctx.fails.setdefault(ip, {"count": 0, "until": 0})
                rec["count"] += 1
                if rec["count"] >= 5:
                    rec["count"] = 0
                    rec["until"] = time.time() + 60
            ctx.log.warn("登录失败（%s）" % ip)
            return self.json_err("密码错误", 401)
        with ctx.fail_lock:
            ctx.fails.pop(ip, None)
        token = ctx.settings.sign_session()
        ctx.log.info("登录成功（%s）" % ip)
        self._send(200, {"ok": True}, extra={
            "Set-Cookie": "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                          % (COOKIE_NAME, token, 12 * 3600)})

    def api_state(self):
        ctx = self.ctx
        apps = ctx.backend.list_apps()
        for app in apps:
            try:
                app["paths"] = ctx.backend.paths_of(app)
            except Exception as exc:
                app["paths"] = []
                app["paths_error"] = str(exc)
        schema = ctx.backend.schema()
        return self.json_ok({
            "version": APP_VERSION,
            "demo": ctx.demo,
            "apps": apps,
            "roots": ctx.backend.roots(),
            "acl_mode": ctx.backend.acl_mode(),
            "db_ok": schema.get("db_ok"),
            "db_error": schema.get("error") or "",
            "schema_ok": schema.get("ok"),
            "settings": {
                "perm_rw": ctx.settings.get("perm_rw"),
                "perm_ro": ctx.settings.get("perm_ro"),
                "acl_mode": ctx.settings.get("acl_mode"),
                "recursive": ctx.settings.get("recursive"),
                "backup": ctx.settings.get("backup"),
                "no_login": bool(ctx.settings.get("no_login")),
                "has_password": ctx.settings.has_password(),
                "roots": ctx.settings.get("roots"),
                "port": ctx.settings.get("port"),
            },
        })

    def _restart_targets(self, results):
        """对本次成功授权的应用重启一次，使新的授权目录立即生效。"""
        ctx = self.ctx
        names = []
        for item in results:
            name = item.get("app")
            if item.get("ok") and name and name not in names:
                names.append(name)
        out = []
        for name in names:
            app = ctx.backend.app_by_name(name)
            if not app:
                continue
            try:
                out.append(ctx.backend.restart_app(app))
            except Exception as exc:
                out.append({"app": name, "ok": False, "message": str(exc)})
        return out

    def _paths_from_body(self, body):
        paths = []
        if body.get("path"):
            paths.append(body["path"])
        for item in body.get("paths") or []:
            if item:
                paths.append(item)
        out = []
        for raw in paths:
            path = clean_path(raw)
            if not under_roots(path, self.ctx.roots()):
                raise PathError("路径 %s 不在允许的根目录内" % path)
            if not self.ctx.demo and not os.path.isdir(path):
                raise PathError("路径不存在或不是文件夹：%s" % path)
            out.append(path)
        if not out:
            raise PathError("请先选择或输入文件夹")
        seen, uniq = set(), []
        for path in out:
            if path not in seen:
                seen.add(path)
                uniq.append(path)
        return uniq

    def api_grant(self):
        ctx = self.ctx
        body = self.read_body()
        perm = str(body.get("perm") or "rw").lower()
        if perm not in ("rw", "ro"):
            raise ValueError("权限只能是 rw 或 ro")
        dry_run = bool(body.get("dry_run"))
        recursive = body.get("recursive")
        paths = self._paths_from_body(body)
        apps, missing = ctx.resolve_apps(body.get("apps"))
        if not apps:
            raise ValueError("请至少选择一个应用")
        if not dry_run and ctx.settings.get("backup") and not ctx.demo:
            ctx.backend.backup("grant")
        results = []
        for app in apps:
            for path in paths:
                try:
                    results.append(ctx.backend.grant_one(app, path, perm, dry_run, recursive))
                except Exception as exc:
                    results.append({"app": app.get("name"), "path": path, "ok": False,
                                    "message": str(exc), "steps": []})
        ok_count = len([r for r in results if r.get("ok")])
        restarts = []
        if body.get("restart") and not dry_run:
            restarts = self._restart_targets(results)
        if not dry_run:
            ctx.log.audit("grant", {"apps": [a["name"] for a in apps], "paths": paths,
                                    "perm": perm, "ok": ok_count, "total": len(results),
                                    "restart": [r["app"] for r in restarts]})
        return self.json_ok({"results": results, "ok_count": ok_count,
                             "total": len(results), "restarts": restarts,
                             "missing": missing, "dry_run": dry_run})

    def api_revoke(self):
        ctx = self.ctx
        body = self.read_body()
        dry_run = bool(body.get("dry_run"))
        paths = self._paths_from_body(body)
        apps, missing = ctx.resolve_apps(body.get("apps"))
        if not apps:
            raise ValueError("请至少选择一个应用")
        if not dry_run and ctx.settings.get("backup") and not ctx.demo:
            ctx.backend.backup("revoke")
        results = []
        for app in apps:
            for path in paths:
                try:
                    results.append(ctx.backend.revoke_one(app, path, dry_run))
                except Exception as exc:
                    results.append({"app": app.get("name"), "path": path, "ok": False,
                                    "message": str(exc), "steps": []})
        ok_count = len([r for r in results if r.get("ok")])
        if not dry_run:
            ctx.log.audit("revoke", {"apps": [a["name"] for a in apps], "paths": paths,
                                     "ok": ok_count, "total": len(results)})
        return self.json_ok({"results": results, "ok_count": ok_count,
                             "total": len(results), "missing": missing,
                             "dry_run": dry_run})

    def api_copy(self):
        ctx = self.ctx
        body = self.read_body()
        source = body.get("source")
        dry_run = bool(body.get("dry_run"))
        src_app = ctx.backend.app_by_name(source) if source else None
        if not src_app:
            raise ValueError("请选择来源应用")
        targets, missing = ctx.resolve_apps(body.get("targets"))
        if not targets:
            raise ValueError("请至少选择一个目标应用")
        source_paths = [p for p in ctx.backend.paths_of(src_app)]
        if not source_paths:
            raise ValueError("来源应用当前没有任何授权目录")
        if not dry_run and ctx.settings.get("backup") and not ctx.demo:
            ctx.backend.backup("copy")
        results = []
        for app in targets:
            if app["name"] == src_app["name"]:
                continue
            for item in source_paths:
                perm = item.get("perm") if item.get("perm") in ("rw", "ro") else "rw"
                if not os.path.isdir(item["path"]):
                    results.append({"app": app["name"], "path": item["path"],
                                    "ok": False, "message": "源路径已不存在，已跳过",
                                    "steps": []})
                    continue
                try:
                    results.append(ctx.backend.grant_one(app, item["path"], perm, dry_run))
                except Exception as exc:
                    results.append({"app": app["name"], "path": item["path"],
                                    "ok": False, "message": str(exc), "steps": []})
        ok_count = len([r for r in results if r.get("ok")])
        if not dry_run:
            ctx.log.audit("copy", {"source": src_app["name"],
                                   "targets": [a["name"] for a in targets],
                                   "ok": ok_count, "total": len(results)})
        return self.json_ok({"results": results, "ok_count": ok_count,
                             "total": len(results), "missing": missing,
                             "source_paths": len(source_paths), "dry_run": dry_run})

    def api_verify(self):
        ctx = self.ctx
        body = self.read_body()
        apps, missing = ctx.resolve_apps(body.get("apps"))
        if not apps:
            raise ValueError("请至少选择一个应用")
        paths = self._paths_from_body(body)
        results = []
        for app in apps:
            for path in paths:
                try:
                    results.append(ctx.backend.verify(app, path))
                except Exception as exc:
                    results.append({"app": app.get("name"), "path": path,
                                    "checks": [], "error": str(exc)})
        return self.json_ok({"results": results, "missing": missing})

    def api_purge(self):
        ctx = self.ctx
        body = self.read_body()
        items = body.get("items") or []
        if not items:
            raise ValueError("没有选中任何记录")
        if ctx.settings.get("backup") and not ctx.demo:
            ctx.backend.backup("purge")
        result = ctx.backend.purge_orphans(items)
        ctx.log.audit("purge", {"count": len(items), "result": result.get("deleted")})
        return self.json_ok({"result": result})

    def api_settings(self):
        ctx = self.ctx
        body = self.read_body()
        update = {}
        for key, caster in (("perm_rw", int), ("perm_ro", int),
                            ("recursive", bool), ("backup", bool),
                            ("no_login", bool)):
            if key in body:
                try:
                    update[key] = caster(body[key])
                except Exception:
                    raise ValueError("%s 取值非法" % key)
        if "acl_mode" in body:
            mode = str(body["acl_mode"])
            if mode not in ("auto", "trimacl", "setfacl"):
                raise ValueError("ACL 模式只能是 auto / trimacl / setfacl")
            update["acl_mode"] = mode
        if "roots" in body:
            roots = [str(r).strip() for r in (body["roots"] or []) if str(r).strip()]
            for root in roots:
                if not root.startswith("/"):
                    raise ValueError("根目录必须是绝对路径")
            update["roots"] = roots or ["/vol"]
        if "port" in body:
            port = int(body["port"])
            if not (1024 <= port <= 65535):
                raise ValueError("端口需在 1024-65535 之间")
            update["port"] = port
        if not update:
            raise ValueError("没有需要保存的设置")
        old_port = int(ctx.settings.get("port") or 0)
        ctx.settings.set(**update)
        ctx.log.audit("settings", update)
        need_restart = int(update.get("port", old_port)) != old_port
        return self.json_ok({
            "restart_required": need_restart,
            "message": "已保存，端口将在重启应用后生效（应用中心 → 停止 → 启动）"
                       if need_restart else "已保存",
        })

    def api_password(self):
        ctx = self.ctx
        body = self.read_body()
        old = str(body.get("old") or "")
        new = str(body.get("new") or "")
        if not ctx.settings.get("no_login") and not ctx.settings.check_password(old):
            return self.json_err("原密码不正确", 403)
        if len(new) < 6:
            raise ValueError("新密码至少 6 位")
        ctx.settings.set_password(new)
        ctx.log.audit("password", {"result": "changed"})
        return self.json_ok({"message": "密码已更新，请重新登录"})


def manifest_version(dest):
    """从包的 manifest 读出真实版本号，避免升级后界面仍显示旧版本。"""
    name = os.environ.get("TRIM_APPNAME") or APP_NAME
    candidates = [
        os.path.join("/var/apps", name, "manifest"),   # 安装后的位置
        os.path.join(dest, "manifest"),                # 开发目录
        os.path.join(dest, "..", "manifest"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip().startswith("version"):
                        value = line.split("=", 1)[1].strip()
                        if value:
                            return value
        except Exception:
            continue
    return ""


def main():
    parser = argparse.ArgumentParser(description="应用授权管家")
    parser.add_argument("--dest", default=os.environ.get("TRIM_APPDEST", os.getcwd()))
    parser.add_argument("--etc", default=os.environ.get("TRIM_PKGETC",
                                                        "/var/apps/appauthz/etc"))
    parser.add_argument("--var", default=os.environ.get("TRIM_PKGVAR",
                                                        "/var/apps/appauthz/var"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-auth", dest="no_auth", action="store_true",
                        help="仅限演示模式：跳过登录，便于本地预览界面")
    args = parser.parse_args()

    ctx = Ctx(args)

    # 版本号以 manifest 为准
    live_version = manifest_version(ctx.dest)
    if live_version and live_version != APP_VERSION:
        globals()["APP_VERSION"] = live_version
        Handler.server_version = "%s/%s" % (APP_NAME, live_version)

    port = int(args.port or ctx.settings.get("port") or 18080)
    httpd = ThreadedHTTPServer((args.host, port), Handler)
    httpd.ctx = ctx

    pidfile = os.path.join(ctx.var, "%s.pid" % APP_NAME)
    try:
        with open(pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass

    ctx.log.info("服务启动：%s:%s（%s 模式）dest=%s etc=%s var=%s"
                 % (args.host, port, "演示" if ctx.demo else "fnOS",
                    ctx.dest, ctx.etc, ctx.var))

    def shutdown(signum, frame):
        ctx.log.info("收到信号 %s，退出" % signum)
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            os.remove(pidfile)
        except Exception:
            pass
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except Exception:
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        try:
            os.remove(pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()
