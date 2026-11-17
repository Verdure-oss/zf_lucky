"""
抽奖判定。

核心事实（实测）：
  情报专栏列表**没有**抽奖标记——26 个字段里没有 is_lottery，
  DOM 里也没有 .tag.lottery 或「抽签福利」字样。
  因此必须逐帖查详情，看 flow.lottery 是否存在。

判定结果永久缓存：一个帖子一旦判定为非抽奖，就不会再变；
判定为抽奖的需要每轮复查 status（感知是否已开奖）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .api import Api, ApiError, CaptchaRequired, RateLimited, SessionExpired
from .scanner import ScannedFlow

log = logging.getLogger(__name__)

# lottery.status 语义（实测 + 前端代码确认）
STATUS_PENDING = 1  # 待抽奖


@dataclass
class LotteryInfo:
    """一个帖子的抽奖判定结果。"""

    is_lottery: bool
    status: Optional[int] = None
    lottery_at: Optional[str] = None
    needs_follow: bool = False
    prizes: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """是否仍在抽奖期（值得参与）。"""
        return self.is_lottery and self.status == STATUS_PENDING

    def prizes_summary(self) -> str:
        """奖品摘要，用于落库与面板展示。"""
        if not self.prizes:
            return ""
        parts = []
        for p in self.prizes[:5]:
            name = p.get("name") or "?"
            cnt = p.get("cnt")
            parts.append(f"{name} x{cnt}" if cnt else name)
        if len(self.prizes) > 5:
            parts.append(f"等{len(self.prizes)}项")
        return " / ".join(parts)

    def prizes_json(self) -> str:
        return json.dumps(self.prizes, ensure_ascii=False) if self.prizes else ""


def parse_lottery(flow: Dict[str, Any]) -> LotteryInfo:
    """
    从 flow 详情解析抽奖信息。

    兼容 prizesGroup（新版分组结构）与 prizes（旧版平铺结构）。
    """
    lot = flow.get("lottery")
    if not isinstance(lot, dict):
        return LotteryInfo(is_lottery=False)

    prizes: List[Dict[str, Any]] = []
    groups = lot.get("prizesGroup")
    if isinstance(groups, list) and groups:
        for g in groups:
            if not isinstance(g, dict):
                continue
            prizes.append({"name": g.get("name"), "cnt": g.get("cnt")})
    else:
        flat = lot.get("prizes")
        if isinstance(flat, list):
            for p in flat:
                if isinstance(p, dict):
                    prizes.append({"name": p.get("name"), "cnt": p.get("cnt")})

    filters = lot.get("filters") or {}
    return LotteryInfo(
        is_lottery=True,
        status=lot.get("status"),
        lottery_at=lot.get("lottery_at") or lot.get("time_str"),
        needs_follow=bool(filters.get("follow")),
        prizes=prizes,
        raw=lot,
    )


class DetailChecker:
    """逐帖查询详情并判定。"""

    def __init__(self, api: Api):
        self.api = api
        self.detail_count = 0

    def check(self, flow_hash: str) -> LotteryInfo:
        """
        查询单个帖子。

        :raises RateLimited: 站点限流（上层应暂停）
        :raises CaptchaRequired: 出现验证码（上层应暂停）
        :raises SessionExpired: 登录失效（上层应中止）
        :raises ApiError: 其他错误（上层应记录并继续下一帖）
        """
        data = self.api.flow_detail(flow_hash)
        self.detail_count += 1
        flow = data.get("flow") or {}
        info = parse_lottery(flow)
        # 把 flow 本体也带上，上层可能要 userId 等
        info.raw = {"lottery": info.raw, "flow": flow}
        return info


def extract_flow_meta(info: LotteryInfo) -> Dict[str, Any]:
    """从判定结果里取出落库所需的 flow 元信息。"""
    flow = (info.raw or {}).get("flow") or {}
    user = flow.get("user") or {}
    return {
        "flow_id": flow.get("id"),
        "title": (flow.get("title") or "").strip(),
        "created_at": flow.get("created_at"),
        "author_uid": user.get("id"),
        "author_hash": user.get("hash_id"),
        "author_nick": user.get("nickname"),
    }


class JudgeResult:
    """一轮判定的汇总。"""

    def __init__(self) -> None:
        self.checked = 0
        self.lottery_found = 0
        self.active_found = 0
        self.errors: List[Dict[str, str]] = []
        self.aborted: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"JudgeResult(checked={self.checked}, lottery={self.lottery_found}, "
            f"active={self.active_found}, errors={len(self.errors)}, "
            f"aborted={self.aborted})"
        )


def judge_pending(
    db,
    checker: DetailChecker,
    max_items: int = 200,
) -> JudgeResult:
    """
    对数据库中「待判定 / 待复查」的帖子逐条查详情并落库。

    中止条件：限流、验证码、登录失效——这三类都是「继续跑只会更糟」的情况。
    单帖的普通错误只记录，不中断，避免一条坏数据阻塞整轮。
    """
    result = JudgeResult()
    pending = db.flows_need_detail(limit=max_items)
    log.info("待判定/复查：%d 条", len(pending))

    for row in pending:
        hash_ = row["hash"]
        try:
            info = checker.check(hash_)
            result.checked += 1

            if not info.is_lottery:
                db.update_lottery(hash_, is_lottery=0)
                continue

            result.lottery_found += 1
            meta = extract_flow_meta(info)
            # 详情里的标题比列表更权威，顺带补全作者信息
            db.upsert_flow(
                {
                    "hash": hash_,
                    "flow_id": meta["flow_id"],
                    "title": meta["title"] or row.get("title"),
                    "created_at": meta["created_at"] or row.get("created_at"),
                    "author_uid": meta["author_uid"],
                    "author_hash": meta["author_hash"],
                    "author_nick": meta["author_nick"],
                }
            )
            db.update_lottery(
                hash_,
                is_lottery=1,
                lottery_status=info.status,
                lottery_at=info.lottery_at,
                prizes=info.prizes_json(),
                needs_follow=1 if info.needs_follow else 0,
            )

            if info.is_active:
                result.active_found += 1
                db.ensure_participation(hash_)
                log.info(
                    "抽奖帖：%s | %s | 开奖 %s | 奖品 %s",
                    hash_,
                    (meta["title"] or "")[:24],
                    info.lottery_at,
                    info.prizes_summary(),
                )
            else:
                log.debug("抽奖已结束：%s（status=%s）", hash_, info.status)

        except CaptchaRequired as exc:
            result.aborted = f"captcha: {exc}"
            log.error("出现验证码，中止判定：%s", exc)
            break
        except RateLimited as exc:
            result.aborted = f"rate_limited: {exc}"
            log.error("被限流，中止判定：%s", exc)
            break
        except SessionExpired as exc:
            result.aborted = f"session_expired: {exc}"
            log.error("登录失效，中止判定：%s", exc)
            break
        except ApiError as exc:
            # 单帖失败不中断
            log.warning("判定 %s 失败：%s", hash_, exc)
            result.errors.append({"hash": hash_, "error": str(exc)})
            db.touch_checked(hash_)
        except Exception as exc:  # 防御：未知错误也不让整轮崩
            log.exception("判定 %s 出现未预期错误", hash_)
            result.errors.append({"hash": hash_, "error": f"{type(exc).__name__}: {exc}"})
            db.touch_checked(hash_)

    return result
