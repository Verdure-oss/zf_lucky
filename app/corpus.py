"""
回复语料。

设计依据：站点对抽奖帖自带「一键冲冲冲」按钮（fastReplyContent 默认 '冲冲冲'），
说明这是站方认可的文案。但同一句反复用会显得机械，因此从池中随机选，
并尽量避开最近用过的。

可配置：.env 的 REPLY_CORPUS，用 | 分隔。
"""

from __future__ import annotations

import logging
import random
from typing import List, Optional

log = logging.getLogger(__name__)

DEFAULT_CORPUS = [
    "冲冲冲",
    "来了来了",
    "参与一下",
    "祝我好运",
    "抽我抽我",
    "蹲一个",
    "支持支持",
    "希望能中",
]

# 站点的回复长度限制（实测未探明上限，取保守值）
MIN_LEN = 1
MAX_LEN = 100


class Corpus:
    """回复语料池，带简单的近期去重。"""

    def __init__(self, items: Optional[List[str]] = None):
        cleaned = [str(i).strip() for i in (items or DEFAULT_CORPUS) if str(i).strip()]
        if not cleaned:
            cleaned = list(DEFAULT_CORPUS)
            log.warning("语料池为空，回退默认值")
        self.items = cleaned
        self._recent: List[str] = []

    def pick(self) -> str:
        """
        选一条回复。

        优先选近期没用过的；全部用过则重置近期记录（说明池子太小）。
        """
        fresh = [i for i in self.items if i not in self._recent]
        if not fresh:
            self._recent.clear()
            fresh = list(self.items)

        choice = random.choice(fresh)
        self._recent.append(choice)
        # 只记最近 len-1 条，保证轮换
        if len(self._recent) >= len(self.items):
            self._recent = self._recent[-(len(self.items) - 1):] if len(self.items) > 1 else []
        return self._normalize(choice)

    @staticmethod
    def _normalize(text: str) -> str:
        """裁到合法长度，去掉控制字符。"""
        text = " ".join(text.split())
        if len(text) < MIN_LEN:
            return DEFAULT_CORPUS[0]
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN]
        return text
