"""
程序入口：调度 + 生命周期。

设计要点：
  1. 单实例运行：用文件锁防止「定时任务 + 手动触发」并发执行，
     并发会成倍放大写操作，是触发风控最直接的方式。
  2. 单次失败不让进程退出：容器 restart 策略会变成重启循环，
     反而更难排查（既有项目的教训）。
  3. 支持手动触发：面板写入一个触发文件，主循环发现后立即跑一轮。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from . import config as cfg_mod
from .db import Database
from .logging_setup import setup as setup_logging
from .runner import Runner

log = logging.getLogger(__name__)

# 手动触发：面板/运维只需 touch 这个文件
TRIGGER_FILE = "/tmp/zf_lucky_trigger"
# 单实例锁
LOCK_FILE = "/tmp/zf_lucky.lock"


class SingleInstance:
    """
    基于文件的单实例锁。

    用 O_CREAT|O_EXCL 原子创建；进程崩溃留下的陈旧锁会在
    「记录 pid 且进程不存在」时自动清理，避免永久卡死。
    """

    def __init__(self, path: str):
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
            return True
        except FileExistsError:
            # 判断是否陈旧锁
            try:
                with open(self.path) as fh:
                    pid = int(fh.read().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                return False
            log.warning("清理陈旧锁（pid=%s 已不存在）", pid)
            self.release()
            # 只重试一次，避免极端情况下无限递归
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.acquired = True
                return True
            except FileExistsError:
                return False

    def release(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.release()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _start_panel(cfg) -> None:
    """
    在后台线程启动 Web 面板。

    面板与主流程共用同一个 SQLite（WAL 模式支持并发读）。
    面板起失败不阻塞主流程——它只是观察窗，不是必需组件。
    """
    try:
        from threading import Thread

        from .web import panel

        panel.init(cfg.db_path, cfg)
        thread = Thread(
            target=panel.run_server,
            args=(cfg.panel_port,),
            daemon=True,
            name="panel",
        )
        thread.start()
        log.info("面板已启动：http://0.0.0.0:%d", cfg.panel_port)
    except Exception:
        log.exception("面板启动失败（不影响主流程）")


def next_run_at(target: str, now: Optional[datetime] = None) -> datetime:
    """
    下一个 HH:MM 的**精确**时间点。

    直接构造 datetime 而不是用「当前时间 + 浮点秒数」：
    后者会因微秒累积导致算出来的时间比目标早不到 1 秒，
    日志显示成 08:59:59，且调度循环会提前 1 秒触发。
    """
    now = now or datetime.now()
    hh, mm = (int(x) for x in target.split(":"))
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt


def seconds_until(target: str, now: Optional[datetime] = None) -> float:
    """
    距离下一个 HH:MM 还有多少秒。

    已经过了今天的点就排到明天。
    """
    now = now or datetime.now()
    return (next_run_at(target, now) - now).total_seconds()


def _stop_watch_vnc_if_needed() -> None:
    """
    如果本轮是「运行并观看」触发的，跑完把网关关掉。

    自动化运行时会开很多页面，忘记关就是长期暴露，
    因此这里自动收尾，而不是等用户手动关。
    """
    import os as _os
    import subprocess
    import sys as _sys

    flag = "/tmp/zf_watch_vnc"
    if not _os.path.exists(flag):
        return
    try:
        _os.unlink(flag)
    except OSError:
        pass
    try:
        subprocess.run(
            [_sys.executable, "/app/scripts/vnc_gate.py", "stop"],
            capture_output=True, text=True, timeout=15,
        )
        log.info("本轮为「运行并观看」，已自动关闭浏览器观看")
    except Exception as exc:
        log.warning("关闭浏览器观看失败：%s", exc)


def run_once(cfg, db: Database) -> None:
    """执行一轮，异常不外溢（避免进程退出）。"""
    lock = SingleInstance(LOCK_FILE)
    if not lock.acquire():
        log.warning("已有实例在运行，本次跳过")
        return
    try:
        Runner(cfg, db).run()
    except Exception:
        log.exception("本轮运行出现未捕获异常（已忽略，进程继续）")
    finally:
        lock.release()
        _stop_watch_vnc_if_needed()


def main() -> int:
    setup_logging()
    cfg = cfg_mod.load()

    log.info("zf_lucky 启动")
    log.info("Cookie：%s", cfg_mod.mask_cookie(cfg.cookie))
    log.info("定时：每天 %s（时区 %s）", cfg.run_at, os.getenv("TZ", "?"))
    log.info(
        "扫描：窗口 %d 天 + 边距 %d 天 = 边界 %d 天",
        cfg.scan_window_days, cfg.scan_buffer_days, cfg.scan_boundary_days,
    )

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("配置错误：%s", p)
        # 不直接退出：面板仍应起来，让用户看到错误原因
        log.error("配置不完整，将不执行任何实际任务")

    db = Database(cfg.db_path)
    db.connect()

    # 面板作为后台线程启动：即使配置有问题也要能打开，
    # 否则用户看不到具体错误原因。
    _start_panel(cfg)

    stopping = {"flag": False}

    def _handle_signal(signum, _frame):
        log.info("收到信号 %s，准备退出…", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    def _triggered() -> bool:
        if os.path.exists(TRIGGER_FILE):
            try:
                os.unlink(TRIGGER_FILE)
            except OSError:
                pass
            return True
        return False

    # 启动即跑（可选）
    if cfg.run_on_start and not problems:
        log.info("RUN_ON_START=true，立即执行一轮")
        run_once(cfg, db)

    next_run = next_run_at(cfg.run_at)
    log.info("下次定时运行：%s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    while not stopping["flag"]:
        try:
            if _triggered():
                log.info("检测到手动触发")
                if problems:
                    log.error("配置不完整，忽略手动触发")
                else:
                    run_once(cfg, db)
                    next_run = next_run_at(cfg.run_at)
                    log.info("下次定时运行：%s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

            if datetime.now() >= next_run:
                if problems:
                    log.error("配置不完整，跳过定时运行")
                else:
                    run_once(cfg, db)
                next_run = next_run_at(cfg.run_at)
                log.info("下次定时运行：%s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

        except Exception:
            # 调度循环本身绝不因异常退出
            log.exception("调度循环异常（已忽略）")

        time.sleep(5)

    db.close()
    log.info("zf_lucky 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
