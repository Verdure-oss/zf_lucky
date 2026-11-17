"""
同源 fetch 封装。

为什么不直接用 requests：
  站点的 X-CSRF-TOKEN 渲染在页面里（window.csrf_token），与服务端会话绑定。
  外部 HTTP 客户端要自己维护 token + Cookie + 过期重取，容易出错。
  在页面上下文里 fetch 时，Cookie 与 CSRF 自动带上，和真人浏览器发出的
  请求完全一致，也没有额外指纹差异。

所有站点接口调用都必须经过这里。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

from .driver import Driver

log = logging.getLogger(__name__)

# 站点在「操作太频繁」时返回的业务码
CODE_RATE_LIMITED = 20001
# 未登录/会话失效
CODE_UNAUTHORIZED = 404
# 需要跳转（通常是登录）
CODE_REDIRECT = 302

_FETCH_JS = """
var args = arguments[0];
var done = arguments[arguments.length - 1];
var headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
    'X-CSRF-TOKEN': window.csrf_token || '',
    'X-CLIENT-LOCALE': 'zh-cn',
    'X-Requested-With': 'XMLHttpRequest'
};
fetch(args.url, {
    method: 'POST',
    credentials: 'include',
    headers: headers,
    body: args.body
})
.then(function (r) { return r.text().then(function (t) { return { status: r.status, text: t }; }); })
.then(function (r) {
    var parsed = null, parseErr = null;
    try { parsed = JSON.parse(r.text); } catch (e) { parseErr = String(e); }
    done({
        httpStatus: r.status,
        json: parsed,
        parseError: parseErr,
        isHtml: r.text.trim().charAt(0) === '<',
        head: r.text.slice(0, 300)
    });
})
.catch(function (e) { done({ error: String(e) }); });
"""


class ApiError(RuntimeError):
    """接口调用失败。"""


class RateLimited(ApiError):
    """站点限流（20001）。"""


class SessionExpired(ApiError):
    """登录态失效。"""


class CaptchaRequired(ApiError):
    """页面出现验证码弹层，必须人工处理。"""


def encode_form(data: Optional[Dict[str, Any]]) -> str:
    """
    把 dict 编码成 application/x-www-form-urlencoded。

    站点允许重复键（tagIds[] 之类），因此支持值为 list 的情况。
    """
    if not data:
        return ""
    parts = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                parts.append(f"{_quote(key)}={_quote(item)}")
        elif isinstance(value, bool):
            parts.append(f"{_quote(key)}={'1' if value else '0'}")
        else:
            parts.append(f"{_quote(key)}={_quote(value)}")
    return "&".join(parts)


def _quote(value: Any) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


class Api:
    """站点接口客户端（始终在页面上下文内发起请求）。"""

    def __init__(
        self,
        driver: Driver,
        delay_min: int = 2,
        delay_max: int = 5,
        captcha_check: bool = True,
    ):
        self.driver = driver
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.captcha_check = captcha_check
        self.request_count = 0

    # ---------- 节奏控制 ----------

    def _sleep(self) -> None:
        """请求间随机延迟，削弱固定节律特征。"""
        if self.delay_max > 0:
            time.sleep(random.uniform(self.delay_min, self.delay_max))

    # ---------- 验证码检测 ----------

    def has_captcha(self) -> bool:
        """检测页面是否弹出了安全验证浮层。"""
        try:
            return bool(
                self.driver.js(
                    """
                    return !!(document.querySelector('.capthcah-overlay .slidercaptcha')
                           || document.querySelector('.capthcah-overlay')
                           || document.querySelector('.slidercaptcha'));
                    """
                )
            )
        except Exception:
            return False

    def ensure_no_captcha(self) -> None:
        if self.captcha_check and self.has_captcha():
            raise CaptchaRequired(
                "页面出现滑块安全验证，已暂停。"
                "请通过 noVNC（127.0.0.1:7900）手动完成验证后重试。"
            )

    # ---------- 请求 ----------

    def post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay: bool = True,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """
        发起同源 POST 请求，返回响应的 data 部分。

        :raises RateLimited: 站点返回 20001
        :raises SessionExpired: 会话失效
        :raises CaptchaRequired: 出现验证码
        :raises ApiError: 其他失败
        """
        if delay:
            self._sleep()

        self.ensure_no_captcha()

        payload = {"url": path, "body": encode_form(data)}
        result = self.driver.js_async(_FETCH_JS, payload, timeout=timeout)
        self.request_count += 1

        if not isinstance(result, dict):
            raise ApiError(f"{path} 返回结构异常：{result!r}")
        if result.get("error"):
            raise ApiError(f"{path} 网络错误：{result['error']}")

        body = result.get("json")
        if body is None:
            if result.get("isHtml"):
                # 整页 HTML 通常是登录跳转
                raise SessionExpired(f"{path} 返回 HTML，登录态可能已失效")
            raise ApiError(
                f"{path} 返回非 JSON：{result.get('head', '')[:120]}"
            )

        code = body.get("ok")
        msg = body.get("msg") or ""

        if code == CODE_RATE_LIMITED:
            # 限流后页面可能已弹验证码，立刻检查
            self.ensure_no_captcha()
            raise RateLimited(f"{path} 被限流：{msg}")

        if code == CODE_UNAUTHORIZED:
            raise SessionExpired(f"{path} 未登录：{msg}")

        if code == CODE_REDIRECT:
            raise SessionExpired(f"{path} 需要重新登录：{msg}")

        if code != 0:
            raise ApiError(f"{path} 失败（ok={code}）：{msg}")

        return body.get("data")

    # ---------- 业务接口 ----------

    def flow_detail(self, hash_: str) -> Dict[str, Any]:
        """帖子详情。返回 {'flow':..., 'zanUsers':..., ...}。"""
        data = self.post("/v2/flow/detail", {"id": hash_})
        return data or {}

    def flow_reply_list(
        self, flow_id: int, page: int = 1, sort_by: str = "ctime_desc", unfold: int = 1
    ) -> Dict[str, Any]:
        """
        帖子回复列表。

        unfold=1 是关键：抽奖帖默认隐藏抽奖楼层，
        不展开就查不到自己那条回复，会误判「没回复成功」。
        """
        data = self.post(
            "/api/circle/flowReplyList",
            {
                "id": flow_id,
                "type": "flow",
                "page": page,
                "sortBy": sort_by,
                "unfold": unfold,
            },
        )
        return data or {}

    def user_detail(self, user_hash: str) -> Dict[str, Any]:
        """用户详情，含 followed（我是否已关注他）。"""
        data = self.post("/v2/user/detail", {"id": user_hash})
        return data or {}

    def follow(self, uid: int) -> Dict[str, Any]:
        data = self.post("/api/follow", {"uid": uid, "action": "follow"})
        return data or {}

    def unfollow(self, uid: int) -> Dict[str, Any]:
        data = self.post("/api/follow", {"uid": uid, "action": "unfollow"})
        return data or {}

    def reply(self, flow_id: int, content: str, reply_id: int = 0) -> Dict[str, Any]:
        """发回复。返回 {'reply': {...}}。"""
        data = self.post(
            "/v2/flow/reply",
            {
                "id": flow_id,
                "type": "flow",
                "reply_id": reply_id,
                "content": content,
            },
            # 写操作给更长的超时，也给更长的前置延迟
            timeout=90,
        )
        return data or {}

    def zan_flow(self, flow_id: int) -> Dict[str, Any]:
        data = self.post("/api/circle/zanFlow", {"id": flow_id, "action": "add"})
        return data or {}

    def unzan_flow(self, flow_id: int) -> Dict[str, Any]:
        data = self.post("/api/circle/zanFlow", {"id": flow_id, "action": "cancel"})
        return data or {}
