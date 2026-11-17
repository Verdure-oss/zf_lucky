"""
情报专栏扫描器。

为什么从 Vue 实例读数据，而不是调列表接口：
  实测 /v2/home/flow/list 无论匿名还是有 Cookie 都返回 20001（限流），
  但从页面正常渲染出列表，说明页面走了不同通道。因此只能读页面。

为什么停止条件不是「看到一条旧的立刻停」：
  实测列表近似按发帖时间倒序，但存在局部倒挂，最大 12.3 小时
  （12 天 60 条样本里 16 对倒挂，无一超过 24 小时）。
  若看到第 3 条（11-16 20:10，早于第 2 条的 11-17 08:30）就判定到边界，
  会漏掉第 4 条（11-17 08:30）。所以要求「连续 N 条都早于边界」才停。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # 纯算法不应依赖 selenium，只在类型检查时引入
    from .driver import Driver

log = logging.getLogger(__name__)

TAG_URL = "https://www.zfrontier.com/app/tag/{tag_id}"

# 从页面 Vue 实例取列表数据的脚本。
# 从任一卡片向上找含 tabList 的组件；找不到再退回 $props.flow 逐条抓。
_GRAB_JS = """
var done = arguments[arguments.length - 1];
try {
    var cards = document.querySelectorAll('.list-flow');
    if (!cards.length) { done({ ok: false, reason: 'no-cards', items: [] }); return; }

    var vm = cards[0].__vue__;
    var guard = 0;
    while (vm && !(vm.$data && vm.$data.tabList) && guard++ < 20) { vm = vm.$parent; }

    var list = null;
    if (vm && vm.$data && vm.$data.tabList) {
        var tl = vm.$data.tabList;
        // tabList 可能是对象（{normal:{list:[]}}）或数组
        if (tl.normal && tl.normal.list) list = tl.normal.list;
        else if (Array.isArray(tl)) {
            for (var i = 0; i < tl.length; i++) {
                if (tl[i] && tl[i].list) { list = tl[i].list; break; }
            }
        }
    }

    if (!list || !list.length) {
        // 兜底：逐卡片读 $props.flow
        list = [];
        for (var k = 0; k < cards.length; k++) {
            var c = cards[k].__vue__;
            if (c && c.$props && c.$props.flow) list.push(c.$props.flow);
        }
    }

    done({
        ok: true,
        total: list.length,
        items: list.map(function (f) {
            return {
                hash: f.hash_id,
                hashId: f.id,
                title: f.title,
                createdAt: f.created_at,
                userId: f.user ? f.user.id : null,
                userHash: f.user ? f.user.hash_id : null,
                userNick: f.user ? f.user.nickname : null
            };
        })
    });
} catch (e) {
    done({ ok: false, reason: String(e), items: [] });
}
"""


@dataclass
class ScannedFlow:
    """扫描到的一条帖子。"""

    hash: str
    flow_id: Optional[int]
    title: str
    created_at: datetime
    author_uid: Optional[int]
    author_hash: Optional[str]
    author_nick: Optional[str]

    @classmethod
    def from_js(cls, item: Dict[str, Any]) -> Optional["ScannedFlow"]:
        hash_ = (item.get("hash") or "").strip()
        created = _parse_time(item.get("createdAt"))
        if not hash_ or created is None:
            return None
        return cls(
            hash=hash_,
            flow_id=item.get("hashId"),
            title=(item.get("title") or "").strip(),
            created_at=created,
            author_uid=item.get("userId"),
            author_hash=item.get("userHash"),
            author_nick=item.get("userNick"),
        )

    def to_db(self) -> Dict[str, Any]:
        return {
            "hash": self.hash,
            "flow_id": self.flow_id,
            "title": self.title,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "author_uid": self.author_uid,
            "author_hash": self.author_hash,
            "author_nick": self.author_nick,
        }


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def compute_stop_boundary(
    now: datetime, window_days: int, buffer_days: int, watermark: Optional[datetime]
) -> datetime:
    """
    计算本轮扫描的时间边界（早于此时间即可停止向后翻）。

    两个约束取**较晚者**（即扫描范围更小、更浅的那个）：
      1. 兜底：now - (window + buffer)，永远不扫太老的内容；
      2. 增量：watermark - buffer，从上次水位回退几天形成重叠，
         避免上次扫描期间新发的帖子落在缝隙里。

    取较晚者而不是较早者，是因为两个约束都是「不必扫更早」的上界：
      水位很新（如昨天）→ 只需回看 2 天 → 取水位-2天
      水位很旧（如一个月前）→ 硬边界更近 → 取硬边界
    混淆方向会导致每次都全量扫 16 天，增量优化失效。

    首次运行（无 watermark）只受约束 1 限制。
    """
    hard = now - timedelta(days=window_days + buffer_days)
    if watermark is None:
        return hard
    soft = watermark - timedelta(days=buffer_days)
    return max(hard, soft)


def should_stop(
    items: List[ScannedFlow],
    boundary: datetime,
    confirm_count: int,
) -> bool:
    """
    是否已可停止滚动。

    判据：列表从新到旧排列，因此从**末尾**往前数，
    连续 confirm_count 条都早于 boundary 才停。

    用「连续」而非「出现过」，是为了抵抗局部倒挂；
    用 confirm_count 而非 1，是因为实测倒挂最多影响 2 条，
    取 10 留了 5 倍裕量。
    """
    if not items:
        return False

    if confirm_count <= 0:
        # 注意不能写成 items[-confirm_count:]，confirm_count=0 时
        # [-0:] 等价于 [0:]（整个列表），会静默退化成错误语义
        return items[-1].created_at < boundary

    if len(items) < confirm_count:
        # 条数不足时，全部早于边界才认为扫完了
        return all(i.created_at < boundary for i in items)

    tail = items[-confirm_count:]
    return all(i.created_at < boundary for i in tail)


class Scanner:
    """情报专栏列表扫描器。"""

    def __init__(
        self,
        driver: "Driver",
        tag_id: int,
        confirm_count: int = 10,
        max_scroll: int = 15,
        max_items: int = 500,
    ):
        self.driver = driver
        self.tag_id = tag_id
        self.confirm_count = confirm_count
        self.max_scroll = max_scroll
        self.max_items = max_items

    def _grab(self) -> Dict[str, Any]:
        result = self.driver.js_async(_GRAB_JS, timeout=30)
        if not isinstance(result, dict):
            return {"ok": False, "reason": f"异常返回 {result!r}", "items": []}
        return result

    def _scroll(self) -> int:
        """滚到底触发瀑布流加载，返回滚动后文档高度。"""
        return int(
            self.driver.js(
                "window.scrollTo(0, document.body.scrollHeight);"
                "return document.body.scrollHeight;"
            )
            or 0
        )

    def scan(self, boundary: datetime) -> List[ScannedFlow]:
        """
        扫描情报专栏，返回去重后的帖子列表（新→旧）。

        :param boundary: 停止边界，连续 confirm_count 条早于它即停
        """
        url = TAG_URL.format(tag_id=self.tag_id)
        log.info("打开情报专栏：%s（边界 %s）", url, boundary.strftime("%Y-%m-%d %H:%M"))
        self.driver.get(url)
        time.sleep(2)

        first = self._grab()
        if not first.get("ok"):
            raise RuntimeError(f"读取列表失败：{first.get('reason')}")

        seen: Dict[str, ScannedFlow] = {}
        order: List[ScannedFlow] = []
        last_count = 0
        stale_rounds = 0

        for round_no in range(self.max_scroll + 1):
            grabbed = self._grab()
            if not grabbed.get("ok"):
                log.warning("第 %d 轮读取失败：%s", round_no, grabbed.get("reason"))
                break

            raw_items = grabbed.get("items") or []
            for raw in raw_items:
                flow = ScannedFlow.from_js(raw)
                if flow is None or flow.hash in seen:
                    continue
                seen[flow.hash] = flow
                order.append(flow)

            log.info(
                "第 %d 轮：页面 %d 条，累计 %d 条，最新 %s，最旧 %s",
                round_no,
                len(raw_items),
                len(order),
                order[0].created_at.strftime("%m-%d %H:%M") if order else "-",
                order[-1].created_at.strftime("%m-%d %H:%M") if order else "-",
            )

            # 已拿到足够多且末尾连续够久早于边界 → 完成
            if should_stop(order, boundary, self.confirm_count):
                log.info(
                    "满足停止条件：末尾连续 %d 条早于 %s",
                    self.confirm_count,
                    boundary.strftime("%Y-%m-%d %H:%M"),
                )
                break

            if len(order) >= self.max_items:
                log.warning("达到条数上限 %d，停止", self.max_items)
                break

            # 滚动加载更多
            before = len(order)
            height_before = self._scroll()
            time.sleep(3.5)

            after = self._grab()
            after_count = len(after.get("items") or []) if after.get("ok") else 0
            height_after = int(self.driver.js("return document.body.scrollHeight") or 0)

            if after_count == last_count and height_after <= height_before:
                stale_rounds += 1
                if stale_rounds >= 2:
                    log.info("连续 %d 轮无新内容，判定已到列表末尾", stale_rounds)
                    break
            else:
                stale_rounds = 0
            last_count = after_count

            if len(order) == before and after_count and after_count <= len(raw_items):
                log.debug("本轮未获得新条目")

        log.info("扫描完成，共 %d 条", len(order))
        return order
