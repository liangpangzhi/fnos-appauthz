#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnOS .fpk 打包器（纯标准库，Windows / Linux / macOS 均可运行）。

.fpk = gzip(tar)，内部结构按官方 fnpack 的实际产物逆向实现：

  外层 tar（USTAR）
    ├── app.tgz            安装到 $TRIM_APPDEST 的内容（自身也是 gzip(tar)，不含 app/ 前缀）
    ├── cmd/               生命周期脚本
    ├── config/            privilege / resource
    ├── ICON.PNG           64x64
    ├── ICON_256.PNG       256x256
    ├── LICENSE
    ├── manifest           末尾追加 checksum = md5(app.tgz)
    └── wizard/            向导（可选）

细节约定（被 fnOS 校验）：
  * tar 用 USTAR（magic "ustar\\0" + version "00"），不用 GNU 扩展；
  * 成员顺序：顶层按名称（不区分大小写）排序，目录项自身也写入，目录项后紧跟其内容；
  * 目录权限 0777、文件 0666；uid=gid=0；uname/gname 留空；
  * 目录名不带结尾斜杠；devmajor/devminor 写 "0000000\\0"；
  * 仅两个结束零块，不做 10240 字节记录对齐；
  * gzip 头固定 FLG=0、MTIME=0、XFL=0、OS=255；
  * cmd/ 下脚本与文本一律 LF；manifest 一律 CRLF。

用法：
  python tools/pack_fpk.py                 # 打包当前工程 → dist/
  python tools/pack_fpk.py --src . --out dist/custom.fpk
  python tools/pack_fpk.py --list x.fpk    # 查看已有 fpk 的成员
