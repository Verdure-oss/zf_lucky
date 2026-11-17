"""
参与执行器：关注 → 回复 → 点赞。

三条不可妥协的原则：

1. **顺序固定：先关注、再回复、最后点赞。**
   点赞放最后，使 has_zan==1 在语义上蕴含「回复也已成功」，
   不会出现「赞了但没回复」的永久漏帖。
   （若反过来，中途失败会导致第二天看到已点赞而跳过，永久错过这帖。）

2. **每一步都要回读服务端状态确认，不信任接口返回码。**
   接口返回 ok=0 不等于业务真的生效——审核吞掉、限流静默拦截都可能发生。
   关注回读 followed==1，回复回读列表里能找到自己的 uid，点赞回读 has_zan==1。

3. **失败只影响当前帖，不中断整轮。**
   单帖失败记为 failed，下一轮自动重试；只有限流/验证码/登录失效才中止整轮。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..corpus import Corpus
from ..db import STATE_DONE, STATE_FAILED, STATE_GIVE_UP
from .api import Api, ApiError, CaptchaRequired, RateLimited, SessionExpired

log = logging.getLogger(__name__)


class StepError(RuntimeError):
    """某一步骤失败（可重试）。"""


class BudgetExhausted(RuntimeError):
    """单轮写操作预算用尽，需中止整轮（不是单帖问题）。"""


@dataclass
class ParticipateResult:
    """单帖参与结果。"""

    hash: str
    success: bool
    steps: List[str] = field(default_factory=list)
    fail_reason: Optional[str] = None
    reply_id: Optional[int] = None
    floor: Optional[int] = None
    aborted: Optional[str] = None  # 非空表示需中止整轮


class Participant:
    """负责把一条抽奖帖参与完。"""

    def __init__(
        self,
        api: Api,
        db,
        corpus: Corpus,
        my_uid: int,
        max_retry: int = 3,
        write_budget: int = 30,
    ):
        self.api = api
        self.db = db
        self.corpus = corpus
        self.my_uid = my_uid
        self.max_retry = max_retry
        self.write_budget = write_budget
        self.write_used = 0
        # 当前处理的帖子 hash。放在实例上而非类属性：
        # 类属性会被所有实例共享，多帖串行时互相覆盖。
        self._current_hash: str = ""

    # ---------- 关注 ----------

    def ensure_followed(self, author_uid: int, author_hash: Optional[str]) -> None:
        """
        确保已关注作者。已关注则跳过。

        回读确认：/v2/user/detail 返回的 user.followed 必须是 1。
        """
        # 先查当前关注状态，避免重复关注
        followed = None
        if author_hash:
            detail = self.api.user_detail(author_hash)
            user = detail.get("user") or {}
            if "followed" in user:
                followed = int(user.get("followed") or 0)

        if followed == 1:
            log.info("  已关注作者 %s，跳过", author_uid)
            return

        self._consume_write()
        self.api.follow(author_uid)
        log.info("  已发送关注请求：%s", author_uid)

        # 回读确认
        if not author_hash:
            raise StepError("缺少作者 hash，无法确认关注状态")

        for attempt in range(3):
            detail = self.api.user_detail(author_hash)
            user = detail.get("user") or {}
            if int(user.get("followed") or 0) == 1:
                log.info("  关注已确认：%s", author_uid)
                return
            import time

            time.sleep(1.5 + attempt)
        raise StepError(f"关注 {author_uid} 后回读仍为未关注")

    # ---------- 回复 ----------

    def send_reply(self, flow_id: int) -> Dict[str, Any]:
        """
        发回复并回读确认。

        回读用 unfold=1：抽奖帖默认折叠抽奖楼层，
        不展开的话查不到自己那条，会误判为「回复失败」进而重复回复。
        """
        content = self.corpus.pick()
        self._consume_write()
        data = self.api.reply(flow_id, content)
        reply = data.get("reply") or {}
        reply_id = reply.get("id")
        floor = reply.get("floor")
        log.info("  已发送回复（id=%s, floor=%s）：%s", reply_id, floor, content)

        if not self._find_my_reply(flow_id):
            raise StepError("回复后回读未找到本人的回复")
        log.info("  回复已确认存在于服务端")
        return {"reply_id": reply_id, "floor": floor, "content": content}

    def _find_my_reply(self, flow_id: int, max_pages: int = 3) -> bool:
        """
        在回复列表里找本人的回复。

        默认按最新排序，本人的刚发的通常在前两页；
        最多翻 3 页避免在几百条回复的帖子上耗太久。
        """
        import time

        for page in range(1, max_pages + 1):
            data = self.api.flow_reply_list(flow_id, page=page)
            items = data.get("list") or []
            for item in items:
                if int(item.get("uid") or 0) == self.my_uid:
                    return True
            if not data.get("has_more"):
                return False
            time.sleep(1)
        return False

    # ---------- 点赞 ----------

    def like_flow(self, flow_id: int) -> None:
        """
        点赞并回读确认 has_zan==1。

        必须先查 has_zan：已点赞的再点会变成取消赞，
        反而把「已参与」标记抹掉。
        """
        info = self.api.flow_detail(self._current_hash)
        flow = info.get("flow") or {}
        if int(flow.get("has_zan") or 0) == 1:
            log.info("  已点赞，跳过")
            return

        self._consume_write()
        self.api.zan_flow(flow_id)
        log.info("  已发送点赞请求")

        for attempt in range(3):
            info = self.api.flow_detail(self._current_hash)
            flow = info.get("flow") or {}
            if int(flow.get("has_zan") or 0) == 1:
                log.info("  点赞已确认")
                return
            import time

            time.sleep(1.5 + attempt)
        raise StepError("点赞后回读 has_zan 仍为 0")

    # ---------- 预算 ----------

    def _consume_write(self) -> None:
        if self.write_used >= self.write_budget:
            raise BudgetExhausted(
                f"已达单轮写操作上限 {self.write_budget}，中止整轮"
            )
        self.write_used += 1

    @property
    def budget_left(self) -> int:
        return max(0, self.write_budget - self.write_used)

    # ---------- 主流程 ----------

    def participate(self, flow_row: Dict[str, Any]) -> ParticipateResult:
        """
        对一条抽奖帖完成关注+回复+点赞。

        注意：调用方负责在开始时把 state 置为 pending。
        """
        hash_ = flow_row["hash"]
        self._current_hash = hash_
        flow_id = flow_row.get("flow_id")
        author_uid = flow_row.get("author_uid")
        author_hash = flow_row.get("author_hash")

        result = ParticipateResult(hash=hash_, success=False)

        if not flow_id:
            result.fail_reason = "缺少 flow_id"
            self._on_fail(hash_, result.fail_reason)
            return result
        if not author_uid:
            result.fail_reason = "缺少作者 uid，无法关注"
            self._on_fail(hash_, result.fail_reason)
            return result

        try:
            # 1. 关注
            self.ensure_followed(int(author_uid), author_hash)
            self.db.mark_followed(hash_)
            result.steps.append("follow")

            # 2. 回复
            reply_info = self.send_reply(int(flow_id))
            self.db.mark_replied(hash_, reply_info["reply_id"], reply_info["floor"])
            result.steps.append("reply")
            result.reply_id = reply_info["reply_id"]
            result.floor = reply_info["floor"]

            # 3. 点赞
            self.like_flow(int(flow_id))
            self.db.mark_liked(hash_)
            result.steps.append("like")

            self.db.mark_state(hash_, STATE_DONE)
            result.success = True
            log.info("✅ 参与完成：%s（%s）", hash_, " → ".join(result.steps))
            return result

        except CaptchaRequired as exc:
            result.fail_reason = str(exc)
            result.aborted = f"captcha: {exc}"
            self._on_fail(hash_, result.fail_reason, count_retry=False)
            log.error("出现验证码，中止：%s", exc)
            return result

        except RateLimited as exc:
            result.fail_reason = str(exc)
            result.aborted = f"rate_limited: {exc}"
            self._on_fail(hash_, result.fail_reason, count_retry=False)
            log.error("被限流，中止：%s", exc)
            return result

        except SessionExpired as exc:
            result.fail_reason = str(exc)
            result.aborted = f"session_expired: {exc}"
            self._on_fail(hash_, result.fail_reason, count_retry=False)
            log.error("登录失效，中止：%s", exc)
            return result

        except BudgetExhausted as exc:
            result.fail_reason = str(exc)
            result.aborted = f"budget: {exc}"
            # 预算耗尽不是这帖的问题，不记失败、不消耗重试
            log.error("写操作预算耗尽，中止整轮：%s", exc)
            return result

        except (StepError, ApiError) as exc:
            result.fail_reason = str(exc)
            self._on_fail(hash_, result.fail_reason)
            log.warning("参与失败 %s：%s（已完成 %s）", hash_, exc, result.steps or "无")
            return result

        except Exception as exc:  # 防御
            result.fail_reason = f"{type(exc).__name__}: {exc}"
            self._on_fail(hash_, result.fail_reason)
            log.exception("参与 %s 出现未预期错误", hash_)
            return result

    def _on_fail(self, hash_: str, reason: str, count_retry: bool = True) -> None:
        """记录失败；重试用尽则置 give_up。"""
        if count_retry:
            cnt = self.db.bump_retry(hash_, reason)
            if cnt >= self.max_retry:
                self.db.mark_state(hash_, STATE_GIVE_UP, reason)
                log.warning("已连续失败 %d 次，标记放弃：%s", cnt, hash_)
        else:
            # 限流/验证码/登录失效不是帖子本身的问题，重试也不会好，
            # 因此只记录原因，不消耗重试次数
            self.db.mark_failed_no_retry(hash_, reason)
