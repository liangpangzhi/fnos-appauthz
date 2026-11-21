#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库，无 Pillow 依赖）。

fnOS 的 ui/config 用 `images/icon_{0}.png` 引用图标，{0} 会被替换为尺寸，
所以文件名必须是「下划线」形式：icon_16.png、icon_48.png、icon_64.png …
另外参考系统自带应用，会额外放一份字面量 icon_{0}.png 作为兜底。

产物：
  app/ui/images/icon_{16,24,32,48,64,72,96,128,256}.png
  app/ui/images/icon_{0}.png        （兜底，尺寸同 256）
  ICON.PNG / ICON_256.PNG           （包根目录，64 / 256）

图形：渐变圆角方块 + 白色钥匙，代表“授权/许可”。
"""
import os
import struct
import sys
import zlib

SS = 4  # 超采样倍数


def write_png(path, width, height, pixels):
    """pixels: list of rows, each row = list of (r,g,b,a)"""
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def lerp(a, b, t):
    return a + (b - a) * t


def in_round_rect(x, y, w, h, r):
    dx = max(r - x, 0, x - (w - r))
    dy = max(r - y, 0, y - (h - r))
    if dx == 0 or dy == 0:
        return 0 <= x <= w and 0 <= y <= h
    return dx * dx + dy * dy <= r * r


def in_capsule(px, py, x1, y1, x2, y2, half):
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    seg = vx * vx + vy * vy
    t = 0.0 if seg == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / seg))
    cx, cy = x1 + t * vx, y1 + t * vy
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= half * half


def in_ring(px, py, cx, cy, r_out, r_in):
    d2 = (px - cx) ** 2 + (py - cy) ** 2
    return r_in * r_in <= d2 <= r_out * r_out


def render(size):
    big = size * SS
    rows = []
    cx = cy = big / 2.0

    # 背景
    radius = big * 0.225
    # 钥匙几何（相对画布比例）
    key_cy = cy
    ring_cx = big * 0.325
    ring_out = big * 0.150
    ring_in = big * 0.078
    shaft_half = big * 0.042
    shaft_x1 = ring_cx + ring_in * 0.75
    shaft_x2 = big * 0.775
    tooth_w = big * 0.052
    tooth_h = big * 0.098

    acc = [[None] * big for _ in range(big)]
    for y in range(big):
        for x in range(big):
            px, py = x + 0.5, y + 0.5
            if not in_round_rect(px, py, big, big, radius):
                acc[y][x] = (0, 0, 0, 0)
                continue
            t = (px + py) / (2.0 * big)
            r = int(lerp(18, 47, t))
            g = int(lerp(184, 107, t))
            b = int(lerp(166, 255, t))

            # 顶部高光
            gl = max(0.0, 1.0 - (py / big) * 2.4) * 0.13
            r = min(255, int(r + 255 * gl))
            g = min(255, int(g + 255 * gl))
            b = min(255, int(b + 255 * gl))

            glyph = False
            if in_ring(px, py, ring_cx, key_cy, ring_out, ring_in):
                glyph = True
            if in_capsule(px, py, shaft_x1, key_cy, shaft_x2, key_cy, shaft_half):
                glyph = True
            for tx in (big * 0.575, big * 0.700):
                if in_round_rect(px - tx, py - key_cy, tooth_w, tooth_h,
                                 tooth_w * 0.28):
                    glyph = True
            if glyph:
                r = g = b = 255
            acc[y][x] = (r, g, b, 255)

    # 降采样
    for y in range(size):
        row = []
        for x in range(size):
            rs = gs = bs = as_ = 0
            for dy in range(SS):
                for dx in range(SS):
                    pr, pg, pb, pa = acc[y * SS + dy][x * SS + dx]
                    rs += pr * pa
                    gs += pg * pa
                    bs += pb * pa
                    as_ += pa
            n = SS * SS
            if as_ == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((rs // as_, gs // as_, bs // as_, as_ // n))
        rows.append(row)
    return rows


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(base, "app", "ui", "images")
    os.makedirs(out_dir, exist_ok=True)

    # fnOS 桌面可能取 16/24/32/48/64/72/96/128/256 中的任意档，全部备齐
    cache = {}
    for size in (16, 24, 32, 48, 64, 72, 96, 128, 256):
        name = "icon_%d.png" % size
        path = os.path.join(out_dir, name)
        cache[size] = render(size)
        write_png(path, size, size, cache[size])
        print("生成 %s (%dx%d)" % (name, size, size))

    # 字面量兜底（系统自带应用也有这个文件）
    fallback = os.path.join(out_dir, "icon_{0}.png")
    with open(os.path.join(out_dir, "icon_256.png"), "rb") as src:
        data = src.read()
    with open(fallback, "wb") as fh:
        fh.write(data)
    print("生成 icon_{0}.png（兜底）")

    # 包根目录图标（应用中心列表用）
    if 256 not in cache:
        cache[256] = render(256)
    for name, size in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
        path = os.path.join(base, name)
        if size not in cache:
            cache[size] = render(size)
        write_png(path, size, size, cache[size])
        print("生成 %s (%dx%d)" % (name, size, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
