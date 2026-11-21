#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在飞牛 NAS 本机跑的端到端验收脚本（只依赖标准库）。

做四件事：
  1. 登录 → 读取 /api/state /api/diagnose（结构识别、ACL 方式、权限统计）
  2. 建一个临时测试目录，用 /api/grant 做真实授权，并直接核对
     数据库记录 + trimacl ACE + 目标账户实测读写
  3. 用 /api/verify 校验，再 /api/revoke 撤销并确认清理干净
  4. 验证「不支持目录授权的应用」会被跳过

用法（在 NAS 上以 root 运行）：
    python3 live_test.py --password 你的管理密码
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18080"
TEST_DIR = "/vol1/appauthz-test"
TRIMACL = "/usr/trim/bin/trimacl" if os.path.exists("/usr/trim/bin/trimacl") else "trimacl"
RESULTS = []


def ok(name, detail=""):
    RESULTS.append((True, name, detail))
    print("  [PASS] %s %s" % (name, detail))


def fail(name, detail=""):
    RESULTS.append((False, name, detail))
    print("  [FAIL] %s %s" % (name, detail))


def call(path, body=None, cookie=None):
    req = urllib.request.Request(BASE + path)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
        req.method = "POST"
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw), []
        except Exception:
            return exc.code, {"error": raw}, []


