#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛 NAS 远程操作助手（paramiko）。

凭据只从环境变量读取，绝不写入文件：
    SSH_HOST   默认 127.0.0.1
    SSH_PORT   默认 2222
    SSH_USER   必填（本机测试环境为 123）
    SSH_PASS   必填（sudo 密码与登录密码相同）

子命令：
    run   <command>            以普通用户执行命令
    sudo  <command>            以 sudo 执行（自动喂密码，-S）
    push  <local> <remote>     上传文件（自动建目录、校验 md5）
    pull  <remote> <local>     下载文件
    probe                      汇总环境信息（系统/磁盘/已装应用/CLI）

示例：
    SSH_USER=123 SSH_PASS=*** python scripts/remote.py run "uname -a"
    SSH_USER=123 SSH_PASS=*** python scripts/remote.py sudo "appcenter-cli list"
"""
import base64
import hashlib
import os
import posixpath
import sys

import paramiko

HOST = os.getenv("SSH_HOST", "127.0.0.1")
PORT = int(os.getenv("SSH_PORT", "2222"))
USER = os.getenv("SSH_USER", "")
PASS = os.getenv("SSH_PASS", "")


def connect():
    if not USER or not PASS:
        sys.stderr.write("缺少 SSH_USER / SSH_PASS 环境变量\n")
        raise SystemExit(2)
    client = paramiko.SSHClient()
    # 测试环境主机密钥可能因重建而变化，这里不读取 known_hosts
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASS,
                   timeout=20, banner_timeout=25, auth_timeout=25,
                   allow_agent=False, look_for_keys=False)
    return client


def run(client, command, sudo=False, timeout=180):
    if sudo:
        # -S 从 stdin 读密码；-p '' 不打印提示；保持环境变量最小化
        command = "sudo -S -p '' bash -lc %s" % _shq(command)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout,
                                                get_pty=False)
    if sudo:
        stdin.write(PASS + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def _shq(text):
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def md5_local(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_remote(client, path):
    rc, out, _ = run(client, "md5sum %s" % _shq(path))
    return out.split()[0] if rc == 0 and out.strip() else ""


def put(client, local, remote):
    """SFTP 在本机飞牛上受限（chroot 且用户无家目录），改用 base64 走 exec 通道。"""
    with open(local, "rb") as fh:
        payload = base64.b64encode(fh.read())
    cmd = "base64 -d > %s" % _shq(remote)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=600)
    step = 60000
    for i in range(0, len(payload), step):
        stdin.write(payload[i:i + step].decode("ascii") + "\n")
        stdin.flush()
    stdin.channel.shutdown_write()
    rc = stdout.channel.recv_exit_status()
    err = stderr.read().decode("utf-8", "replace")
    if rc != 0:
        print("上传失败 rc=%d %s" % (rc, err))
        return 1
    local_md5 = md5_local(local)
    remote_md5 = md5_remote(client, remote)
    print("上传 %s → %s（%.1f KB）" % (local, remote, os.path.getsize(local) / 1024.0))
    print("md5 local  = %s" % local_md5)
    print("md5 remote = %s" % remote_md5)
    if local_md5 != remote_md5:
        print("!! 校验不一致")
        return 1
    print("校验一致 ✓")
    return 0


def get(client, remote, local):
    rc, out, err = run(client, "base64 -w0 %s" % _shq(remote), timeout=600)
    if rc != 0:
        print("下载失败：%s" % err)
        return 1
    with open(local, "wb") as fh:
        fh.write(base64.b64decode(out))
    print("已下载 %s → %s" % (remote, local))
    return 0


PROBE = r"""
set +e
echo "== 系统 =="
. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"
uname -r
echo "== 用户 =="
id -un; id -Gn | tr ' ' ','
echo "== 磁盘 =="
df -h / /vol1 2>/dev/null | sed -n '1,4p'
echo "== appcenter-cli =="
command -v appcenter-cli || ls -l /usr/trim/bin/appcenter-cli 2>/dev/null
appcenter-cli --help 2>&1 | head -30
echo "== 已安装应用（目录） =="
ls /var/apps 2>/dev/null | tr '\n' ' '; echo
echo "== appauthz 是否已装 =="
ls -d /var/apps/appauthz 2>/dev/null || echo "未安装"
echo "== 数据库 =="
command -v psql; sudo -S -p '' -n true 2>/dev/null && echo "sudo 免密"
echo "== ACL 工具 =="
command -v trimacl setfacl getfacl
echo "== 端口 18080 占用 =="
ss -ltn 2>/dev/null | grep -E ':18080|:5666' || echo "18080 空闲"
"""


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    action = args[0]
    client = connect()
    try:
        if action == "run":
            rc, out, err = run(client, " ".join(args[1:]))
        elif action == "sudo":
            rc, out, err = run(client, " ".join(args[1:]), sudo=True)
        elif action in ("push", "put"):
            return put(client, args[1], args[2])
        elif action in ("pull", "get"):
            return get(client, args[1], args[2])
        elif action == "probe":
            rc, out, err = run(client, PROBE)
        else:
            print(__doc__)
            return 1
        sys.stdout.write(out)
        if err.strip():
            sys.stdout.write("\n--- stderr ---\n" + err)
        print("\n[exit %d]" % rc)
        return 0 if rc == 0 else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