"""
import argparse
import hashlib
import os
import struct
import sys
import zlib

BLOCK = 512
TEXT_EXT = {".sh", ".py", ".json", ".txt", ".md", ".conf", ".cfg", ".ini",
            ".html", ".css", ".js", ".yaml", ".yml", ".service"}

# 顶层成员顺序（不区分大小写排序，见文件头说明）
TOP_ORDER = ["app.tgz", "cmd", "config", "ICON.PNG", "ICON_256.PNG",
             "LICENSE", "manifest", "wizard"]


# --------------------------------------------------------------------------
# tar 写入
# --------------------------------------------------------------------------
def _oct7(value):
    """7 位八进制 + NUL（与 fnpack 的 devmajor/devminor 写法一致）。"""
    return ("%07o" % value).encode("ascii") + b"\0"


def tar_header(name, size, mode, typeflag):
    raw = name.encode("utf-8")
    if len(raw) > 100:
        raise ValueError("路径超过 100 字节，USTAR 不支持：%s" % name)
    header = bytearray(BLOCK)
    header[0:len(raw)] = raw
    header[100:108] = _oct7(mode)
    header[108:116] = _oct7(0)          # uid
    header[116:124] = _oct7(0)          # gid
    header[124:136] = _oct7(size)
    header[136:148] = _oct7(0)          # mtime
    header[148:156] = b" " * 8          # chksum 占位
    header[156:157] = typeflag
    header[157:257] = b"\0" * 100       # linkname
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[265:297] = b"\0" * 32        # uname
    header[297:329] = b"\0" * 32        # gname
    header[329:337] = b"0000000\0"      # devmajor
    header[337:345] = b"0000000\0"      # devminor
    header[345:500] = b"\0" * 155       # prefix
    header[500:512] = b"\0" * 12
    checksum = sum(header)
    header[148:156] = ("%06o" % checksum).encode("ascii") + b"\0 "
    return bytes(header)


class TarBuilder(object):
    def __init__(self):
        self.buf = bytearray()

    def add_dir(self, name):
        self.buf += tar_header(name, 0, 0o777, b"5")

    def add_file(self, name, data):
        self.buf += tar_header(name, len(data), 0o666, b"0")
        self.buf += data
        pad = (-len(data)) % BLOCK
        if pad:
            self.buf += b"\0" * pad

    def finish(self):
        return bytes(self.buf) + b"\0" * (BLOCK * 2)


def gzip_bytes(data):
    """构造 gzip 流：FLG=0、MTIME=0、XFL=0、OS=255（与 fnpack 一致）。"""
    header = b"\x1f\x8b\x08\x00" + b"\x00\x00\x00\x00" + b"\x00" + b"\xff"
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = comp.compress(data) + comp.flush()
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF,
                          len(data) & 0xFFFFFFFF)
    return header + body + trailer


# --------------------------------------------------------------------------
# 文本规整
# --------------------------------------------------------------------------
def normalize(data, ext, is_cmd):
    """cmd/ 下的脚本必须是 LF；manifest 必须是 CRLF。"""
    text = data
    if ext in TEXT_EXT or is_cmd:
        # 仅对文本做行尾归一，二进制原样保留
        if b"\0" in data[:4096]:
            return data
        text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if is_cmd:
            return text
    return text


def collect_dir(root, prefix=""):
    """返回 [(name, kind, payload)]，按名称不区分大小写排序。"""
    entries = []
    names = sorted(os.listdir(root), key=lambda x: x.lower())
    for name in names:
        full = os.path.join(root, name)
        rel = (prefix + "/" + name) if prefix else name
        if os.path.isdir(full):
            entries.append((rel, "dir", None))
            entries.extend(collect_dir(full, rel))
        elif os.path.isfile(full):
            entries.append((rel, "file", full))
    return entries


def build_payload(app_dir, config_dir=None):
    """构造 app.tgz。fnpack 会把 config/ 一并打进 app.tgz，这里保持一致。"""
    builder = TarBuilder()
    for name, kind, full in collect_dir(app_dir):
        if kind == "dir":
            builder.add_dir(name)
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(full)[1].lower()
        builder.add_file(name, normalize(data, ext, False))
    if config_dir and os.path.isdir(config_dir):
        builder.add_dir("config")
        for name, kind, full in collect_dir(config_dir, "config"):
            if kind == "dir":
                builder.add_dir(name)
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(full)[1].lower()
            builder.add_file(name, normalize(data, ext, False))
    return gzip_bytes(builder.finish())


def read_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_manifest(text):
    info = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip()
    return info


def rewrite_manifest(text, checksum, platform=None):
    """行尾统一 CRLF，末尾追加 checksum = md5(app.tgz)；可覆盖 platform。"""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    out = []
    for line in lines:
        if line.startswith("platform") and ";" in line:
            key, value = line.split("=", 1)
            parts = [p.strip() for p in value.split(";") if p.strip()]
            for extra in parts[1:]:
                out.append("; " + extra)
            out.append("%s = %s" % (key.strip(), parts[0]))
            continue
        out.append(line)
    if platform:
        replaced = []
        for line in out:
            if line.strip().startswith("platform"):
                replaced.append("%-27s= %s" % ("platform", platform))
            else:
                replaced.append(line)
        out = replaced
    out = [ln for ln in out if not ln.startswith("checksum")]
    out.append("%-21s = %s" % ("checksum", checksum))
    return ("\r\n".join(out) + "\r\n").encode("utf-8")


# --------------------------------------------------------------------------
# 打包
# --------------------------------------------------------------------------
def pack(src, out_path=None, platform=None):
    src = os.path.abspath(src)
    manifest_path = os.path.join(src, "manifest")
    if not os.path.isfile(manifest_path):
        raise SystemExit("找不到 manifest：%s" % manifest_path)

    manifest_text = read_manifest(manifest_path)
    info = parse_manifest(manifest_text)
    appname = info.get("appname") or "app"
    version = info.get("version") or "0.0.0"
    platform = (platform or info.get("platform") or "all").replace(";", "-")

    for required in ("config/privilege", "config/resource"):
        if not os.path.isfile(os.path.join(src, required.replace("/", os.sep))):
            raise SystemExit("缺少必需文件：%s" % required)
    for icon in ("ICON.PNG", "ICON_256.PNG"):
        if not os.path.isfile(os.path.join(src, icon)):
            raise SystemExit("缺少图标：%s（请先运行 tools/make_icons.py）" % icon)

    app_tgz = build_payload(os.path.join(src, "app"), os.path.join(src, "config"))
    checksum = hashlib.md5(app_tgz).hexdigest()

    files = {"app.tgz": app_tgz,
             "manifest": rewrite_manifest(manifest_text, checksum, platform)}
    dirs = {}
    for name in ("cmd", "config", "wizard"):
        path = os.path.join(src, name)
        if os.path.isdir(path):
            entries = collect_dir(path, name)
            dirs[name] = entries
    for name in ("ICON.PNG", "ICON_256.PNG", "LICENSE"):
        path = os.path.join(src, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                files[name] = fh.read()

    builder = TarBuilder()
    for name in TOP_ORDER:
        if name in files:
            builder.add_file(name, files[name])
        elif name in dirs:
            builder.add_dir(name)
            for rel, kind, full in dirs[name]:
                if kind == "dir":
                    builder.add_dir(rel)
                    continue
                with open(full, "rb") as fh:
                    data = fh.read()
                ext = os.path.splitext(full)[1].lower()
                builder.add_file(rel, normalize(data, ext, rel.startswith("cmd/")))
    payload = gzip_bytes(builder.finish())

    if out_path is None:
        out_dir = os.path.join(src, "dist")
        out_path = os.path.join(out_dir, "%s-%s-%s.fpk" % (appname, version, platform))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(payload)
    return {"path": out_path, "size": len(payload), "checksum": checksum,
            "appname": appname, "version": version, "platform": platform}


def list_fpk(path):
    import gzip
    with open(path, "rb") as fh:
        raw = gzip.decompress(fh.read())
    pos = 0
    rows = []
    while pos + BLOCK <= len(raw):
        block = raw[pos:pos + BLOCK]
        if block == b"\0" * BLOCK:
            break
        name = block[0:100].rstrip(b"\0").decode("utf-8", "replace")
        mode = int(block[100:108].rstrip(b"\0 ").decode() or "0", 8)
        size = int(block[124:136].rstrip(b"\0 ").decode() or "0", 8)
        typeflag = block[156:157]
        rows.append((name, "dir" if typeflag == b"5" else "file", size, mode))
        pos += BLOCK + ((size + BLOCK - 1) // BLOCK) * BLOCK
    return rows


def main():
    parser = argparse.ArgumentParser(description="fnOS fpk 打包器")
    parser.add_argument("--src", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), help="工程根目录（含 manifest）")
    parser.add_argument("--out", default=None, help="输出 .fpk 路径")
    parser.add_argument("--platform", default=None,
                        help="覆盖 manifest 的 platform（x86 / arm）")
    parser.add_argument("--list", dest="list_file", default=None,
                        help="列出已有 fpk 的成员")
    args = parser.parse_args()

    if args.list_file:
        for name, kind, size, mode in list_fpk(args.list_file):
            print("%-34s %-5s %8d %04o" % (name, kind, size, mode))
        return 0

    result = pack(args.src, args.out, args.platform)
    print("打包完成：%s" % result["path"])
    print("  应用     : %s %s (%s)" % (result["appname"], result["version"],
                                       result["platform"]))
    print("  体积     : %.1f KB" % (result["size"] / 1024.0))
    print("  app.tgz  : md5 %s" % result["checksum"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
