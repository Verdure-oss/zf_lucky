"""
.env 安全写入。

这是整个项目里唯一会修改配置文件的代码，因此要求：
  1. **原子写**：先写临时文件再 rename，避免写一半断电导致 .env 损坏；
  2. **保留其余内容**：只替换目标键，其他键、注释、顺序都不动；
  3. **备份**：改动前留一份 .env.bak，出问题可回滚；
  4. **权限收紧**：.env 含 Cookie，写成 0600。

为什么不整体重写 .env：
  用户可能在 .env 里放了很多手写注释与自定义配置，整体重写会全部丢失。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def update_env_var(path: str, key: str, value: str, backup: bool = True) -> Tuple[bool, str]:
    """
    设置 .env 中某个键的值（存在则替换，不存在则追加）。

    :return: (是否成功, 说明)
    """
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return False, f"非法的键名：{key!r}"

    original = _read(path)

    # 逐行处理，保留顺序与注释
    lines = original.splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    out = []
    for line in lines:
        if pattern.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip() == "":
            out.insert(len(out) - 1, f"{key}={value}")
        else:
            out.append(f"{key}={value}")

    new_text = "\n".join(out)
    if not new_text.endswith("\n"):
        new_text += "\n"

    if new_text == original:
        return True, "值未变化，无需写入"

    # 备份
    if backup and os.path.exists(path):
        try:
            bak = f"{path}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, bak)
            os.chmod(bak, 0o600)
            log.info("已备份 .env → %s", os.path.basename(bak))
        except OSError as exc:
            log.warning("备份 .env 失败（继续写入）：%s", exc)

    # 原子写
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"写入失败：{exc}"

    return True, "已写入"


def read_env_var(path: str, key: str) -> Optional[str]:
    """读取 .env 中某个键（用于回显是否已配置，不回显明文）。"""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
    for line in _read(path).splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def mask(value: Optional[str]) -> str:
    """Cookie 等敏感值的脱敏展示。"""
    if not value:
        return "(未配置)"
    parts = [p.strip() for p in value.split(";") if p.strip()]
    names = [p.split("=", 1)[0] for p in parts if "=" in p]
    if not names:
        return f"(已配置，长度 {len(value)})"
    return f"已配置 {len(names)} 项：{', '.join(names[:6])}" + (
        "..." if len(names) > 6 else ""
    )
