"""
日志读取。

面板要能看日志，但直接 read_text() 整份文件在日志几 MB 时会卡住面板、
也会把内存打满。因此按「从文件尾部倒着读」的方式取最后 N 行。

同时要考虑日志轮转：RotatingFileHandler 会把旧日志改名成 .1 .2，
面板应当能一并查看，否则出事时最需要的那部分刚好被转走了。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

MAX_LINES = 2000
DEFAULT_LINES = 300
_CHUNK = 64 * 1024

# 日志行格式：2026-11-18 18:23:45 - INFO    - app.db - 消息
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+-\s+"
    r"(?P<level>[A-Z]+)\s+-\s+"
    r"(?P<logger>[^\s]+)\s+-\s+"
    r"(?P<msg>.*)$"
)

# 终端颜色转义（Werkzeug 会在日志里塞 ANSI 码），面板显示前必须去掉
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def list_log_files(log_dir: str, base: str = "zf_lucky") -> List[Dict[str, Any]]:
    """列出日志目录下的日志文件（含轮转文件），按新旧排序。"""
    files: List[Dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(log_dir)):
            if not name.startswith(base):
                continue
            path = os.path.join(log_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append(
                {
                    "name": name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
    except OSError as exc:
        log.warning("读取日志目录失败：%s", exc)
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def tail_lines(path: str, n: int = DEFAULT_LINES, max_bytes: int = 8 * 1024 * 1024) -> List[str]:
    """
    读取文件最后 n 行。

    从文件末尾分块倒读，避免把整个文件读进内存。
    max_bytes 限制最多回读多少字节，防止日志畸形（比如没有换行）时吃满内存。
    """
    n = max(1, min(int(n or DEFAULT_LINES), MAX_LINES))
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max_bytes)
            fh.seek(size - block)
            data = fh.read(block)
    except OSError as exc:
        log.warning("读取日志失败（%s）：%s", path, exc)
        return []

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # 若发生过截断，首行可能是半行，丢掉避免误导
    if block < size and lines:
        lines = lines[1:]
    return lines[-n:]


def parse_line(line: str) -> Dict[str, Any]:
    """把一行拆成结构化字段，解析不了就整行当消息。"""
    line = strip_ansi(line)
    m = _LINE_RE.match(line)
    if not m:
        return {"ts": "", "level": "", "logger": "", "msg": line, "raw": line}
    return {
        "ts": m.group("ts"),
        "level": m.group("level"),
        "logger": m.group("logger"),
        "msg": m.group("msg"),
        "raw": line,
    }


def tail(
    log_dir: str,
    n: int = DEFAULT_LINES,
    level: str = "",
    keyword: str = "",
    base: str = "zf_lucky",
) -> Dict[str, Any]:
    """
    取尾部日志，可选按级别与关键字过滤。

    :param level: 最低级别（INFO/WARNING/ERROR），空表示全部
    :param keyword: 消息中包含的子串（不区分大小写）
    """
    files = list_log_files(log_dir, base)
    if not files:
        return {"files": [], "lines": [], "total": 0, "file": None, "truncated": False}

    # 默认看最新的那个文件
    target = files[0]["name"]
    path = os.path.join(log_dir, target)

    raw_lines = tail_lines(path, n=n)
    parsed = [parse_line(line) for line in raw_lines]

    levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    min_idx = 0
    if level.upper() in levels:
        min_idx = levels.index(level.upper())

    out: List[Dict[str, Any]] = []
    kw = (keyword or "").strip().lower()
    for item in parsed:
        lv = (item.get("level") or "").upper()
        if min_idx and lv in levels and levels.index(lv) < min_idx:
            continue
        if lv not in levels and min_idx:
            # 无级别的行（如 Flask 访问日志）在过滤时保留，避免漏掉上下文
            pass
        if kw and kw not in item.get("raw", "").lower():
            continue
        out.append(item)

    return {
        "files": files,
        "file": target,
        "lines": out,
        "total": len(parsed),
        "matched": len(out),
        "truncated": len(parsed) >= n,
    }
