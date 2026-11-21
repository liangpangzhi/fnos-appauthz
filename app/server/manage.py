#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行辅助：安装/升级初始化、密码重置。

  manage.py bootstrap --dest <APPDEST> --etc <PKGETC> --var <PKGVAR>
  manage.py reset-password --etc <PKGETC> --password <新密码>
  manage.py show --etc <PKGETC>
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import APP_NAME, APP_VERSION, Settings, now_str, write_json


def fail(msg):
    sys.stderr.write(msg + "\n")
    return 1


def patch_ui_port(dest, port):
    """把 ui/config 里的桌面入口端口改成向导里填的端口。"""
    path = os.path.join(dest, "ui", "config")
    data = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return
    changed = False
    for key, entry in (data.get(".url") or {}).items():
        if entry.get("port") not in (None, "") and str(entry.get("port")) != str(port):
            entry["port"] = str(port)
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


def cmd_bootstrap(args):
    os.makedirs(args.etc, exist_ok=True)
    os.makedirs(args.var, exist_ok=True)
    settings = Settings(args.etc)

    pwd1 = (os.environ.get("wizard_admin_password") or "").strip()
    pwd2 = (os.environ.get("wizard_admin_password2") or "").strip()
    port_env = (os.environ.get("wizard_port") or "").strip()

    first_time = not settings.has_password()
    if first_time:
        if not pwd1:
            return fail("未收到管理密码（wizard_admin_password），无法初始化")
        if pwd2 and pwd1 != pwd2:
            return fail("两次输入的密码不一致")
        if len(pwd1) < 6:
            return fail("管理密码至少 6 位")
        settings.set_password(pwd1)
        settings.data["created_at"] = settings.data.get("created_at") or now_str()
        settings.save()

    if port_env.isdigit():
        port = int(port_env)
        if 1024 <= port <= 65535:
            settings.data["port"] = port
            settings.save()
            patch_ui_port(args.dest, port)

    settings.ensure_secret()
    if not settings.get("created_at"):
        settings.data["created_at"] = now_str()
        settings.save()

    # 权限收敛：配置文件仅 root 可读
    for name in ("settings.json",):
        try:
            os.chmod(os.path.join(args.etc, name), 0o600)
        except Exception:
            pass

    print("bootstrap ok: version=%s port=%s first_time=%s etc=%s"
          % (APP_VERSION, settings.get("port"), first_time, args.etc))
    return 0


def cmd_reset_password(args):
    settings = Settings(args.etc)
    if len(args.password) < 6:
        return fail("密码至少 6 位")
    settings.set_password(args.password)
    print("密码已重置")
    return 0


def cmd_auth(args):
    """开关免登录（不校验管理密码）。"""
    settings = Settings(args.etc)
    if args.mode == "show":
        print("no_login = %s" % bool(settings.get("no_login")))
        print("has_password = %s" % settings.has_password())
        return 0
    enabled = args.mode == "off"
    settings.set(no_login=enabled)
    if enabled:
        print("已开启免登录：打开界面不再需要管理密码。")
        print("安全提示：任何能访问该端口的人都能改授权，建议限制端口访问范围。")
        print("随时可恢复：manage.py auth --on")
    else:
        if not settings.has_password():
            print("已关闭免登录，但当前没有设置过密码，界面会提示初始化。")
            print("请执行：manage.py reset-password --password '你的密码'")
        else:
            print("已关闭免登录：打开界面需要输入管理密码。")
    return 0


def cmd_show(args):
    settings = Settings(args.etc)
    data = dict(settings.data)
    data["admin"] = {"algo": (data.get("admin") or {}).get("algo"),
                     "has_hash": bool((data.get("admin") or {}).get("hash"))}
    data.pop("secret", None)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="应用授权管家 辅助工具")
    sub = parser.add_subparsers(dest="cmd")

    boot = sub.add_parser("bootstrap")
    boot.add_argument("--dest", default=os.environ.get("TRIM_APPDEST", os.getcwd()))
    boot.add_argument("--etc", default=os.environ.get("TRIM_PKGETC",
                                                      "/var/apps/appauthz/etc"))
    boot.add_argument("--var", default=os.environ.get("TRIM_PKGVAR",
                                                      "/var/apps/appauthz/var"))

    reset = sub.add_parser("reset-password")
    reset.add_argument("--etc", default=os.environ.get("TRIM_PKGETC",
                                                       "/var/apps/appauthz/etc"))
    reset.add_argument("--password", required=True)

    auth = sub.add_parser("auth", help="开关免登录")
    auth.add_argument("--etc", default=os.environ.get("TRIM_PKGETC",
                                                      "/var/apps/appauthz/etc"))
    auth.add_argument("--mode", choices=("off", "on", "show"), default="show",
                      help="off=免登录，on=需要密码，show=查看当前")

    show = sub.add_parser("show")
    show.add_argument("--etc", default=os.environ.get("TRIM_PKGETC",
                                                      "/var/apps/appauthz/etc"))

    args = parser.parse_args(argv)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(args)
    if args.cmd == "auth":
        return cmd_auth(args)
    if args.cmd == "reset-password":
        return cmd_reset_password(args)
    if args.cmd == "show":
        return cmd_show(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
