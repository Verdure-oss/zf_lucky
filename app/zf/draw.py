"""
开奖结果检测。

判定依据（实测自真实已开奖帖）：

    lottery.status      = 2      表示已结束（1 = 待抽奖）
    prizesGroup[].list[].rewardUser   中奖者，未开奖时为 null
        rewardUser.id       数字 uid，可与当前账号比对
        rewardUser.nickname 昵称

因此：
    中奖   = 已开奖 且 任一 prize.rewardUser.id == 我的 uid
    未中奖 = 已开奖 且 没有我的 uid

注意不能靠昵称比对：昵称可改且有重名，必须用数字 id。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# lottery.status 语义
STATUS_PENDING = 1  # 待抽奖
STATUS_ENDED = 2    # 已结束（含已开奖）

# 数据库中记录的抽奖结果
OUTCOME_WON = "won"
OUTCOME_LOST = "lost"
OUTCOME_PENDING = "pending"


@dataclass
class DrawResult:
    """一次开奖检测的结果。"""

    hash: str
    status: Optional[int]
    ended: bool
    won: bool
    winners: List[Dict[str, Any]]
    prize_name: Optional[str] = None

    @property
    def outcome(self) -> str:
        if not self.ended:
            return OUTCOME_PENDING
        return OUTCOME_WON if self.won else OUTCOME_LOST


def parse_draw(flow: Dict[str, Any], my_uid: int) -> DrawResult:
    """
    从 flow 详情解析开奖结果。

    :param my_uid: 我的数字 uid，用于判断是否中奖
    """
    hash_ = flow.get("hash_id") or ""
    lot = flow.get("lottery") or {}

    if not isinstance(lot, dict):
        return DrawResult(hash=hash_, status=None, ended=False, won=False, winners=[])

    status = lot.get("status")
    # 未开奖：status 为待抽奖，且所有 rewardUser 都是 null
    ended = status is not None and status != STATUS_PENDING

    winners: List[Dict[str, Any]] = []
    my_prize: Optional[str] = None

    groups = lot.get("prizesGroup") or []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for item in g.get("list") or []:
            if not isinstance(item, dict):
                continue
            ru = item.get("rewardUser")
            if not isinstance(ru, dict):
                continue
            uid = ru.get("id")
            if uid is None:
                continue
            winners.append(
                {
                    "uid": int(uid),
                    "nickname": ru.get("nickname") or "",
                    "prize": item.get("name") or g.get("name") or "",
                }
            )
            # 用数字 id 比对，昵称不可靠
            if int(uid) == int(my_uid) and my_prize is None:
                my_prize = item.get("name") or g.get("name") or ""

    # 有中奖者出现即视为已开奖（有些帖 status 更新滞后）
    if winners:
        ended = True

    return DrawResult(
        hash=hash_,
        status=status,
        ended=ended,
        won=my_prize is not None,
        winners=winners,
        prize_name=my_prize,
    )


def check_finished_flows(db, api, my_uid: int, limit: int = 200) -> Dict[str, Any]:
    """
    遍历「已完成参与但还没记录开奖结果」的帖子，更新中奖状态。

    只查已参与的帖：没参与的帖中奖与否与我无关，没必要浪费请求。

    :return: 统计摘要
    """
    rows = db.flows_awaiting_result(limit=limit)
    if not rows:
        return {"checked": 0, "won": 0, "lost": 0, "pending": 0}

    log.info("待检测开奖结果：%d 条", len(rows))
    stats = {"checked": 0, "won": 0, "lost": 0, "pending": 0, "errors": 0, "won_list": []}

    for row in rows:
        hash_ = row["hash"]
        try:
            flow = (api.flow_detail(hash_).get("flow") or {})
            result = parse_draw(flow, my_uid)
            stats["checked"] += 1

            if not result.ended:
                stats["pending"] += 1
                continue

            db.set_outcome(
                hash_,
                outcome=result.outcome,
                prize=result.prize_name,
                winners=result.winners,
            )

            if result.won:
                stats["won"] += 1
                stats["won_list"].append(
                    {"hash": hash_, "title": row.get("title"), "prize": result.prize_name}
                )
                log.info(
                    "🎉 中奖！%s | 奖品：%s", hash_, result.prize_name
                )
            else:
                stats["lost"] += 1
                log.info(
                    "未中奖：%s（%d 位中奖者）", hash_, len(result.winners)
                )

        except Exception as exc:
            stats["errors"] += 1
            log.warning("检测 %s 开奖结果失败：%s", hash_, exc)

    return stats
