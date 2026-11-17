"""
日志初始化。

统一格式，同时输出到 stdout（容器日志）与文件（便于回看）。
敏感信息（Cookie）在调用处脱敏，本模块只负责格式。
"""

from __future__ import annotations

import logging
import os
import sys


def setup(level: int = logging.INFO, log_dir: str = "/app/logs") -> None:
    """配置根 logger。重复调用安全。"""
    root = logging.getLogger()
    if getattr(root, "_zf_configured", False):
        return

    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s - %(levelname)-7s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台：容器日志（docker logs / dozzle 可见）
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件：滚动写入，防止长跑撑爆磁盘
    try:
        os.makedirs(log_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(
            os.path.join(log_dir, "zf_lucky.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # 目录不可写时不应导致启动失败
        root.warning("无法写入日志文件（%s），仅输出到控制台", exc)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)

    root._zf_configured = True  # type: ignore[attr-defined]
