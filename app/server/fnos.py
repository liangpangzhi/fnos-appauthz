#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS 授权后端（Linux）。

职责：
  1. 枚举已安装应用，解析其系统账户（uid/gid）与 appcenter 中的 app_id；
  2. 读取/写入应用可访问目录：
       - 文件系统层：trimacl（fnOS 1.2+ 的 Windows ACL 工具）或 setfacl
       - 数据库层：PostgreSQL appcenter 库
         * app_authorization_path(app_id, path, ...)
         * authorization_path(path, user_group_id, perm, type='appuser', ...)
  3. 以应用账户身份实测读写权限；
  4. 巡检失效路径（卷/网盘卸载后遗留的“死授权”）并提供清理。

所有写操作前会先备份相关数据表记录，SQL 全部在事务中执行。
"""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

try:
    import pwd  # 仅 Unix 可用；Windows 上用于本地 UI 调试时缺失
except ImportError:  # pragma: no cover
    pwd = None

from common import (PathError, clean_path, human_size, now_str,
                    posix_norm, under_roots)

DB_NAME = "appcenter"
KNOWN_COLS = {"id", "app_id", "path", "user_group_id", "perm", "trimacl_perm",
              "type", "created_at", "updated_at"}

# fnOS 把 trimacl 放在 /usr/trim/bin，不在普通用户的 PATH 里
EXTRA_BIN_DIRS = ("/usr/trim/bin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
                  "/sbin", "/bin")
SQL_RESERVED = ("'", "\n", "\r")


class DbError(Exception):
    pass


def sh_join(argv):
    try:
        return " ".join(shlex.quote(str(a)) for a in argv)
    except Exception:
        return " ".join(str(a) for a in argv)


def sqlq(value):
    if any(tok in str(value) for tok in SQL_RESERVED):
        raise DbError("参数包含非法字符，已拒绝执行 SQL")
    return "'" + str(value).replace("\\", "\\\\") + "'"


class FnosBackend(object):
    platform = "fnos"

    def __init__(self, settings, log, var_dir):
        self.s = settings
        self.log = log
        self.var_dir = var_dir
        self.lock = threading.RLock()
        self._schema = None
        self._schema_at = 0
        self._meta = {}
        self._meta_at = 0
        self._tools = None

    # ------------------------------------------------------------------
    # 进程执行
    # ------------------------------------------------------------------
    def run(self, argv, timeout=90, stdin_data=None, cwd="/"):
        env = dict(os.environ)
        env.update({"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
                    "PGCLIENTENCODING": "UTF8"})
        try:
            proc = subprocess.Popen(
                [str(a) for a in argv], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, env=env)
            out, err = proc.communicate(
                input=(stdin_data.encode("utf-8") if stdin_data else None),
                timeout=timeout)
            return (proc.returncode,
                    out.decode("utf-8", "replace"),
                    err.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return 124, "", "命令执行超时（%ss）" % timeout
        except FileNotFoundError:
            return 127, "", "命令不存在：%s" % argv[0]
        except Exception as exc:  # pragma: no cover
            return 1, "", str(exc)

    def _which(self, name):
        """在 PATH 之外再查 fnOS 的私有目录（trimacl 就在 /usr/trim/bin）。"""
        found = shutil.which(name)
        if found:
            return found
        for base in EXTRA_BIN_DIRS:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------
    @property
    def is_root(self):
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    def _psql_bin(self):
        return self._which("psql") or "/usr/bin/psql"

    def _psql_argv(self, db=DB_NAME):
        psql = self._psql_bin()
        base = []
        if self.is_root:
            base = ["runuser", "-u", "postgres", "--"]
        return base + [psql, "-X", "-q", "-A", "-t", "-F", "\t",
                       "-v", "ON_ERROR_STOP=1", "-d", db]

    def psql_rows(self, sql, db=DB_NAME, timeout=60):
        rc, out, err = self.run(self._psql_argv(db) + ["-c", sql], timeout=timeout)
        if rc != 0:
            raise DbError((err or out or "psql 执行失败").strip())
        rows = []
        for line in out.splitlines():
            if line == "":
                continue
            rows.append(line.split("\t"))
        return rows

    def psql_script(self, script, db=DB_NAME, timeout=90):
        argv = self._psql_argv(db) + ["-f", "-"]
        rc, out, err = self.run(argv, timeout=timeout, stdin_data=script)
        if rc != 0:
            raise DbError((err or out or "psql 执行失败").strip())
        return out

    def db_ok(self):
        try:
            rows = self.psql_rows("SELECT 1", timeout=15)
            return bool(rows)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 结构探测
    # ------------------------------------------------------------------
    def table_meta(self, force=False):
        if self._meta and not force and time.time() - self._meta_at < 120:
            return self._meta
        sql = ("SELECT table_name, column_name, is_nullable, "
               "COALESCE(column_default,'') FROM information_schema.columns "
               "WHERE table_schema='public' ORDER BY table_name, ordinal_position")
        rows = self.psql_rows(sql, timeout=30)
        meta = {}
        for row in rows:
            if len(row) < 4:
                continue
            table, column, nullable, default = row[0], row[1], row[2], row[3]
            meta.setdefault(table, []).append(
                {"column": column, "nullable": nullable == "YES",
                 "default": default})
        self._meta = meta
        self._meta_at = time.time()
        return meta

    def schema(self, force=False):
        if self._schema and not force and time.time() - self._schema_at < 120:
            return self._schema
        info = {"ok": False, "error": "", "app_path_table": None,
                "path_table": None, "app_table": None, "app_name_col": None,
                "path_table_has_type": False, "app_path_has_id": False,
                "path_table_has_id": False, "db_ok": False, "tables": []}
        try:
            if not self.db_ok():
                info["error"] = "无法连接 appcenter 数据库（请确认 PostgreSQL 正常运行）"
                self._schema = info
                self._schema_at = time.time()
                return info
            info["db_ok"] = True
            meta = self.table_meta(force=force)
            info["tables"] = sorted(meta.keys())
            cols = {t: {c["column"] for c in cs} for t, cs in meta.items()}
            for table, cs in sorted(cols.items()):
                if info["app_path_table"] is None and {"app_id", "path"} <= cs:
                    info["app_path_table"] = table
                if info["path_table"] is None and {"path", "user_group_id", "perm"} <= cs:
                    info["path_table"] = table
            used = {info["app_path_table"], info["path_table"]}
            prefer = ["app", "apps", "app_info", "app_detail", "app_list"]
            name_cols = ["app_name", "appname", "package_name", "pkg_name", "name"]
            for table in prefer + sorted(cols.keys()):
                if table in used or table not in cols or "id" not in cols[table]:
                    continue
                for col in name_cols:
                    if col in cols[table]:
                        info["app_table"] = table
                        info["app_name_col"] = col
                        break
                if info["app_table"]:
                    break
            if info["path_table"]:
                info["path_table_has_type"] = "type" in cols[info["path_table"]]
                info["path_table_has_id"] = "id" in cols[info["path_table"]]
            if info["app_path_table"]:
                info["app_path_has_id"] = "id" in cols[info["app_path_table"]]
            info["ok"] = bool(info["app_path_table"] and info["path_table"])
            if not info["ok"]:
                info["error"] = "未能在 appcenter 库中定位授权数据表，可能系统版本结构不同"
        except Exception as exc:
            info["error"] = str(exc)
        self._schema = info
        self._schema_at = time.time()
        return info

    def _check_writable(self, table):
        """确保我们只写已知列，且没有未知的必填列会导致插入失败。"""
        unknown = []
        for col in self.table_meta().get(table, []):
            name = col["column"]
            if name in KNOWN_COLS:
                continue
            if not col["nullable"] and not col["default"]:
                unknown.append(name)
        if unknown:
            raise DbError("数据表 %s 存在未知必填列 %s，为安全起见已中止写入"
                          % (table, ",".join(unknown)))

    def _ts_cols(self, table):
        cols = {c["column"] for c in self.table_meta().get(table, [])}
        return [c for c in ("created_at", "updated_at") if c in cols]

    # ------------------------------------------------------------------
    # 应用枚举
    # ------------------------------------------------------------------
    @staticmethod
    def _plain_name(value):
        """过滤掉 fnOS 未替换的 i18n 占位符（如 "${common.display_name}"）。"""
        text = str(value or "").strip().strip('"').strip()
        if not text or "${" in text or text.startswith("appcgi."):
            return ""
        return text

    @classmethod
    def _i18n_display_name(cls, raw):
        """从 app.i18n_matadata 里取 zh-CN 的 DisplayName。"""
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        for locale in ("zh-CN", "zh_CN", "zh", "en-US", "en"):
            entry = data.get(locale)
            if isinstance(entry, dict):
                name = cls._plain_name(entry.get("DisplayName"))
                if name:
                    return name
        for entry in data.values():
            if isinstance(entry, dict):
                name = cls._plain_name(entry.get("DisplayName"))
                if name:
                    return name
        return ""

    def db_app_map(self):
        """app_id -> {app_name, name, version, no_authz}；失败返回空表。"""
        sc = self.schema()
        out = {}
        if not sc.get("app_table"):
            return out
        table = sc["app_table"]
        cols = {c["column"] for c in self.table_meta().get(table, [])}
        select = ["id", sc["app_name_col"]]
        for extra in ("name", "display_name", "version", "disable_authorization_path",
                      "i18n_matadata"):
            if extra in cols and extra not in select:
                select.append(extra)
        try:
            rows = self.psql_rows("SELECT %s FROM %s" % (", ".join(select), table))
        except Exception as exc:
            self.log.warn("读取应用列表失败：%s" % exc)
            return out
        for row in rows:
            if len(row) < 2:
                continue
            rec = {"id": row[0], "app_name": row[1], "name": "", "version": "",
                   "no_authz": False, "i18n_name": ""}
            for idx, col in enumerate(select[2:], start=2):
                if idx >= len(row):
                    continue
                value = row[idx]
                if col in ("name", "display_name"):
                    rec["name"] = value
                elif col == "i18n_matadata":
                    rec["i18n_name"] = self._i18n_display_name(value)
                elif col == "version":
                    rec["version"] = value
                elif col == "disable_authorization_path":
                    rec["no_authz"] = str(value).lower() in ("t", "true", "1")
            out[str(row[0])] = rec
        return out

    def resolve_user(self, appname):
        if pwd is None:
            return None, None, None
        candidates = [appname,
                      appname.replace(".", "-"),
                      appname.replace("_", "-"),
                      appname.replace(".", "-").replace("_", "-"),
                      appname.lower(),
                      appname.lower().replace(".", "-").replace("_", "-")]
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            try:
                pw = pwd.getpwnam(cand)
                return pw.pw_uid, pw.pw_gid, pw.pw_name
            except KeyError:
                continue
        return None, None, None

    def read_manifest(self, path):
        data = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip().lstrip("\ufeff")
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip()] = value.strip()
        except Exception:
            return {}
        return data

    def list_apps(self, include_self=False):
        apps_dir = "/var/apps"
        db_map = self.db_app_map()
        by_name = {}
        for app_id, rec in db_map.items():
            if rec.get("app_name"):
                by_name[rec["app_name"]] = app_id
        result = []
        try:
            names = sorted(os.listdir(apps_dir))
        except Exception:
            names = []
        for name in names:
            base = os.path.join(apps_dir, name)
            manifest_path = os.path.join(base, "manifest")
            if not os.path.isfile(manifest_path):
                continue
            if name == "appauthz" and not include_self:
                continue
            manifest = self.read_manifest(manifest_path)
            uid, gid, uname = self.resolve_user(name)
            app_id = by_name.get(name)
            rec = db_map.get(app_id, {}) if app_id else {}
            display = (self._plain_name(manifest.get("display_name"))
                       or self._plain_name(rec.get("name"))
                       or self._plain_name(rec.get("i18n_name"))
                       or name)
            result.append({
                "name": name,
                "display": display,
                "version": manifest.get("version") or rec.get("version") or "",
                "platform": manifest.get("platform", ""),
                "uid": uid,
                "gid": gid,
                "uname": uname,
                "app_id": app_id,
                "builtin": name.startswith("trim."),
                "no_authz": bool(rec.get("no_authz")),
                "install_type": manifest.get("install_type", ""),
            })
        result.sort(key=lambda a: (a["app_id"] is None, a["display"].lower()))
        return result

    def app_by_name(self, name):
        for app in self.list_apps(include_self=True):
            if app["name"] == name:
                return app
        return None

    # ------------------------------------------------------------------
    # 授权读取
    # ------------------------------------------------------------------
    def perm_label(self, perm):
        try:
            value = int(perm)
        except Exception:
            return "未知(%s)" % perm
        if value == int(self.s.get("perm_rw")):
            return "rw"
        if value == int(self.s.get("perm_ro")):
            return "ro"
        return "other(%s)" % value

    def paths_of(self, app):
        """返回 [{path, perm, exists, source}]，合并 DB 两张表的记录。"""
        sc = self.schema()
        uid = app.get("uid")
        app_id = app.get("app_id")
        perms = {}
        if uid is not None and sc.get("path_table"):
            table = sc["path_table"]
            where = "user_group_id = %d" % int(uid)
            if sc.get("path_table_has_type"):
                where += " AND type = 'appuser'"
            try:
                for row in self.psql_rows(
                        "SELECT path, perm FROM %s WHERE %s" % (table, where)):
                    if len(row) >= 2:
                        perms[row[0]] = row[1]
            except Exception as exc:
                self.log.warn("读取 %s 授权失败：%s" % (app.get("name"), exc))
        paths = set(perms.keys())
        if app_id is not None and sc.get("app_path_table"):
            try:
                for row in self.psql_rows(
                        "SELECT path FROM %s WHERE app_id = %s"
                        % (sc["app_path_table"], int(app_id))):
                    if row:
                        paths.add(row[0])
            except Exception as exc:
                self.log.warn("读取 %s 授权失败：%s" % (app.get("name"), exc))
        out = []
        for path in sorted(paths):
            perm = perms.get(path)
            out.append({
                "path": path,
                "perm": self.perm_label(perm) if perm is not None else "db-only",
                "perm_raw": perm,
                "exists": os.path.exists(path),
                "acl": perm is not None,
            })
        return out

    def trimacl_aces(self, path):
        """解析 `trimacl <path>` 的输出，返回 [{index, allow, type, id, permset}]。"""
        tool = self._which("trimacl")
        if not tool:
            return []
        rc, out, err = self.run([tool, path], timeout=20)
        if rc != 0:
            return []
        aces = []
        for line in out.splitlines():
            line = line.strip().lstrip("*").strip()
            match = re.match(r"(\d+)\s+<(\w+)>\s+(\w+):(\d+)\s+permset:(\S+)", line)
            if match:
                aces.append({"index": int(match.group(1)),
                             "allow": match.group(2),
                             "type": match.group(3),
                             "id": match.group(4),
                             "permset": match.group(5)})
        return aces

    def acl_summary(self, path, uid):
        """读出该 uid 的实际 ACL 条目。trimacl 优先，其次 getfacl。失败返回 None。"""
        if self.acl_mode() == "trimacl":
            for ace in self.trimacl_aces(path):
                if ace["type"] == "uid" and ace["id"] == str(uid):
                    return ace["permset"]
            return "none"
        tool = self._which("getfacl")
        if not tool:
            return None
        rc, out, err = self.run([tool, "-p", path], timeout=20)
        if rc != 0:
            return None
        entry = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("user:%s:" % uid):
                entry = line.split(":", 2)[2]
        return "none" if entry is None else entry

    # ------------------------------------------------------------------
    # 命令构造
    # ------------------------------------------------------------------
    def acl_mode(self):
        mode = str(self.s.get("acl_mode") or "auto").lower()
        if mode in ("trimacl", "setfacl"):
            return mode
        if self._which("trimacl"):
            return "trimacl"
        if self._which("setfacl"):
            return "setfacl"
        return "none"

    def acl_commands(self, uid, path, perm, recursive=False):
        """构造授权命令。

        注意：trimacl 不支持 -R，但它的 ACE 带 sfd 继承标志，
        会由文件系统自动作用于子文件与子目录，因此无需递归。
        """
        mode = self.acl_mode()
        if mode == "trimacl":
            tool = self._which("trimacl") or "trimacl"
            spec = "rwxpdDaAeEc" if perm == "rw" else "rx"
            return [[tool, "-m", "a:u:%d:%s:sfd" % (int(uid), spec), path]]
        if mode == "setfacl":
            tool = self._which("setfacl") or "setfacl"
            perms = "rwx" if perm == "rw" else "r-x"
            rec = ["-R"] if recursive else []
            cmds = [[tool] + rec + ["-m", "u:%d:%s" % (int(uid), perms), path]]
            if os.path.isdir(path) and not recursive:
                cmds.append([tool, "-m", "d:u:%d:%s" % (int(uid), perms), path])
            return cmds
        return []

    def revoke_commands(self, uid, path):
        """trimacl 的 -x 按 ACE 序号删除，需要先列出当前 ACE 找到序号。"""
        mode = self.acl_mode()
        if mode == "trimacl":
            tool = self._which("trimacl") or "trimacl"
            cmds = []
            for ace in self.trimacl_aces(path):
                if ace["type"] == "uid" and ace["id"] == str(uid):
                    cmds.append([tool, "-x%d" % ace["index"], path])
            return cmds
        if mode == "setfacl":
            tool = self._which("setfacl") or "setfacl"
            cmds = []
            if os.path.isdir(path):
                cmds.append([tool, "-x", "d:u:%d" % int(uid), path])
            cmds.append([tool, "-x", "u:%d" % int(uid), path])
            return cmds
        return []

    def grant_sql(self, app_id, uid, path, perm):
        sc = self.schema()
        if not sc.get("ok"):
            return None
        perm_value = int(self.s.get("perm_rw") if perm == "rw" else self.s.get("perm_ro"))
        ap, pt = sc["app_path_table"], sc["path_table"]
        lines = ["BEGIN;"]
        if app_id is not None:
            self._check_writable(ap)
            ts = self._ts_cols(ap)
            cols = ["app_id", "path"] + ts
            vals = [str(int(app_id)), sqlq(path)] + ["now()"] * len(ts)
            cond = "app_id = %d AND path = %s" % (int(app_id), sqlq(path))
            lines.append("INSERT INTO %s (%s) SELECT %s WHERE NOT EXISTS "
                         "(SELECT 1 FROM %s WHERE %s);"
                         % (ap, ", ".join(cols), ", ".join(vals), ap, cond))
        self._check_writable(pt)
        ts = self._ts_cols(pt)
        has_type = sc.get("path_table_has_type")
        pt_cols = {c["column"] for c in self.table_meta().get(pt, [])}
        cols = ["path", "user_group_id", "perm"]
        vals = [sqlq(path), str(int(uid)), str(perm_value)]
        if "trimacl_perm" in pt_cols:
            cols.append("trimacl_perm")
            vals.append("0")
        if has_type:
            cols.append("type")
            vals.append("'appuser'")
        cols += ts
        vals += ["now()"] * len(ts)
        cond = "path = %s AND user_group_id = %d" % (sqlq(path), int(uid))
        if has_type:
            cond += " AND type = 'appuser'"
        lines.append("INSERT INTO %s (%s) SELECT %s WHERE NOT EXISTS "
                     "(SELECT 1 FROM %s WHERE %s);"
                     % (pt, ", ".join(cols), ", ".join(vals), pt, cond))
        upd = "UPDATE %s SET perm = %d" % (pt, perm_value)
        if "updated_at" in ts:
            upd += ", updated_at = now()"
        upd += " WHERE path = %s AND user_group_id = %d AND perm <> %d;" % (
            sqlq(path), int(uid), perm_value)
        lines.append(upd)
        lines.append("COMMIT;")
        return "\n".join(lines)

    def revoke_sql(self, app_id, uid, path):
        sc = self.schema()
        if not sc.get("ok"):
            return None
        ap, pt = sc["app_path_table"], sc["path_table"]
        lines = ["BEGIN;"]
        if app_id is not None:
            lines.append("DELETE FROM %s WHERE app_id = %d AND path = %s;"
                         % (ap, int(app_id), sqlq(path)))
        lines.append("DELETE FROM %s WHERE path = %s AND user_group_id = %d%s;"
                     % (pt, sqlq(path), int(uid),
                        " AND type = 'appuser'" if sc.get("path_table_has_type") else ""))
        lines.append("COMMIT;")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------
    def backup(self, reason="op"):
        if not self.s.get("backup"):
            return None
        sc = self.schema()
        if not sc.get("ok"):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = os.path.join(self.var_dir, "backup")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "appcenter-%s-%s.sql" % (stamp, reason))
        pg_dump = self._which("pg_dump") or "/usr/bin/pg_dump"
        argv = []
        if self.is_root:
            argv += ["runuser", "-u", "postgres", "--"]
        argv += [pg_dump, "-d", DB_NAME, "--data-only", "--column-inserts",
                 "-t", sc["app_path_table"], "-t", sc["path_table"]]
        rc, out, err = self.run(argv, timeout=180)
        if rc == 0 and out.strip():
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(out)
            self.log.info("已备份授权表到 %s" % target)
            self._prune_backups(folder)
            return target
        # 退化方案：把两张表的内容存成 JSON，便于人工比对
        try:
            snap = {"ts": now_str(), "reason": reason, "tables": {}}
            snap["tables"][sc["app_path_table"]] = self.psql_rows(
                "SELECT * FROM %s" % sc["app_path_table"])
            snap["tables"][sc["path_table"]] = self.psql_rows(
                "SELECT * FROM %s" % sc["path_table"])
            target = target[:-4] + ".json"
            with open(target, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False, indent=1)
            self.log.warn("pg_dump 不可用，已导出 JSON 快照：%s" % target)
            self._prune_backups(folder)
            return target
        except Exception as exc:
            self.log.error("备份失败：%s" % exc)
            return None

    def _prune_backups(self, folder, keep=40):
        try:
            files = sorted(glob.glob(os.path.join(folder, "appcenter-*")))
            for old in files[:-keep]:
                os.remove(old)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 授权 / 撤销
    # ------------------------------------------------------------------
    def test_access(self, uname, path, flag):
        if not uname:
            return None
        script = 'test -%s "$1"' % flag
        argv = []
        if self.is_root:
            argv = ["runuser", "-u", uname, "--"]
        argv += ["/bin/sh", "-c", script, "_", path]
        rc, out, err = self.run(argv, timeout=20)
        if rc == 0:
            return True
        if rc == 1:
            return False
        return None

    def verify(self, app, path):
        checks = []
        uname = app.get("uname")
        for flag, label in (("r", "读取"), ("w", "写入"), ("x", "进入目录")):
            got = self.test_access(uname, path, flag)
            checks.append({"label": label, "ok": got})
        acl = self.acl_summary(path, app.get("uid")) if app.get("uid") is not None else None
        return {"app": app.get("name"), "path": path, "checks": checks,
                "acl": acl, "user": uname}

    def grant_one(self, app, path, perm, dry_run=False, recursive=None):
        if recursive is None:
            recursive = bool(self.s.get("recursive"))
        steps = []
        res = {"app": app.get("name"), "display": app.get("display"),
               "path": path, "perm": perm, "ok": False, "skipped": False,
               "steps": steps, "message": ""}
        if app.get("no_authz"):
            res["skipped"] = True
            res["message"] = "该应用未开放目录授权（系统标记 disable_authorization_path），已跳过"
            return res
        uid = app.get("uid")
        if uid is None:
            res["message"] = "未找到该应用的系统账户（可能使用宿主账户运行），已跳过"
            res["skipped"] = True
            return res
        if not os.path.isdir(path):
            res["message"] = "路径不存在或不是目录"
            return res

        for argv in self.acl_commands(uid, path, perm, recursive):
            steps.append({"kind": "acl", "cmd": sh_join(argv)})
        sql = self.grant_sql(app.get("app_id"), uid, path, perm)
        if sql:
            steps.append({"kind": "db", "cmd": "psql -d appcenter", "sql": sql})
        else:
            res["message"] = "数据库结构未识别，仅应用了文件 ACL"
        if app.get("app_id") is None:
            res["message"] = ("appcenter 中未找到该应用的 app_id：仅写入文件 ACL，"
                              "系统设置界面可能不显示该授权")

        if dry_run:
            res["ok"] = True
            res["message"] = res["message"] or "预览模式，未做任何修改"
            return res

        failed = []
        for step in steps:
            if step["kind"] == "acl":
                argv = shlex.split(step["cmd"])
                rc, out, err = self.run(argv, timeout=180)
                step["rc"] = rc
                step["out"] = (out or "").strip()[:2000]
                step["err"] = (err or "").strip()[:2000]
                # setfacl 默认 ACL 在部分文件系统不可用，属可容忍失败
                if rc != 0 and "d:u:" not in step["cmd"]:
                    failed.append("ACL: %s" % (step["err"] or step["cmd"]))
            else:
                try:
                    self.psql_script(step["sql"])
                    step["rc"] = 0
                except Exception as exc:
                    step["rc"] = 1
                    step["err"] = str(exc)
                    failed.append("数据库: %s" % exc)

        if failed:
            res["message"] = "；".join(failed)[:800]
            self.log.error("授权失败 %s -> %s: %s" % (app.get("name"), path, failed))
            return res

        res["ok"] = True
        if not res["message"]:
            res["message"] = "已授权"
        self.log.info("已授权 %s -> %s (%s)" % (app.get("name"), path, perm))
        return res

    def revoke_one(self, app, path, dry_run=False):
        steps = []
        res = {"app": app.get("name"), "display": app.get("display"), "path": path,
               "ok": False, "steps": steps, "message": ""}
        uid = app.get("uid")
        if uid is None:
            res["message"] = "未找到该应用的系统账户"
            return res
        for argv in self.revoke_commands(uid, path):
            steps.append({"kind": "acl", "cmd": sh_join(argv)})
        sql = self.revoke_sql(app.get("app_id"), uid, path)
        if sql:
            steps.append({"kind": "db", "cmd": "psql -d appcenter", "sql": sql})
        if dry_run:
            res["ok"] = True
            res["message"] = "预览模式"
            return res
        failed = []
        for step in steps:
            if step["kind"] == "acl":
                rc, out, err = self.run(shlex.split(step["cmd"]), timeout=180)
                step["rc"] = rc
                step["err"] = (err or "").strip()[:2000]
                # 撤销 ACL 失败意味着权限仍残留，需要提示用户
                if rc != 0 and "d:u:" not in step["cmd"]:
                    failed.append("ACL 撤销失败：%s" % (step["err"] or step["cmd"]))
            else:
                try:
                    self.psql_script(step["sql"])
                    step["rc"] = 0
                except Exception as exc:
                    step["rc"] = 1
                    step["err"] = str(exc)
                    failed.append("数据库: %s" % exc)
        if failed:
            res["message"] = "；".join(failed)[:800]
            return res
        res["ok"] = True
        res["message"] = "已撤销"
        self.log.info("已撤销 %s -> %s" % (app.get("name"), path))
        return res

    def restart_app(self, app):
        """调用目标应用的生命周期脚本重启，让它重新读取授权目录。"""
        name = (app or {}).get("name") or ""
        main = "/var/apps/%s/cmd/main" % name
        if not name or not os.path.isfile(main):
            return {"app": name, "ok": False,
                    "message": "未找到 %s，已跳过重启" % main}
        rc, out, err = self.run([main, "restart"], timeout=180)
        msg = (err or out or "").strip().replace("\n", " ")[:300]
        if rc == 0:
            return {"app": name, "ok": True, "message": msg or "已重启"}
        return {"app": name, "ok": False,
                "message": msg or ("重启失败（退出码 %s）" % rc)}

    # ------------------------------------------------------------------
    # 失效授权巡检
    # ------------------------------------------------------------------
    def orphans(self):
        sc = self.schema()
        if not sc.get("ok"):
            raise DbError(sc.get("error") or "数据库结构未识别")
        db_map = self.db_app_map()
        uid_names = {}
        if pwd is not None:
            try:
                for pw in pwd.getpwall():
                    uid_names[pw.pw_uid] = pw.pw_name
            except Exception:
                pass
        installed = set()
        try:
            installed = set(os.listdir("/var/apps"))
        except Exception:
            pass

        items = []
        ap = sc["app_path_table"]
        select = "id, app_id, path" if sc.get("app_path_has_id") else "NULL, app_id, path"
        for row in self.psql_rows("SELECT %s FROM %s" % (select, ap)):
            if len(row) < 3:
                continue
            row_id, app_id, path = row[0], row[1], row[2]
            app = db_map.get(str(app_id), {})
            app_name = app.get("app_name") or ""
            display = (self._plain_name(app.get("name"))
                       or self._plain_name(app.get("i18n_name")) or app_name)
            reason = ""
            if not os.path.exists(path):
                reason = "路径已不存在"
            elif app_name and app_name not in installed:
                reason = "应用已卸载"
            if reason:
                items.append({"table": ap, "row_id": row_id, "app_id": app_id,
                              "app_name": display or app_name, "uid": None,
                              "uname": None, "path": path, "reason": reason})

        pt = sc["path_table"]
        select = "id, user_group_id, path, perm" if sc.get("path_table_has_id") \
            else "NULL, user_group_id, path, perm"
        for row in self.psql_rows("SELECT %s FROM %s" % (select, pt)):
            if len(row) < 4:
                continue
            row_id, uid, path, perm = row[0], row[1], row[2], row[3]
            uname = uid_names.get(int(uid)) if str(uid).isdigit() else None
            reason = ""
            if not os.path.exists(path):
                reason = "路径已不存在"
            elif uname is None:
                reason = "账户已不存在"
            if reason:
                items.append({"table": pt, "row_id": row_id, "app_id": None,
                              "app_name": uname or "", "uid": uid, "uname": uname,
                              "path": path, "reason": reason,
                              "perm": self.perm_label(perm)})
        items.sort(key=lambda x: (x["reason"], x["table"], x["path"]))
        return items

    def purge_orphans(self, items):
        sc = self.schema()
        if not sc.get("ok"):
            raise DbError(sc.get("error") or "数据库结构未识别")
        by_table = {}
        for item in items:
            table = item.get("table")
            row_id = item.get("row_id")
            if not table or row_id in (None, "", "None"):
                continue
            if table not in (sc["app_path_table"], sc["path_table"]):
                continue
            by_table.setdefault(table, []).append(str(int(row_id)))
        if not by_table:
            return {"deleted": 0, "detail": "没有可清理的记录（缺少主键，请手动处理）"}
        lines = ["BEGIN;"]
        total = 0
        for table, ids in by_table.items():
            for chunk in [ids[i:i + 200] for i in range(0, len(ids), 200)]:
                lines.append("DELETE FROM %s WHERE id IN (%s);" % (table, ",".join(chunk)))
                total += len(chunk)
        lines.append("COMMIT;")
        self.psql_script("\n".join(lines))
        self.log.info("已清理失效授权记录 %d 条" % total)
        return {"deleted": total, "detail": "\n".join(lines)}

    # ------------------------------------------------------------------
    # 目录浏览
    # ------------------------------------------------------------------
    def roots(self):
        out = []
        for root in self.s.get("roots") or ["/vol"]:
            root = posix_norm(root)
            if root == "/vol":
                for path in sorted(glob.glob("/vol*")):
                    if os.path.isdir(path) and not os.path.islink(path):
                        out.append({"path": path, "label": path})
            elif os.path.isdir(root):
                out.append({"path": root, "label": root})
        return out

    def browse(self, path):
        roots = self.s.get("roots") or ["/vol"]
        if not path:
            return {"path": "", "parent": None, "roots": self.roots(), "dirs": []}
        path = clean_path(path)
        if not under_roots(path, roots):
            raise PathError("该目录不在允许范围内（可在诊断页调整允许的根目录）")
        if not os.path.isdir(path):
            raise PathError("目录不存在")
        dirs = []
        try:
            for name in sorted(os.listdir(path), key=lambda x: x.lower()):
                if name.startswith("."):
                    continue
                full = path.rstrip("/") + "/" + name
                if not os.path.isdir(full):
                    continue
                dirs.append({"name": name, "path": full,
                             "empty": not os.listdir(full) if os.access(full, os.R_OK) else False})
                if len(dirs) >= 800:
                    break
        except PermissionError:
            raise PathError("没有权限读取该目录")
        parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
        if not under_roots(parent, roots):
            parent = ""
        return {"path": path, "parent": parent, "roots": self.roots(), "dirs": dirs}

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def tools(self):
        if self._tools is not None:
            return self._tools
        info = {}
        for name in ("trimacl", "setfacl", "getfacl", "psql", "pg_dump",
                     "runuser", "python3"):
            info[name] = self._which(name)
        self._tools = info
        return info

    def diagnose(self):
        sc = self.schema(force=True)
        data = {
            "platform": self.platform,
            "is_root": self.is_root,
            "tools": self.tools(),
            "acl_mode": self.acl_mode(),
            "schema": sc,
            "db_ok": sc.get("db_ok"),
            "db_error": sc.get("error"),
            "settings": {
                "port": self.s.get("port"),
                "perm_rw": self.s.get("perm_rw"),
                "perm_ro": self.s.get("perm_ro"),
                "acl_mode": self.s.get("acl_mode"),
                "recursive": self.s.get("recursive"),
                "backup": self.s.get("backup"),
                "no_login": bool(self.s.get("no_login")),
                "has_password": bool((self.s.data.get("admin") or {}).get("hash")),
                "roots": self.s.get("roots"),
            },
            "perm_stats": [],
            "volumes": [],
            "du": [],
        }
        try:
            rows = self.psql_rows(
                "SELECT perm, COALESCE(type,''), count(*) FROM %s "
                "GROUP BY 1,2 ORDER BY 3 DESC" % sc["path_table"]) if sc.get("ok") else []
            data["perm_stats"] = [
                {"perm": r[0], "label": self.perm_label(r[0]), "type": r[1], "count": r[2]}
                for r in rows if len(r) >= 3]
        except Exception as exc:
            data["perm_stats_error"] = str(exc)
        for vol in sorted(glob.glob("/vol*")):
            if not os.path.ismount(vol) and not os.path.isdir(vol):
                continue
            try:
                st = os.statvfs(vol)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                data["volumes"].append({"path": vol, "total": human_size(total),
                                        "free": human_size(free)})
            except Exception:
                data["volumes"].append({"path": vol, "total": "?", "free": "?"})
        return data
