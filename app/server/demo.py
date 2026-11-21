#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示后端：在没有 fnOS 的机器上跑通界面与流程（不触碰任何系统资源）。

用法： python app.py --demo
"""
import os
import random

from common import PathError, clean_path, human_size, under_roots


class DemoBackend(object):
    platform = "demo"

    def __init__(self, settings, log, var_dir=None):
        self.s = settings
        self.log = log
        self.var_dir = var_dir or os.getcwd()
        self.is_root = False
        self._apps = [
            {"name": "trim.media", "display": "影视", "version": "0.8.32",
             "uid": 996, "gid": 996, "uname": "trim-media", "app_id": "1",
             "builtin": True},
            {"name": "qbittorrent", "display": "qBittorrent", "version": "4.6.2",
             "uid": 982, "gid": 982, "uname": "qbittorrent", "app_id": "27",
             "builtin": False},
            {"name": "alist", "display": "AList", "version": "3.29.2",
             "uid": 1010, "gid": 1010, "uname": "alist", "app_id": "41",
             "builtin": False},
            {"name": "immich", "display": "Immich", "version": "1.106.4",
             "uid": 1011, "gid": 1011, "uname": "immich", "app_id": "52",
             "builtin": False},
            {"name": "syncthing", "display": "Syncthing", "version": "1.27.2",
             "uid": 1012, "gid": 1012, "uname": "syncthing", "app_id": None,
             "builtin": False},
        ]
        self._perm = {}
        seed = [
            ("trim.media", "/vol1/1000/电影", 6),
            ("trim.media", "/vol1/1000/剧集", 6),
            ("trim.media", "/vol2/1000-1-4b2d2ad8", 4),
            ("qbittorrent", "/vol1/1000/下载", 6),
            ("alist", "/vol1/1000/下载", 4),
        ]
        for name, path, perm in seed:
            self._perm[(name, path)] = perm

    # -- 与应用枚举保持同构 ------------------------------------------------
    def list_apps(self, include_self=False):
        return [dict(a) for a in self._apps]

    def app_by_name(self, name):
        for app in self._apps:
            if app["name"] == name:
                return dict(app)
        return None

    def perm_label(self, perm):
        if perm is None:
            return "db-only"
        try:
            value = int(perm)
        except Exception:
            return "other(%s)" % perm
        if value == int(self.s.get("perm_rw")):
            return "rw"
        if value == int(self.s.get("perm_ro")):
            return "ro"
        return "other(%s)" % value

    def paths_of(self, app):
        out = []
        for (name, path), perm in sorted(self._perm.items()):
            if name != app["name"]:
                continue
            out.append({"path": path, "perm": self.perm_label(perm),
                        "perm_raw": perm, "exists": True, "acl": True})
        return out

    def grant_one(self, app, path, perm, dry_run=False, recursive=None):
        value = int(self.s.get("perm_rw") if perm == "rw" else self.s.get("perm_ro"))
        spec = "rwxpdDaAeEc" if perm == "rw" else "rx"
        res = {"app": app.get("name"), "display": app.get("display"), "path": path,
               "perm": perm, "ok": True, "steps": [
                   {"kind": "acl", "cmd": "trimacl -m a:u:%s:%s:sfd %s"
                    % (app.get("uid"), spec, path), "rc": 0},
                   {"kind": "db", "cmd": "psql -d appcenter",
                    "sql": "INSERT ... -- demo"},
               ], "message": "（演示模式）已记录"}
        if not dry_run:
            self._perm[(app["name"], path)] = value
            self.log.info("demo grant %s -> %s" % (app["name"], path))
        return res

    def revoke_one(self, app, path, dry_run=False):
        res = {"app": app.get("name"), "display": app.get("display"), "path": path,
               "ok": True, "steps": [{"kind": "acl", "cmd": "trimacl -x a:u:%s %s"
                                      % (app.get("uid"), path), "rc": 0}],
               "message": "（演示模式）已撤销"}
        if not dry_run:
            self._perm.pop((app["name"], path), None)
        return res

    def restart_app(self, app):
        return {"app": app.get("name"), "ok": True,
                "message": "（演示模式）已重启"}

    def verify(self, app, path):
        return {"app": app.get("name"), "path": path,
                "user": app.get("uname"), "acl": "rwx",
                "checks": [{"label": "读取", "ok": True},
                           {"label": "写入", "ok": random.random() > 0.15},
                           {"label": "进入目录", "ok": True}]}

    def orphans(self):
        return [{"table": "authorization_path", "row_id": "12", "app_id": None,
                 "app_name": "trim-media", "uid": 996, "uname": "trim-media",
                 "path": "/vol02/1000-1-00fd125e", "reason": "路径已不存在",
                 "perm": "rw"},
                {"table": "app_authorization_path", "row_id": "21", "app_id": "41",
                 "app_name": "alist", "uid": None, "uname": None,
                 "path": "/vol2/1000-1-214faa07", "reason": "路径已不存在"}]

    def purge_orphans(self, items):
        return {"deleted": len(items), "detail": "（演示模式）"}

    def backup(self, reason="op"):
        return None

    def schema(self, force=False):
        return {"ok": True, "db_ok": True, "error": "",
                "app_path_table": "app_authorization_path",
                "path_table": "authorization_path", "app_table": "app",
                "app_name_col": "app_name", "path_table_has_type": True,
                "app_path_has_id": True, "path_table_has_id": True,
                "tables": ["app", "app_authorization_path", "authorization_path"]}

    def tools(self):
        return {"trimacl": "/usr/bin/trimacl", "setfacl": "/usr/bin/setfacl",
                "getfacl": "/usr/bin/getfacl", "psql": "/usr/bin/psql",
                "pg_dump": "/usr/bin/pg_dump", "runuser": "/usr/sbin/runuser",
                "python3": "/usr/bin/python3"}

    def acl_mode(self):
        return "trimacl"

    def roots(self):
        return [{"path": "/vol1", "label": "/vol1"},
                {"path": "/vol2", "label": "/vol2"}]

    def browse(self, path):
        if not path:
            return {"path": "", "parent": None, "roots": self.roots(), "dirs": []}
        path = clean_path(path)
        allowed = [r["path"] for r in self.roots()] + ["/vol"]
        if not under_roots(path, allowed):
            raise PathError("该目录不在允许范围内")
        if path == "/vol1":
            dirs = [{"name": n, "path": "/vol1/" + n, "empty": False}
                    for n in ("1000", "1001", "@appdata", "@appshare")]
        elif path == "/vol1/1000":
            dirs = [{"name": n, "path": "/vol1/1000/" + n, "empty": False}
                    for n in ("电影", "剧集", "下载", "音乐", "照片", "备份")]
        else:
            dirs = [{"name": "子目录示例", "path": path + "/子目录示例", "empty": True}]
        return {"path": path, "parent": "/vol1" if path != "/vol1" else "",
                "roots": self.roots(), "dirs": dirs}

    def diagnose(self):
        return {"platform": "demo", "is_root": False, "tools": self.tools(),
                "acl_mode": "trimacl", "schema": self.schema(), "db_ok": True,
                "db_error": "", "settings": {
                    "port": self.s.get("port"), "perm_rw": self.s.get("perm_rw"),
                    "perm_ro": self.s.get("perm_ro"),
                    "acl_mode": self.s.get("acl_mode"),
                    "recursive": self.s.get("recursive"),
                    "backup": self.s.get("backup"),
                    "roots": self.s.get("roots")},
                "perm_stats": [{"perm": "6", "label": "rw", "type": "appuser", "count": "18"},
                               {"perm": "4", "label": "ro", "type": "appuser", "count": "7"}],
                "volumes": [{"path": "/vol1", "total": "3.6TB", "free": "1.2TB"},
                            {"path": "/vol2", "total": "1.8TB", "free": "640.0GB"}]}
