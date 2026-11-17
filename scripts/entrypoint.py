"""
容器入口：等待依赖就绪后启动主程序。

selenium 是外部容器，compose 的 depends_on 只保证启动顺序，
不代表 Grid 真的 ready（实测容器起来后还要几十秒）。
因此这里做一次带超时的就绪探测，避免应用首启即崩。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.zf.driver import wait_selenium_ready  # noqa: E402


def main() -> int:
    selenium_url = os.getenv("SELENIUM_URL", "http://bili-selenium:4444")
    timeout = int(os.getenv("WAIT_TIMEOUT", "180"))

    print(f"[entrypoint] 等待 Selenium 就绪：{selenium_url}（上限 {timeout}s）", flush=True)
    if wait_selenium_ready(selenium_url, timeout=timeout):
        print("[entrypoint] Selenium 已就绪", flush=True)
    else:
        # 依赖不可用仍继续启动：主程序对单次失败有兜底，
        # 直接退出只会触发 restart 循环，反而更难排查。
        print(
            "[entrypoint] 警告：Selenium 未在超时内就绪，仍继续启动（请查看日志）",
            flush=True,
        )

    print("[entrypoint] 启动主程序", flush=True)
    os.execvp(sys.executable, [sys.executable, "-m", "app.main"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
