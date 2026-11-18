"""
参与执行器（真实点击版）：关注 → 回复 → 点赞。

与旧版的根本区别：

  旧版：只打开列表页，然后在页面里 fetch() 调接口。
        浏览器只是个"能带 Cookie 的 HTTP 客户端"，一次真实点击都没有，
        所以在 noVNC 里永远看不到任何动作。

  新版：真开帖子页、真点按钮、真往富文本编辑器里打字、真点发送。
        每一步都能在浏览器里看到，行为也更接近真人。

判定成功仍靠回读服务端状态（点击本身不可信：
可能被遮挡、可能点击了但接口没生效），只是"动作"改成真实交互。

为什么顺序是 关注 → 回复 → 点赞：
  点赞放最后，使 has_zan==1 在语义上蕴含"回复也已完成"，
  杜绝"赞了但没回复"的永久漏帖。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..corpus import Corpus
from ..db import STATE_DONE, STATE_FAILED, STATE_GIVE_UP
from .api import Api, ApiError, CaptchaRequired, RateLimited, SessionExpired

log = logging.getLogger(__name__)

# 帖子页关键选择器（实测自真实页面）
SEL_ZAN = ".target_zan-flow"
SEL_EDITOR = ".target_comment-editor"
SEL_QUILL = SEL_EDITOR + " .ql-editor"
SEL_FAST_SUBMIT = "a.plugin-btn.submit"
SEL_AUTHOR_LINK = 'a[href*="/app/user/"]'


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
    """通过真实浏览器交互完成一条抽奖帖的参与。"""

    def __init__(
        self,
        driver,
        api: Api,
        db,
        corpus: Corpus,
        my_uid: int,
        max_retry: int = 3,
        write_budget: int = 30,
        delay_min: int = 2,
        delay_max: int = 5,
    ):
        self.driver = driver
        self.api = api
        self.db = db
        self.corpus = corpus
        self.my_uid = my_uid
        self.max_retry = max_retry
        self.write_budget = write_budget
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.write_used = 0
        self._current_hash: str = ""

    # ---------- 通用 ----------

    def _pause(self, lo: Optional[int] = None, hi: Optional[int] = None) -> None:
        """随机停顿，让节奏像人而不是脚本。"""
        import random

        lo = self.delay_min if lo is None else lo
        hi = self.delay_max if hi is None else hi
        time.sleep(random.uniform(lo, max(lo, hi)))

    def _consume_write(self) -> None:
        if self.write_used >= self.write_budget:
            raise BudgetExhausted(
                f"已达单轮写操作上限 {self.write_budget}，中止整轮"
            )
        self.write_used += 1

    def _open_flow(self, hash_: str) -> None:
        """打开帖子详情页并等评论区渲染完成。"""
        url = f"https://www.zfrontier.com/app/flow/{hash_}"
        self.driver.get(url)
        # 评论区是异步渲染的，等编辑器出现才算就绪
        if not self.driver.wait_selector(SEL_EDITOR, timeout=20):
            raise StepError("帖子页评论区未渲染（编辑器未出现）")
        self._pause(1, 2)

    # ---------- 关注 ----------

    def ensure_followed(self, author_hash: Optional[str], author_uid: int) -> None:
        """
        确保已关注作者——真实点击作者名进主页，再点关注按钮。

        已关注则跳过。关注后回读 /v2/user/detail 的 followed 确认。
        """
        if not author_hash:
            raise StepError("缺少作者 hash，无法执行关注")

        # 先查当前状态，已关注就不必再进主页
        user = (self.api.user_detail(author_hash) or {}).get("user") or {}
        if user.get("followed"):
            log.info("  已关注作者 %s，跳过", author_uid)
            return

        # 真实操作：点作者名进入主页
        try:
            link = self.driver.find(SEL_AUTHOR_LINK, timeout=10)
        except Exception as exc:
            raise StepError(f"未找到作者主页链接：{exc}") from exc

        self._consume_write()
        kind = self.driver.safe_click(link, "作者名")
        if kind == "failed":
            raise StepError("点击作者名失败")

        # 主页是新标签还是本页跳转，两种都处理
        try:
            handles = self.driver.raw.window_handles
            if len(handles) > 1:
                self.driver.raw.switch_to.window(handles[-1])
            self.driver.wait_ready()
        except Exception as exc:
            log.debug("切换作者主页窗口失败：%s", exc)

        # 主页上找关注按钮并点击
        follow_sel = ".bt-green-border.follow"
        try:
            btn = self.driver.find(follow_sel, timeout=15)
        except Exception as exc:
            raise StepError(f"作者主页未找到关注按钮：{exc}") from exc

        text = (btn.text or "").strip()
        log.info("  关注按钮文案：%r", text)
        if "已关注" not in text:
            self.driver.safe_click(btn, "关注按钮")
            log.info("  已点击关注按钮")
            self._pause(2, 3)

        # 回读确认
        for attempt in range(4):
            user = (self.api.user_detail(author_hash) or {}).get("user") or {}
            if user.get("followed"):
                log.info("  关注已确认（第 %d 次回读）", attempt + 1)
                self._close_extra_tabs()
                return
            time.sleep(2 + attempt)

        raise StepError(f"关注 {author_uid} 后回读仍为未关注")

    def _close_extra_tabs(self) -> None:
        """关掉关注时可能打开的新标签，回到帖子页。"""
        try:
            while len(self.driver.raw.window_handles) > 1:
                self.driver.raw.switch_to.window(self.driver.raw.window_handles[-1])
                self.driver.raw.close()
            self.driver.raw.switch_to.window(self.driver.raw.window_handles[0])
        except Exception as exc:
            log.debug("关闭多余标签失败：%s", exc)

    # ---------- 回复 ----------

    def send_reply(self, flow_id: int, hash_: str) -> Dict[str, Any]:
        """
        发回复——真实点编辑器、真实打字、真实点发送。

        优先用站点自带的快捷按钮（抽奖帖有「点击抽奖」，内容为冲冲冲），
        避免往富文本里塞内容时被站点校验拦掉。
        """
        content = self.corpus.pick()

        # 确保在帖子页
        if hash_ not in (self.driver.current_url() or ""):
            self._open_flow(hash_)

        self._consume_write()

        # 方式一：快捷提交按钮（抽奖帖专有，最稳）
        used_fast = False
        try:
            fasts = self.driver.find_all(SEL_FAST_SUBMIT)
            fast = next(
                (f for f in fasts if "抽奖" in (f.text or "") or f.is_displayed()),
                None,
            )
            if fast is not None:
                self.driver.safe_click(fast, "快捷回复按钮")
                used_fast = True
                log.info("  已点击站点快捷回复按钮：%r", (fast.text or "").strip())
        except Exception as exc:
            log.debug("快捷按钮不可用，改用编辑器：%s", exc)

        # 方式二：点编辑器 → 输入 → 点发送
        if not used_fast:
            try:
                editor = self.driver.find(SEL_QUILL, timeout=10)
            except Exception as exc:
                raise StepError(f"未找到回复输入框：{exc}") from exc

            self.driver.safe_click(editor, "回复输入框")
            self._pause(1, 2)

            # Quill 是 contenteditable，send_keys 有时不进内容，先聚焦再输入
            try:
                self.driver.raw.execute_script(
                    "arguments[0].focus();"
                    "var r=document.createRange();r.selectNodeContents(arguments[0]);"
                    "r.collapse(false);var s=window.getSelection();"
                    "s.removeAllRanges();s.addRange(r);",
                    editor,
                )
            except Exception:
                pass
            editor.send_keys(content)
            self._pause(1, 2)
            log.info("  已输入回复内容：%r", content)

            # 找发送按钮
            submit = None
            for sel in ("a.plugin-btn.submit", "button.button-black", ".editor .button"):
                try:
                    cands = self.driver.find_all(sel)
                    submit = next(
                        (c for c in cands if (c.text or "").strip() in ("发送", "回复", "")),
                        cands[0] if cands else None,
                    )
                    if submit is not None:
                        break
                except Exception:
                    continue
            if submit is None:
                raise StepError("未找到发送按钮")
            self.driver.safe_click(submit, "发送按钮")

        log.info("  已发送回复")

        # 回读：抽奖帖回复楼层会被折叠，必须 unfold=1
        for attempt in range(5):
            time.sleep(2 + attempt)
            if self._find_my_reply(flow_id):
                log.info("  回复已确认（第 %d 次回读）", attempt + 1)
                info = self._latest_my_reply(flow_id)
                return {
                    "reply_id": info.get("id"),
                    "floor": info.get("floor"),
                    "content": content,
                }
        raise StepError("回复后回读未找到本人回复")

    def _find_my_reply(self, flow_id: int) -> bool:
        return self._latest_my_reply(flow_id) is not None

    def _latest_my_reply(self, flow_id: int) -> Optional[Dict[str, Any]]:
        """在回复列表里找本人最新一条。"""
        for page in (1, 2):
            try:
                data = self.api.flow_reply_list(flow_id, page=page)
            except ApiError as exc:
                log.debug("查回复列表失败：%s", exc)
                return None
            for item in data.get("list") or []:
                if int(item.get("uid") or 0) == self.my_uid:
                    return item
            if not data.get("has_more"):
                break
        return None

    # ---------- 点赞 ----------

    def like_flow(self, flow_id: int, hash_: str) -> None:
        """
        点赞——真实点那个 👍 图标。

        先确认未赞（已赞再点会变取消赞，反而抹掉标记），
        点完用点赞列表核对（has_zan 有读缓存，实测 16~30 秒才刷新）。
        """
        if self._in_watchlist(hash_):
            log.info("  点赞列表已有，跳过")
            return

        if hash_ not in (self.driver.current_url() or ""):
            self._open_flow(hash_)

        try:
            zan = self.driver.find(SEL_ZAN, timeout=10)
        except Exception as exc:
            raise StepError(f"未找到点赞按钮：{exc}") from exc

        cls = zan.get_attribute("class") or ""
        if "active" in cls:
            log.info("  按钮已处于点赞态，跳过")
            return

        self._consume_write()
        self.driver.safe_click(zan, "点赞按钮")
        log.info("  已点击点赞按钮")

        for attempt in range(5):
            time.sleep(2 + attempt)
            if self._in_watchlist(hash_):
                log.info("  点赞已确认（列表核对，第 %d 次）", attempt + 1)
                return
        # 兜底看 has_zan
        for wait in (3, 6):
            time.sleep(wait)
            flow = (self.api.flow_detail(hash_).get("flow") or {})
            if int(flow.get("has_zan") or 0) == 1:
                log.info("  点赞已确认（has_zan）")
                return
        raise StepError("点赞后无法确认（列表与 has_zan 均未反映）")

    def _in_watchlist(self, hash_: str, pages: int = 3) -> bool:
        for page in range(1, pages + 1):
            try:
                data = self.api.post("/v2/user/zan/list", {"page": page}) or {}
            except ApiError:
                return False
            items = data.get("list") or []
            if not items:
                break
            for it in items:
                h = it.get("hash_id") or (it.get("flow") or {}).get("hash_id")
                if h == hash_:
                    return True
        return False

    # ---------- 主流程 ----------

    def participate(self, flow_row: Dict[str, Any]) -> ParticipateResult:
        """对一条抽奖帖完成关注+回复+点赞（真实点击）。"""
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

        try:
            # 打开帖子页（后续关注/回复/点赞都在这个页面上操作）
            self._open_flow(hash_)
            result.steps.append("open")

            done = self.db.get_participation(hash_) or {}

            # 1. 关注（已完成则跳过，绝不重复）
            if not done.get("followed_at"):
                if author_uid:
                    self.ensure_followed(author_hash, int(author_uid))
                self.db.mark_followed(hash_)
                result.steps.append("follow")
                # 关注会跳走，回来重新开帖
                self._open_flow(hash_)
            else:
                log.info("  关注已完成过，跳过")

            # 2. 回复（已完成则绝不重复——回复不可撤销）
            if not done.get("replied_at"):
                info = self.send_reply(int(flow_id), hash_)
                self.db.mark_replied(hash_, info["reply_id"], info["floor"])
                result.steps.append("reply")
                result.reply_id = info["reply_id"]
                result.floor = info["floor"]
            else:
                log.info("  回复已完成过（reply_id=%s），跳过", done.get("reply_id"))
                result.reply_id = done.get("reply_id")
                result.floor = done.get("reply_floor")

            # 3. 点赞
            self.like_flow(int(flow_id), hash_)
            self.db.mark_liked(hash_)
            result.steps.append("like")

            self.db.mark_state(hash_, STATE_DONE)
            result.success = True
            log.info("✅ 参与完成：%s（%s）", hash_, " → ".join(result.steps))
            return result

        except BudgetExhausted as exc:
            result.fail_reason = str(exc)
            result.aborted = f"budget: {exc}"
            log.error("写操作预算耗尽，中止整轮：%s", exc)
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

        except (StepError, ApiError) as exc:
            result.fail_reason = str(exc)
            self._on_fail(hash_, result.fail_reason)
            log.warning("参与失败 %s：%s（已完成 %s）", hash_, exc, result.steps or "无")
            return result

        except Exception as exc:
            result.fail_reason = f"{type(exc).__name__}: {exc}"
            self._on_fail(hash_, result.fail_reason)
            log.exception("参与 %s 出现未预期错误", hash_)
            return result

    def _on_fail(self, hash_: str, reason: str, count_retry: bool = True) -> None:
        if count_retry:
            cnt = self.db.bump_retry(hash_, reason)
            if cnt >= self.max_retry:
                self.db.mark_state(hash_, STATE_GIVE_UP, reason)
                log.warning("已连续失败 %d 次，标记放弃：%s", cnt, hash_)
        else:
            self.db.mark_failed_no_retry(hash_, reason)

    @property
    def budget_left(self) -> int:
        return max(0, self.write_budget - self.write_used)
