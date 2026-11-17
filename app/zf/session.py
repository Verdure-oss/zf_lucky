"""
登录会话管理。

站点登录态完全由 Cookie 承载。本项目不保存账号密码、不自动登录
（登录页有滑块验证码），而是由用户从浏览器导出 Cookie 填进 .env。

会话有效性判定不看「有没有某个 cookie」，而看**站点是否认这个会话**：
访问需要登录才返回数据的接口（/v2/user/card），能拿到用户信息才算有效。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .driver import Driver, PageLoadError

log = logging.getLogger(__name__)

HOME = "https://www.zfrontier.com"


class SessionError(RuntimeError):
    """登录态相关错误。"""


def parse_cookie_string(raw: str) -> List[Dict[str, Any]]:
    """
    把浏览器复制的 "a=1; b=2" 解析成 WebDriver 可用的 cookie 列表。

    容错：值里可能含 '='（如 base64），所以只按第一个 '=' 切分。
    """
    cookies: List[Dict[str, Any]] = []
    for item in raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value})
    return cookies


class Session:
    """封装一次运行期间的登录态。"""

    def __init__(self, driver: Driver, cookie_raw: str):
        self.driver = driver
        self.cookie_raw = cookie_raw
        self.user: Optional[Dict[str, Any]] = None

    # ---------- 建立 ----------

    def establish(self) -> Dict[str, Any]:
        """
        注入 Cookie 并验证登录态，返回当前用户信息。

        :raises SessionError: Cookie 无效或已过期
        """
        cookies = parse_cookie_string(self.cookie_raw)
        if not cookies:
            raise SessionError(
                "ZF_COOKIE 无法解析出任何键值对，请确认格式形如 "
                "'name1=value1; name2=value2'"
            )

        # 必须先访问一次目标域，否则 add_cookie 会因 domain 不匹配被拒
        self.driver.get(HOME)

        self.driver.clear_cookies()
        added = self.driver.add_cookies(cookies)
        log.info("已注入 %d/%d 个 Cookie", added, len(cookies))

        if added == 0:
            raise SessionError("所有 Cookie 都注入失败，请检查 Cookie 是否完整")

        # 注入后必须刷新，让页面携带新会话重新请求
        self.driver.get(HOME)

        user = self.fetch_user()
        if user is None:
            raise SessionError(
                "Cookie 已失效或无效：/v2/user/card 未返回用户信息。"
                "请重新从浏览器导出 Cookie 填入 .env"
            )

        self.user = user
        log.info(
            "登录成功：%s（uid=%s）",
            user.get("nickname") or "?",
            user.get("userId") or user.get("user_id") or "?",
        )
        return user

    # ---------- 查询 ----------

    def fetch_user(self) -> Optional[Dict[str, Any]]:
        """
        拉取当前登录用户。

        /v2/user/card 在未登录时返回 HTML（整页）而非 JSON，
        因此这里既处理 JSON 也处理 HTML，避免把「未登录」误判成「接口错误」。
        """
        result = self.driver.js_async(
            """
            var done = arguments[arguments.length - 1];
            fetch('/v2/user/card', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRF-TOKEN': window.csrf_token || ''
                },
                body: ''
            })
            .then(function (r) { return r.text(); })
            .then(function (t) {
                var j = null;
                try { j = JSON.parse(t); } catch (e) {}
                done({ json: j, isHtml: t.trim().charAt(0) === '<' });
            })
            .catch(function (e) { done({ error: String(e) }); });
            """
        )

        if not isinstance(result, dict) or result.get("error"):
            log.debug("拉取用户信息失败：%s", result)
            return None
        if result.get("isHtml"):
            log.debug("用户接口返回 HTML，说明未登录")
            return None

        payload = result.get("json") or {}
        if payload.get("ok") != 0:
            log.debug("用户接口返回非成功：ok=%s msg=%s", payload.get("ok"), payload.get("msg"))
            return None

        data = payload.get("data") or {}
        # 不同版本字段名不一致，尽量兼容
        user = data.get("user") or data.get("userInfo") or data
        if not isinstance(user, dict) or not user:
            return None

        uid = user.get("userId") or user.get("user_id") or user.get("id")
        if not uid:
            return None

        return {
            "userId": int(uid),
            "hash": user.get("hash_id") or user.get("hashId") or "",
            "nickname": user.get("nickname") or "",
            "raw": user,
        }

    def check_publish_rule(self) -> Optional[Dict[str, Any]]:
        """
        检查是否允许回复（站点要求绑定手机号）。

        返回 publish/rules 的 data；拿不到返回 None。
        """
        result = self.driver.js_async(
            """
            var done = arguments[arguments.length - 1];
            fetch('/v2/publish/rules', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRF-TOKEN': window.csrf_token || ''
                },
                body: ''
            })
            .then(function (r) { return r.text(); })
            .then(function (t) {
                var j = null;
                try { j = JSON.parse(t); } catch (e) {}
                done({ json: j, head: t.slice(0, 60) });
            })
            .catch(function (e) { done({ error: String(e) }); });
            """
        )
        if not isinstance(result, dict) or result.get("error"):
            return None
        payload = result.get("json")
        if not payload or payload.get("ok") != 0:
            log.debug("publish/rules 返回异常：%s", result.get("head"))
            return None
        return payload.get("data") or {}

    @property
    def uid(self) -> Optional[int]:
        return self.user.get("userId") if self.user else None
