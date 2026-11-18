"""
登录会话器：把「人工在 noVNC 里登录」转成「程序可用的 Cookie」。

为什么需要它：
  站点登录页有滑块验证码，程序不能也不应破解。正确做法是让人在真浏览器里
  完成登录，然后由程序把会话 Cookie 取出来，后续自动化复用这份 Cookie。

生命周期：
  1. 打开一个**独立的、长期存活**的浏览器会话（不随主流程退出）
  2. 导航到登录页，提示用户到 noVNC 里操作
  3. 用户登录完成后，程序轮询检测「是否已登录」
  4. 检测到登录 → 读出全部 Cookie → 写入 .env → 可选重启
  5. 会话可由用户主动释放

与主流程的隔离：
  主流程每轮用完就 quit()，而这个会话必须常驻，因此用独立进程/独立 Driver
  实例，不共用主流程的 driver。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

STATE_FILE = "/tmp/zf_login_session.json"
LOGIN_URL = "https://www.zfrontier.com/app/login"
ENV_PATH = "/app/.env"

# 需要写回 .env 的敏感项，日志中必须脱敏
SENSITIVE = ("value",)


class LoginSession:
    """
    常驻的登录用浏览器会话。

    单例式使用：面板通过 get() 拿全局实例。
    """

    def __init__(self, selenium_url: str):
        self.selenium_url = selenium_url
        self._driver = None
        self._lock = threading.Lock()
        self._started_at: Optional[float] = None
        self.message = ""
        self.last_error = ""

    # ---------- 状态 ----------

    @property
    def running(self) -> bool:
        return self._driver is not None

    def status(self) -> Dict[str, Any]:
        alive = False
        if self._driver is not None:
            try:
                # 轻量探活：读取当前 URL
                self._driver.current_url()
                alive = True
            except Exception:
                alive = False
        return {
            "running": self.running and alive,
            "started_at": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._started_at))
                if self._started_at
                else None
            ),
            "message": self.message,
            "error": self.last_error,
        }

    # ---------- 启停 ----------

    def start(self) -> Dict[str, Any]:
        """启动登录会话并打开登录页。已存在则直接返回。"""
        with self._lock:
            if self._driver is not None:
                st = self.status()
                if st["running"]:
                    return {"ok": True, "msg": "登录会话已在运行", **st}
                # 死会话，先清掉
                self._quiet_quit()

            from ..zf.driver import Driver

            try:
                drv = Driver(self.selenium_url)
                drv.start()
                drv.get(LOGIN_URL)
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("启动登录会话失败")
                return {"ok": False, "msg": f"启动浏览器失败：{exc}"}

            self._driver = drv
            self._started_at = time.time()
            self.message = "请在 noVNC 中完成登录"
            self.last_error = ""
            log.info("登录会话已启动，已打开登录页")
            return {"ok": True, "msg": "已打开登录页，请到 noVNC 中登录", **self.status()}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._quiet_quit()
        return {"ok": True, "msg": "登录会话已关闭"}

    def _quiet_quit(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        self._started_at = None

    # ---------- 检测登录 ----------

    def check_logged_in(self) -> Dict[str, Any]:
        """
        检查当前浏览器是否已登录。

        **不能用 /v2/user/card**：实测该接口在本站持续返回 HTTP 500
        （「操作太频繁了」），登录与否都拿不到数据。按它判定会导致
        用户明明登录成功却一直显示「尚未登录」——这正是最初的 bug。

        优先从页面 Vuex 读身份（零请求、不受限流影响），
        读不到再用 /v2/publish/rules 判断会话是否有效。
        """
        if self._driver is None:
            return {"ok": False, "logged_in": False, "msg": "登录会话未启动"}

        # ---- 1) Vuex（最可靠）----
        try:
            raw = self._driver.js(
                """
                var vm = document.querySelector('#app') && document.querySelector('#app').__vue__;
                if (!vm || !vm.$store || !vm.$store.state) return null;
                var st = vm.$store.state.user || {};
                var info = st.userInfo || st.info || null;
                if (!info) return null;
                return {
                    userId: info.userId || info.user_id || info.id || null,
                    nickname: info.nickname || '',
                    hash: info.hashId || info.hash_id || ''
                };
                """
            )
            if isinstance(raw, dict) and raw.get("userId"):
                return {
                    "ok": True,
                    "logged_in": True,
                    "uid": int(raw["userId"]),
                    "nickname": raw.get("nickname") or "",
                    "msg": f"已登录：{raw.get('nickname') or raw['userId']}",
                }
        except Exception as exc:
            log.debug("Vuex 读取失败，走接口兜底：%s", exc)

        # ---- 2) 接口兜底：判断会话有效 ----
        try:
            result = self._driver.js_async(
                """
                var done = arguments[arguments.length - 1];
                var tok = window.csrf_token || '';
                function probe(url) {
                    return fetch(url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'X-CSRF-TOKEN': tok
                        },
                        body: ''
                    }).then(function (r) {
                        return r.text().then(function (x) {
                            var j = null;
                            try { j = JSON.parse(x); } catch (e) {}
                            return { status: r.status, ok: j ? j.ok : null };
                        });
                    }).catch(function (e) { return { status: 0, error: String(e) }; });
                }
                Promise.all([probe('/v2/publish/rules'), probe('/v2/user/getMobile')])
                  .then(function (r) { done({ rules: r[0], mobile: r[1] }); })
                  .catch(function (e) { done({ error: String(e) }); });
                """,
                timeout=45,
            )
        except Exception as exc:
            return {"ok": False, "logged_in": False, "msg": f"检测失败：{exc}"}

        if not isinstance(result, dict) or result.get("error"):
            return {"ok": False, "logged_in": False, "msg": "检测请求失败"}

        rules = result.get("rules") or {}
        mobile = result.get("mobile") or {}

        if rules.get("ok") == 0 or mobile.get("ok") == 0:
            return {
                "ok": True,
                "logged_in": True,
                "nickname": "",
                "msg": "已登录（会话有效；页面身份待刷新后读取）",
            }

        return {
            "ok": True,
            "logged_in": False,
            "msg": "尚未登录",
            "detail": f"rules.ok={rules.get('ok')} mobile.ok={mobile.get('ok')}",
        }

    # ---------- 导出 Cookie ----------

    def export_cookie_string(self) -> str:
        """把浏览器全部 cookie 拼成 'a=1; b=2' 形式。"""
        if self._driver is None:
            raise RuntimeError("登录会话未启动")
        cookies = self._driver.get_cookies()
        if not cookies:
            raise RuntimeError("浏览器中没有 cookie")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
