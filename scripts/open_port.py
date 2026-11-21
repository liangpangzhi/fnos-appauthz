#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给本机飞牛虚拟机的 QEMU 热加一条端口转发（不需要重启虚拟机）。

背景：这台测试虚拟机用 QEMU user 网络，只映射了 5666 / 2222，
所以 http://localhost:18080 打不开（桌面点图标会提示无法访问）。
QEMU 支持在运行时通过 QMP 增加 hostfwd：

    python scripts/open_port.py               # 默认补 18080
    python scripts/open_port.py --port 18099
    python scripts/open_port.py --list        # 看当前转发规则

QMP 端口从虚拟机启动参数里读（默认 127.0.0.1:7666）。

注意：热加的规则在虚拟机关机后失效。若要长期保留，把端口写进
启动器的 fnnas-data/config.json 的 forward_ports 列表。
"""
import argparse
import json
import socket


def qmp(port, commands, host="127.0.0.1"):
    sock = socket.create_connection((host, port), timeout=8)
    stream = sock.makefile("rwb")
    stream.readline()  # QMP 问候语
    out = []
    for cmd in commands:
        stream.write((json.dumps(cmd) + "\n").encode())
        stream.flush()
        line = stream.readline().decode().strip()
        while line.startswith('{"event"'):
            line = stream.readline().decode().strip()
        out.append(line)
    sock.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="QEMU 热加端口转发")
    ap.add_argument("--qmp", type=int, default=7666, help="QMP 端口，默认 7666")
    ap.add_argument("--port", type=int, default=18080, help="要转发的端口，默认 18080")
    ap.add_argument("--netdev", default="net0", help="netdev 名，默认 net0")
    ap.add_argument("--list", action="store_true", help="只列出当前转发规则")
    args = ap.parse_args()

    try:
        if args.list:
            out = qmp(args.qmp, [
                {"execute": "qmp_capabilities"},
                {"execute": "human-monitor-command",
                 "arguments": {"command-line": "info usernet"}},
            ])
            print(out[-1])
            return 0

        out = qmp(args.qmp, [
            {"execute": "qmp_capabilities"},
            {"execute": "human-monitor-command",
             "arguments": {"command-line": "hostfwd_add %s tcp::%d-:%d"
                                            % (args.netdev, args.port, args.port)}},
            {"execute": "human-monitor-command",
             "arguments": {"command-line": "info usernet"}},
        ])
        reply = out[1]
        if '"error"' in reply:
            print("添加失败：%s" % reply)
            return 1
        print("已添加转发：本机 *:%d → 虚拟机 :%d" % (args.port, args.port))
        print(out[-1])
        return 0
    except Exception as exc:
        print("无法连接 QMP（%s:%d）：%s" % ("127.0.0.1", args.qmp, exc))
        print("虚拟机没开 QMP，或用了别的端口。可改用启动器的 forward_ports 配置。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
