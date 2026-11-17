#!/usr/bin/env python3
"""
VNC 网关开关。

设计动机：
  noVNC 的 7900 端口是**免密**的，能连上就等于能操作你已登录的浏览器。
  因此不常开：默认关闭，只在需要登录或排障时打开，用完即关。

实现方式：
  不在 nginx 里写开关（reload 太重且易出错），而是控制一个
  **流式转发进程**：开着时它在 127.0.0.1:7910 监听并把流量转给 7900，
  nginx 始终指向 7910。关闭时杀掉该进程，nginx 侧自然 502/断开。

  这样做的额外好处：关闭瞬间所有 WebSocket 连接立即断开，
  不存在「已关闭但旧连接还在用」的窗口。

用法：
  python3 scripts/vnc_gate.py start|stop|status
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 7910
# 上游默认指向同一 docker 网络内的 selenium 容器。
# 注意不能用 127.0.0.1：本服务是独立容器，它看到的回环是自己，
# 而 noVNC 开在 bili-selenium 容器里。两者同在 bili-net，用服务名即可。
TARGET_HOST = os.getenv("VNC_UPSTREAM_HOST", "bili-selenium")
TARGET_PORT = int(os.getenv("VNC_UPSTREAM_PORT", "7900"))
# 网关监听地址：nginx 运行在宿主网络，需经 published 端口访问，
# 因此容器内监听 0.0.0.0，再由 compose 映射到宿主回环。
LISTEN_HOST = os.getenv("VNC_LISTEN_HOST", "0.0.0.0")
PID_FILE = "/tmp/zf_vnc_gate.pid"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int:
    try:
        with open(PID_FILE) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status() -> int:
    pid = _read_pid()
    running = _alive(pid) and _port_open(LISTEN_HOST, LISTEN_PORT)
    upstream = _port_open(TARGET_HOST, TARGET_PORT)
    print(f"gate={'running' if running else 'stopped'} pid={pid or '-'} "
          f"upstream_7900={'ok' if upstream else 'unreachable'}")
    return 0 if running else 1


def stop() -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.1)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
    print("gate=stopped")
    return 0


def start() -> int:
    if status() == 0:
        print("gate 已在运行", file=sys.stderr)
        return 0

    if not _port_open(TARGET_HOST, TARGET_PORT):
        print(
            f"无法连接上游 {TARGET_HOST}:{TARGET_PORT}，"
            "请确认 bili-selenium 容器在运行且与本服务同在网络",
            file=sys.stderr,
        )
        return 2

    # 用 socat 做 TCP 转发；没有 socat 时退回 Python 实现
    socat = subprocess.run(
        ["which", "socat"], capture_output=True, text=True
    ).stdout.strip()

    if socat:
        cmd = [
            socat,
            f"TCP-LISTEN:{LISTEN_PORT},bind={LISTEN_HOST},fork,reuseaddr",
            f"TCP:{TARGET_HOST}:{TARGET_PORT}",
        ]
    else:
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "_forward",
        ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # 脱离父进程，容器重启也不影响本进程生命周期
    )
    with open(PID_FILE, "w") as fh:
        fh.write(str(proc.pid))

    for _ in range(30):
        if _port_open(LISTEN_HOST, LISTEN_PORT):
            print(f"gate=started pid={proc.pid} {LISTEN_HOST}:{LISTEN_PORT} -> "
                  f"{TARGET_HOST}:{TARGET_PORT}")
            return 0
        time.sleep(0.1)

    print("启动后端口未就绪", file=sys.stderr)
    return 3


def _forward() -> int:
    """
    纯 Python 的 TCP 转发（无 socat 时的兜底）。

    每个连接一个线程，双向阻塞转发。WebSocket 是长连接，
    所以不能设超时，靠对端关闭来结束。
    """
    import threading

    def pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(64)

    def serve():
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection((TARGET_HOST, TARGET_PORT))
            except OSError:
                client.close()
                continue
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    serve()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="noVNC 网关开关")
    parser.add_argument("action", choices=["start", "stop", "status", "_forward"])
    args = parser.parse_args()

    if args.action == "_forward":
        return _forward()
    return {"start": start, "stop": stop, "status": status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
