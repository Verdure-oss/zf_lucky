"""
配置加载。

所有配置来自环境变量（容器场景由 .env 经 env_file 注入）。
设计原则：
  1. 每项都有合理默认值，只有 Cookie 是必填；
  2. 非法值不抛异常导致容器起不来，而是回退默认值并记警告；
  3. 提供明确的校验方法，让问题在启动时暴露而不是跑到一半才炸。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)


def _str(key: str, default: str = "") -> str:
    """读字符串，去空白，空串回退默认值。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _int(key: str, default: int) -> int:
    raw = _str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("配置 %s=%r 不是合法整数，回退默认值 %d", key, raw, default)
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass
class Config:
    """运行期配置快照。"""

    # ---- 账号 ----
    cookie: str = ""

    # ---- 调度 ----
    run_at: str = "09:00"
    run_on_start: bool = False

    # ---- 扫描 ----
    scan_window_days: int = 14
    scan_buffer_days: int = 2
    scan_confirm_count: int = 10
    scan_max_scroll: int = 15
    scan_max_items: int = 500

    # ---- 回复 ----
    reply_corpus: List[str] = field(default_factory=list)

    # ---- 频控 ----
    request_delay_min: int = 2
    request_delay_max: int = 5
    max_write_per_run: int = 30
    max_retry: int = 3

    # ---- 依赖 ----
    selenium_url: str = "http://bili-selenium:4444"

    # ---- 存储 ----
    db_path: str = "/app/data/zf_lucky.db"
    panel_port: int = 8080

    # ---- 常量（不通过环境变量配置）----
    TAGS_INFO: int = 2007
    SITE: str = "https://www.zfrontier.com"

    @property
    def scan_boundary_days(self) -> int:
        """边界天数 = 窗口 + 边距。"""
        return self.scan_window_days + self.scan_buffer_days

    def validate(self) -> List[str]:
        """
        校验配置，返回问题列表（空列表表示通过）。

        只把「必然导致跑不通」的列为错误；可自动修正的已在加载时处理。
        """
        problems: List[str] = []

        if not self.cookie:
            problems.append(
                "未配置 ZF_COOKIE。请在 .env 中填入 zFrontier 的完整 Cookie 字符串。"
            )

        if not _TIME_RE.match(self.run_at):
            problems.append(
                f"RUN_AT={self.run_at!r} 格式非法，应为 HH:MM（如 09:00）。"
            )

        if self.scan_window_days < 1:
            problems.append(f"SCAN_WINDOW_DAYS={self.scan_window_days} 必须 ≥ 1。")

        if self.scan_buffer_days < 0:
            problems.append(f"SCAN_BUFFER_DAYS={self.scan_buffer_days} 不能为负。")

        if self.scan_confirm_count < 1:
            problems.append(f"SCAN_CONFIRM_COUNT={self.scan_confirm_count} 必须 ≥ 1。")

        if self.request_delay_min > self.request_delay_max:
            problems.append(
                f"REQUEST_DELAY_MIN({self.request_delay_min}) "
                f"> REQUEST_DELAY_MAX({self.request_delay_max})。"
            )

        if not self.reply_corpus:
            problems.append("REPLY_CORPUS 为空，无法生成回复内容。")

        return problems


def load() -> Config:
    """从环境变量构造配置。"""
    corpus_raw = _str(
        "REPLY_CORPUS",
        "冲冲冲|来了来了|参与一下|祝我好运|抽我抽我|蹲一个|支持支持|希望能中",
    )
    corpus = [item.strip() for item in corpus_raw.split("|") if item.strip()]

    delay_min = _int("REQUEST_DELAY_MIN", 2)
    delay_max = _int("REQUEST_DELAY_MAX", 5)
    # 交换而非报错：区间反了仍能工作，只是顺序问题
    if delay_min > delay_max:
        log.warning(
            "REQUEST_DELAY_MIN(%d) > MAX(%d)，已自动交换", delay_min, delay_max
        )
        delay_min, delay_max = delay_max, delay_min

    cfg = Config(
        cookie=_str("ZF_COOKIE"),
        run_at=_str("RUN_AT", "09:00"),
        run_on_start=_bool("RUN_ON_START", False),
        scan_window_days=_int("SCAN_WINDOW_DAYS", 14),
        scan_buffer_days=_int("SCAN_BUFFER_DAYS", 2),
        scan_confirm_count=_int("SCAN_CONFIRM_COUNT", 10),
        scan_max_scroll=_int("SCAN_MAX_SCROLL", 15),
        scan_max_items=_int("SCAN_MAX_ITEMS", 500),
        reply_corpus=corpus,
        request_delay_min=delay_min,
        request_delay_max=delay_max,
        max_write_per_run=_int("MAX_WRITE_PER_RUN", 30),
        max_retry=_int("MAX_RETRY", 3),
        selenium_url=_str("SELENIUM_URL", "http://bili-selenium:4444").rstrip("/"),
        db_path=_str("DB_PATH", "/app/data/zf_lucky.db"),
        panel_port=_int("PANEL_PORT", 8080),
    )
    return cfg


def mask_cookie(cookie: str) -> str:
    """
    Cookie 脱敏，用于日志。

    绝不把完整 Cookie 写进日志：它有等价于密码的效力。
    """
    if not cookie:
        return "(空)"
    parts = [p.strip() for p in cookie.split(";") if p.strip()]
    names = [p.split("=", 1)[0] for p in parts if "=" in p]
    if not names:
        return f"(无法解析，长度 {len(cookie)})"
    return f"已配置 {len(names)} 项: {', '.join(names[:6])}" + (
        "..." if len(names) > 6 else ""
    )
