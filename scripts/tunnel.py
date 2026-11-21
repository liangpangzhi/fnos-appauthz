#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机端口转发到飞牛 NAS 上的应用端口（paramiko 直连转发）。

凭据同 remote.py（SSH_USER / SSH_PASS / SSH_HOST / SSH_PORT）。
    SSH_USER=123 SSH_PASS=*** python scripts/tunnel.py --local 18080 --remote 18080
按 Ctrl+C 结束。
"""
import argparse
import os
import selectors
import socket
import socketserver
import sys

import paramiko

HOST = os.getenv("SSH_HOST", "127.0.0.1")
PORT = int(os.getenv("SSH_PORT", "2222"))
USER = os.getenv("SSH_USER", "")
PASS = os.getenv("SSH_PASS", "")


class Forwarder(socketserver.BaseRequestHandler):
    def handle(self):
        chan = self.ssh_transport.open_channel(
            "direct-tcpip", (self.remote_host, self.remote_port),
            self.request.getpeername())
        if chan is None:
            return
        try:
            while True:
                r, _, _ = selectors.DefaultSelector().select([], 0)  # noqa: F841
                data = self.request.recv(65536)
                if not data:
                    break
                chan.sendall(data)
                back = chan.recv(65536)
                if not back:
                    break
                self.request.sendall(back)
        except Exception:
            pass
        finally:
            chan.close()
            self.request.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", type=int, default=18080)
    parser.add_argument("--remote", type=int, default=18080)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()

    if not USER or not PASS:
        sys.stderr.write("缺少 SSH_USER / SSH_PASS\n")
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20,
                   allow_agent=False, look_for_keys=False)
    transport = client.get_transport()

    class Handler(Forwarder):
        ssh_transport = transport
        remote_host = "127.0.0.1"
        remote_port = args.remote

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((args.bind, args.local), Handler)
    server.daemon_threads = True
    print("隧道就绪：%s:%d → NAS 127.0.0.1:%d" % (args.bind, args.local, args.remote))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
