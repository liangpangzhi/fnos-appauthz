#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键构建：生成图标 → 语法自检 → 打包 fpk。

  python build.py                      # 产物写入 dist/
  python build.py --platform arm       # 以 arm 平台打包
  python build.py --no-icon            # 跳过图标生成
"""
import os
import py_compile
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def ensure_icons():
    import make_icons
    make_icons.main()   # 同时生成 ui/images/icon_*.png 与根目录 ICON.PNG / ICON_256.PNG
    print("图标已就绪：ui/images/icon_*.png + ICON.PNG / ICON_256.PNG")


def check_syntax():
    bad = 0
    for folder in ("app/server", "tools"):
        for name in sorted(os.listdir(os.path.join(ROOT, folder))):
            if not name.endswith(".py"):
                continue
            path = os.path.join(ROOT, folder, name)
            try:
                with open(path, "rb") as fh:
                    compile(fh.read(), path, "exec")
            except SyntaxError as exc:
                bad += 1
                print("语法错误：%s\n%s" % (path, exc))
    if bad:
        raise SystemExit("存在 %d 个语法错误，构建中止" % bad)
    print("语法自检通过")


def main():
    args = sys.argv[1:]
    if "--no-icon" not in args:
        ensure_icons()
    check_syntax()

    platform = None
    for idx, item in enumerate(args):
        if item == "--platform" and idx + 1 < len(args):
            platform = args[idx + 1]
        elif item.startswith("--platform="):
            platform = item.split("=", 1)[1]

    import pack_fpk
    result = pack_fpk.pack(ROOT, None, platform)
    print("")
    print("=" * 62)
    print("构建成功")
    print("  产物   : %s" % result["path"])
    print("  应用   : %s %s (%s)" % (result["appname"], result["version"],
                                     result["platform"]))
    print("  体积   : %.1f KB" % (result["size"] / 1024.0))
    print("  app.tgz: md5 %s" % result["checksum"])
    print("=" * 62)
    print("安装：飞牛应用中心 → 手动安装 → 选择上面的 .fpk 文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