def sh(cmd):
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    out = proc.communicate()[0].decode("utf-8", "replace")
    return proc.returncode, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", required=True)
    ap.add_argument("--password2", default=None)
    ap.add_argument("--app", default="fnos-scraper")
    ap.add_argument("--noauthz-app", default="victoria")
    ap.add_argument("--perm", default="ro", choices=("ro", "rw"))
    args = ap.parse_args()

    print("== 1. 登录与服务状态 ==")
    status, data, cookies = call("/api/ping")
    if status == 200 and data.get("installed"):
        ok("服务可访问", "v%s" % data.get("version"))
    else:
        fail("服务不可访问", str(data))
        return 1
    status, data, cookies = call("/api/login", {"password": args.password})
    if status != 200:
        fail("登录失败", str(data))
        return 1
    cookie = (cookies[0].split(";")[0] if cookies else "")
    ok("登录成功")

    print("== 2. 环境与结构识别 ==")
    status, data, _ = call("/api/diagnose", cookie=cookie)
    diag = data.get("data", {})
    if diag.get("is_root"):
        ok("以 root 运行")
    else:
        fail("非 root 运行")
    mode = diag.get("acl_mode")
    if mode in ("trimacl", "setfacl"):
        ok("ACL 工具", mode + " @ " + (diag.get("tools", {}).get(mode) or "?"))
    else:
        fail("未找到 ACL 工具")
    schema = diag.get("schema", {})
    if schema.get("ok"):
        ok("授权表识别",
           "%s + %s + %s" % (schema.get("app_path_table"),
                             schema.get("path_table"), schema.get("app_table")))
    else:
        fail("授权表未识别", schema.get("error", ""))
    stats = diag.get("perm_stats") or []
    ok("perm 统计", ", ".join("%s=%s×%s" % (s["perm"], s["label"], s["count"])
                              for s in stats) or "无数据")

    print("== 3. 应用与现有授权 ==")
    status, data, _ = call("/api/state", cookie=cookie)
    apps = data.get("apps", [])
    mapped = [a for a in apps if a.get("app_id")]
    ok("枚举到应用", "%d 个（其中 %d 个有 app_id）" % (len(apps), len(mapped)))
    target = next((a for a in apps if a["name"] == args.app), None)
    if not target:
        fail("找不到测试应用 %s" % args.app, "可选：%s" % ", ".join(a["name"] for a in apps))
        return 1
    ok("测试应用", "%s → %s uid=%s app_id=%s" % (target["display"], target["name"],
                                                target["uid"], target["app_id"]))

    print("== 4. 真实授权（%s） ==" % ("读写" if args.perm == "rw" else "只读"))
    if os.path.isdir(TEST_DIR):
        sh("rm -rf %s" % TEST_DIR)
    os.makedirs(TEST_DIR + "/child", exist_ok=True)
    with open(TEST_DIR + "/child/sample.txt", "w") as fh:
        fh.write("appauthz test\n")
    sh("chown -R 1000:1001 %s" % TEST_DIR)
    ok("建立测试目录", TEST_DIR)

    rc, out = sh("runuser -u %s -- test -r %s && echo Y || echo N" % (target["uname"], TEST_DIR))
    before = out.strip() == "Y"
    print("  授权前 %s 可读：%s" % (target["uname"], "是" if before else "否"))

    status, data, _ = call("/api/grant", {"apps": [args.app], "path": TEST_DIR,
                                          "perm": args.perm}, cookie=cookie)
    res = (data.get("results") or [{}])[0]
    if res.get("ok"):
        ok("授权接口返回成功", res.get("message", ""))
    else:
        fail("授权失败", res.get("message", ""))

    rc, out = sh("runuser -u %s -- test -r %s && echo Y || echo N" % (target["uname"], TEST_DIR))
    ok_read = out.strip() == "Y"
    rc, out = sh("runuser -u %s -- test -w %s && echo Y || echo N" % (target["uname"], TEST_DIR))
    ok_write = out.strip() == "Y"
    rc, out = sh("runuser -u %s -- cat %s/child/sample.txt 2>/dev/null" % (target["uname"], TEST_DIR))
    child_read = "appauthz test" in out
    if ok_read:
        ok("ACL 生效：可读")
    else:
        fail("ACL 未生效：不可读")
    if args.perm == "ro":
        if not ok_write:
            ok("只读语义正确：写被拒")
        else:
            fail("只读语义错误：仍可写")
    else:
        if ok_write:
            ok("读写语义正确：可写")
        else:
            fail("读写语义错误：仍不可写")
    if child_read:
        ok("继承生效：子文件可读")
    else:
        fail("继承失效：子文件不可读")

    rc, out = sh("%s %s 2>/dev/null | grep 'uid:%s'" % (TRIMACL, TEST_DIR, target["uid"]))
    if out.strip():
        ok("trimacl ACE 已写入", out.strip().split("\n")[0].strip())
    else:
        fail("trimacl 中没有对应 ACE")

    rc, out = sh("runuser -u postgres -- psql -X -q -A -t -d appcenter -c "
                 "\"select perm,trimacl_perm,type from authorization_path "
                 "where path='%s' and user_group_id=%s\"" % (TEST_DIR, target["uid"]))
    if out.strip():
        ok("数据库权限记录", out.strip().replace("\t", " / "))
    else:
        fail("数据库没有权限记录")
    rc, out = sh("runuser -u postgres -- psql -X -q -A -t -d appcenter -c "
                 "\"select app_id from app_authorization_path where path='%s'\"" % TEST_DIR)
    if out.strip():
        ok("数据库应用映射", "app_id=" + out.strip())
    else:
        fail("数据库缺少应用映射")

    print("== 5. 实测校验接口 ==")
    status, data, _ = call("/api/verify", {"apps": [args.app], "paths": [TEST_DIR]},
                           cookie=cookie)
    checks = ((data.get("results") or [{}])[0]).get("checks") or []
    summary = ", ".join("%s=%s" % (c["label"], c["ok"]) for c in checks)
    want_write = args.perm == "rw"
    want_ok = all(c["ok"] == (want_write if c["label"] == "写入" else True)
                  for c in checks)
    if checks and want_ok:
        ok("校验接口", summary)
    else:
        fail("校验接口异常", summary)

    print("== 6. 不支持目录授权的应用应被跳过 ==")
    status, data, _ = call("/api/grant", {"apps": [args.noauthz_app], "path": TEST_DIR,
                                          "perm": "rw"}, cookie=cookie)
    res = (data.get("results") or [{}])[0]
    if res.get("skipped"):
        ok("已跳过", res.get("message", ""))
    else:
        print("  [SKIP] %s 未标记为不支持目录授权：%s" % (args.noauthz_app, res.get("message")))

    print("== 7. 撤销与还原 ==")
    status, data, _ = call("/api/revoke", {"apps": [args.app], "paths": [TEST_DIR]},
                           cookie=cookie)
    res = (data.get("results") or [{}])[0]
    if res.get("ok"):
        ok("撤销接口返回成功", res.get("message", ""))
    else:
        fail("撤销失败", res.get("message", ""))
    rc, out = sh("runuser -u %s -- test -r %s && echo Y || echo N" % (target["uname"], TEST_DIR))
    if out.strip() == "N":
        ok("撤销后不可读")
    else:
        fail("撤销后仍可读")
    rc, out = sh("%s %s 2>/dev/null | grep 'uid:%s'" % (TRIMACL, TEST_DIR, target["uid"]))
    if not out.strip():
        ok("trimacl ACE 已移除")
    else:
        fail("trimacl ACE 仍存在")
    rc, out = sh("runuser -u postgres -- psql -X -q -A -t -d appcenter -c "
                 "\"select count(*) from authorization_path where path='%s'\"" % TEST_DIR)
    if out.strip() == "0":
        ok("数据库记录已清除")
    else:
        fail("数据库残留", out.strip())

    print("== 8. 失效授权巡检 ==")
    status, data, _ = call("/api/orphans", cookie=cookie)
    items = data.get("items") or []
    ok("巡检可用", "发现 %d 条失效记录" % len(items))
    false_pos = [i for i in items if i.get("reason") == "应用已卸载"
                 and os.path.isdir("/var/apps/%s" % i.get("app_name", ""))]
    if not false_pos:
        ok("巡检无误报", "无“已卸载”误报")
    else:
        fail("巡检误报", "、".join("%s(%s)" % (i["app_name"], i["path"]) for i in false_pos))

    sh("rm -rf %s" % TEST_DIR)
    print()
    passed = len([r for r in RESULTS if r[0]])
    total = len(RESULTS)
    print("==== 结果：%d/%d 通过 ====" % (passed, total))
    for good, name, detail in RESULTS:
        if not good:
            print("  失败：%s %s" % (name, detail))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
