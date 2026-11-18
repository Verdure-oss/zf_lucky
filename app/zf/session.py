"""
登录会话管理。

站点登录态完全由 Cookie 承载。本项目不保存账号密码、不自动登录
（登录页有滑块验证码），而是由用户从浏览器导出 Cookie 填进 .env。

会话有效性判定不看「有没有某个 cookie」，而看**站点是否认这个会话**：
访问需要登录才返回数据的接口（/v2/user/card），能拿到用户信息才算有效。
"""

from __future__ import annotations

import logging
import time
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

        # 关键：确认会话真正装载后再返回。
        # 实测踩坑——注入 cookie 后立刻跳 tag 页，首屏的列表请求会带着
        # 尚未就绪的会话发出，表现是「已登录但列表 0 条」（匿名反而 10 条）。
        # 先在这里等 Vuex 装载用户信息，后续页面就正常。
        self._await_session_ready()

        user = self.fetch_user()
        if user is None:
            raise SessionError(
                "Cookie 已失效或无效：会话探活未通过。"
                "请重新从浏览器导出 Cookie 填入 .env"
            )

        self.user = user
        if user.get("degraded"):
            log.warning("已登录但未取得 uid，回复回读校验将降级")
        else:
            log.info(
                "登录成功：%s（uid=%s）",
                user.get("nickname") or "?",
                user.get("userId") or "?",
            )
        return user

    def _await_session_ready(self, timeout: int = 25) -> None:
        """
        等页面把登录态装载进 Vuex。

        登录后首屏要先拉用户信息才会渲染列表；不等就跳页，
        列表接口会以游客身份请求，结果就是「登录了但一条也没有」。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ready = self.driver.js(
                    """
                    var vm = document.querySelector('#app') && document.querySelector('#app').__vue__;
                    if (!vm || !vm.$store || !vm.$store.state) return false;
                    var st = vm.$store.state.user || {};
                    var info = st.userInfo || st.info || null;
                    return !!(info && (info.userId || info.user_id || info.id));
                    """
                )
                if ready:
                    log.debug("会话已装载进 Vuex")
                    return
            except Exception:
                pass
            time.sleep(0.8)
        log.debug("等待会话装载超时，继续（后续步骤会再校验）")

    # ---------- 查询 ----------

    def fetch_user(self) -> Optional[Dict[str, Any]]:
        """
        获取当前登录用户身份。

        实测坑：/v2/user/card 在本站**持续返回 HTTP 500**（「操作太频繁了」），
        无论登录与否都拿不到数据，因此绝不能作为登录判据——那会把有效
        Cookie 误判为失效，也会让 uid 拿不到（进而回复回读校验失效）。

        正确来源按可靠性排序：
          1. 页面 Vuex store 的 state.user.userInfo —— 已登录时一定有，
             不消耗接口、不受限流影响（首选）
          2. /v2/user/getMobile 的 ok=0 —— 能证明会话有效，但没有 uid
          3. /v2/publish/rules 的 ok=0 —— 证明会话有效且已绑手机

        判据原则：**会话有效性**与**身份信息**分开判断。
        会话有效但拿不到 uid 时，返回 degraded 标记而不是判定「Cookie 失效」。
        """
        # ---- 1) 优先从页面 Vuex 读取身份（零请求、最可靠）----
        try:
            raw = self.driver.js(
                """
                var vm = document.querySelector('#app') && document.querySelector('#app').__vue__;
                if (!vm || !vm.$store || !vm.$store.state) return null;
                var st = vm.$store.state.user || {};
                var info = st.userInfo || st.info || null;
                if (!info) return null;
                return {
                    userId: info.userId || info.user_id || info.id || null,
                    nickname: info.nickname || '',
                    hash: info.hashId || info.hash_id || '',
                    avatar: info.avatarPath || ''
                };
                """
            )
            if isinstance(raw, dict) and raw.get("userId"):
                return {
                    "userId": int(raw["userId"]),
                    "hash": raw.get("hash") or "",
                    "nickname": raw.get("nickname") or "",
                    "raw": raw,
                    "source": "vuex",
                }
        except Exception as exc:
            log.debug("从 Vuex 读取用户失败（尝试接口兜底）：%s", exc)

        # ---- 2) 接口兜底：判断会话是否有效 ----
        result = self.driver.js_async(
            """
            var done = arguments[arguments.length - 1];
            var tok = window.csrf_token || '';

            function probe(url, body) {
                return fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRF-TOKEN': tok
                    },
                    body: body || ''
                }).then(function (r) {
                    return r.text().then(function (x) {
                        var j = null;
                        try { j = JSON.parse(x); } catch (e) {}
                        return {
                            status: r.status,
                            ok: j ? j.ok : null,
                            data: j ? j.data : null,
                            isHtml: x.trim().charAt(0) === '<'
                        };
                    });
                }).catch(function (e) { return { status: 0, error: String(e) }; });
            }

            Promise.all([
                probe('/v2/publish/rules'),
                probe('/v2/user/getMobile')
            ]).then(function (r) {
                done({ rules: r[0], mobile: r[1] });
            }).catch(function (e) { done({ error: String(e) }); });
            """,
            timeout=60,
        )

        if not isinstance(result, dict) or result.get("error"):
            log.debug("会话探活失败：%s", result)
            return None

        rules = result.get("rules") or {}
        mobile = result.get("mobile") or {}

        session_ok = rules.get("ok") == 0 or mobile.get("ok") == 0
        if not session_ok:
            log.debug(
                "会话无效（rules.ok=%s mobile.ok=%s）",
                rules.get("ok"),
                mobile.get("ok"),
            )
            return None

        # 会话有效但没有 uid：标记 degraded，上层据此降低校验强度
        log.warning(
            "会话有效但未能取得 uid（Vuex 未就绪，规则接口 ok=%s）。"
            "回复回读校验将无法按 uid 匹配。",
            rules.get("ok"),
        )
        return {
            "userId": 0,
            "hash": "",
            "nickname": "",
            "raw": {},
            "degraded": True,
            "source": "api",
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
