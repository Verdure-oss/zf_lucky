"""
WebDriver 封装。

复用宿主机既有的 Selenium 容器（selenium/standalone-chrome，原生 aarch64），
不自己拉 Chromium。所有站点交互都通过这个 driver 进行。

设计要点：
  1. session 显式设置较长 timeout，否则默认空闲 5 分钟就被回收；
  2. 所有 execute_script 都走 async 版本处理 Promise，避免拿到未决结果；
  3. 对「元素被遮挡」的点击做防御——固定工具栏是这类站点的通病；
  4. 任何 WebDriver 异常都包装成本项目自己的异常类型，便于上层分类型处理。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# selenium 只在真正驱动浏览器时才需要。
# 采用软导入，使「纯逻辑模块」（调度、DB、语料、面板）在没有 selenium
# 的环境里也能 import 与单测——否则每加一个新模块都要重复踩这个坑。
try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    SELENIUM_AVAILABLE = True
    SELENIUM_IMPORT_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - 仅无依赖环境触发
    webdriver = None  # type: ignore[assignment]
    SELENIUM_AVAILABLE = False
    SELENIUM_IMPORT_ERROR = str(exc)

    class WebDriverException(Exception):  # type: ignore[no-redef]
        """占位：无 selenium 时用于类型与 except 语句。"""

    class NoSuchElementException(Exception):  # type: ignore[no-redef]
        """占位。"""

    class TimeoutException(Exception):  # type: ignore[no-redef]
        """占位。"""

    Options = None  # type: ignore[assignment]
    By = None  # type: ignore[assignment]
    EC = None  # type: ignore[assignment]
    WebDriverWait = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class DriverError(RuntimeError):
    """WebDriver 层的错误基类。"""


def require_selenium() -> None:
    """在真正需要驱动浏览器前调用，给出明确的安装提示。"""
    if not SELENIUM_AVAILABLE:
        raise DriverError(
            "缺少 selenium 依赖"
            f"（{SELENIUM_IMPORT_ERROR}）。请执行 pip install -r requirements.txt"
        )


class SeleniumUnavailable(DriverError):
    """Selenium 服务连不上。"""


class PageLoadError(DriverError):
    """页面加载失败。"""


def wait_selenium_ready(url: str, timeout: int = 120) -> bool:
    """
    轮询等待 Selenium Grid 就绪。

    不能只探测 TCP：容器起来后 Grid 还要几十秒才 ready。
    """
    status_url = url.rstrip("/") + "/wd/hub/status"
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(status_url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("value", {}).get("ready"):
                    log.info("Selenium 已就绪（第 %d 次探测）", attempt)
                    return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt == 1 or attempt % 6 == 0:
                log.info("等待 Selenium 就绪…（%s）", exc)
        time.sleep(5)
    return False


class Driver:
    """对 selenium.webdriver.Remote 的薄封装。"""

    def __init__(self, selenium_url: str, page_load_timeout: int = 60):
        self.selenium_url = selenium_url
        self.page_load_timeout = page_load_timeout
        self._driver: Optional[webdriver.Remote] = None

    # ---------- 生命周期 ----------

    def start(self, session_timeout: int = 1800) -> None:
        """创建浏览器会话。"""
        require_selenium()
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--window-size=1600,1200")
        options.add_argument("--lang=zh-CN")
        # 关掉 「Chrome 正受自动测试软件控制」 提示条，减少显眼的自动化特征
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        try:
            self._driver = webdriver.Remote(
                command_executor=self.selenium_url,
                options=options,
            )
        except WebDriverException as exc:
            raise SeleniumUnavailable(f"无法连接 Selenium（{self.selenium_url}）：{exc}") from exc

        self._driver.set_page_load_timeout(self.page_load_timeout)
        # 显式延长会话空闲超时：默认 300s 对本站的页面加载偏紧
        try:
            self._driver.command_executor._commands["SET_TIMEOUTS"] = (
                "POST",
                "/session/$sessionId/timeouts",
            )
            self._driver.execute(
                "SET_TIMEOUTS",
                {
                    "script": 30000,
                    "pageLoad": self.page_load_timeout * 1000,
                    "implicit": 0,
                    "sessionId": self._driver.session_id,
                },
            )
        except Exception as exc:  # 非致命：部分 Grid 版本不支持
            log.debug("设置 session timeout 失败（不影响运行）：%s", exc)

        log.info("浏览器会话已创建：%s", self._driver.session_id)

    def quit(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
                log.info("浏览器会话已关闭")
            except WebDriverException as exc:
                log.debug("关闭会话时出错（忽略）：%s", exc)
            finally:
                self._driver = None

    def __enter__(self) -> "Driver":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.quit()

    @property
    def raw(self) -> webdriver.Remote:
        if self._driver is None:
            raise DriverError("浏览器会话尚未创建")
        return self._driver

    # ---------- 导航 ----------

    def get(self, url: str, retries: int = 2) -> None:
        """打开页面，失败重试。"""
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                self.raw.get(url)
                self.wait_ready()
                return
            except (TimeoutException, WebDriverException) as exc:
                last = exc
                if attempt < retries:
                    log.warning("打开 %s 失败（第 %d 次），重试：%s", url, attempt + 1, exc)
                    time.sleep(2 + attempt * 2)
        raise PageLoadError(f"无法打开页面 {url}：{last}") from last

    def wait_ready(self, timeout: int = 20) -> None:
        """等待 document.readyState == complete。"""
        WebDriverWait(self.raw, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def current_url(self) -> str:
        return self.raw.current_url

    def title(self) -> str:
        return self.raw.title

    # ---------- 脚本执行 ----------

    def js(self, script: str, *args: Any) -> Any:
        """同步执行 JS。"""
        return self.raw.execute_script(script, *args)

    def js_async(self, script: str, *args: Any, timeout: int = 60) -> Any:
        """
        执行返回 Promise 的 JS 并等待结果。

        必须用 async 版本：普通 execute_script 遇到 Promise 会立刻返回
        undefined 或一个空的 script 对象，拿不到真实数据。
        """
        self.raw.set_script_timeout(timeout)
        return self.raw.execute_async_script(script, *args)

    # ---------- 元素操作 ----------

    def find(self, css: str, timeout: int = 10):
        """等待元素出现并返回。"""
        try:
            return WebDriverWait(self.raw, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, css))
            )
        except TimeoutException as exc:
            raise NoSuchElementException(f"未找到元素：{css}") from exc

    def find_all(self, css: str) -> List[Any]:
        return self.raw.find_elements(By.CSS_SELECTOR, css)

    def exists(self, css: str) -> bool:
        return len(self.raw.find_elements(By.CSS_SELECTOR, css)) > 0

    def safe_click(self, element, label: str = "元素", timeout: int = 8) -> str:
        """
        稳健点击。返回 'native' / 'js' / 'failed'。

        借鉴自既有项目：本站有固定工具栏与浮层，
        元素「存在且可见」不等于「点得到」，会被别的元素接走点击。
        先用 elementFromPoint 确认该坐标点命中的是自己（或自己的父子），
        不行再滚到视口中间重试，最后才用 JS 派发（低可信，需上层校验结果）。
        """
        try:
            self.js(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                element,
            )
        except WebDriverException:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = self.js(
                    """
                    var el = arguments[0];
                    var r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    var x = r.left + r.width / 2, y = r.top + r.height / 2;
                    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
                    var top = document.elementFromPoint(x, y);
                    if (!top) return false;
                    return el === top || el.contains(top) || top.contains(el);
                    """,
                    element,
                )
                if ok:
                    element.click()
                    return "native"
            except WebDriverException as exc:
                log.debug("点击 %s 时探测出错：%s", label, exc)
            time.sleep(0.4)

        try:
            self.js("arguments[0].click();", element)
            log.warning("%s 原生点击被遮挡，改用 JS 兜底（结果需上层校验）", label)
            return "js"
        except WebDriverException as exc:
            log.error("%s 点击失败：%s", label, exc)
            return "failed"

    # ---------- Cookie ----------

    def get_cookies(self) -> List[Dict[str, Any]]:
        return self.raw.get_cookies()

    def clear_cookies(self) -> None:
        self.raw.delete_all_cookies()

    def add_cookies(self, cookies: List[Dict[str, Any]]) -> int:
        """
        批量注入 cookie，返回成功条数。

        单条失败不中断：某些 cookie（如带 __Host- 前缀）对 domain/path 有额外约束，
        失败一条不应影响其余。
        """
        added = 0
        for ck in cookies:
            try:
                entry: Dict[str, Any] = {
                    "name": ck["name"],
                    "value": ck["value"],
                    "path": ck.get("path") or "/",
                }
                if ck.get("domain"):
                    entry["domain"] = ck["domain"]
                if ck.get("secure") is not None:
                    entry["secure"] = bool(ck["secure"])
                if ck.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(ck["httpOnly"])
                self.raw.add_cookie(entry)
                added += 1
            except WebDriverException as exc:
                log.debug("注入 cookie %s 失败：%s", ck.get("name"), exc)
        return added

    def has_cookie(self, name: str) -> bool:
        return any(c["name"] == name for c in self.get_cookies())
