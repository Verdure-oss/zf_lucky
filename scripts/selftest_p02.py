"""
P0-2 联网自检：WebDriver + 会话 + 同源 fetch。

与 P0-1 不同，这个必须真的连 Selenium 和 zFrontier 才能验证。
默认只做**只读**操作，不发任何写请求。

用法：
    python3 scripts/selftest_p02.py                    # 只读自检
    ZF_COOKIE='...' python3 scripts/selftest_p02.py    # 带登录态自检
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as cfg_mod  # noqa: E402
from app.zf.api import Api, ApiError, RateLimited, SessionExpired, encode_form  # noqa: E402
from app.zf.driver import Driver, DriverError, wait_selenium_ready  # noqa: E402
from app.zf.session import Session, SessionError, parse_cookie_string  # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def main() -> int:
    cfg = cfg_mod.load()

    print("\n[1] URL 编码（离线）")
    check("空数据", encode_form(None) == "")
    check("基本编码", encode_form({"id": "abc"}) == "id=abc")
    check("中文编码", "%E4%B8%AD" in encode_form({"c": "中"}))
    check("布尔转 0/1", encode_form({"b": True}) == "b=1")
    check("列表展开", encode_form({"t": [1, 2]}) == "t=1&t=2")
    check("None 被跳过", encode_form({"a": 1, "b": None}) == "a=1")

    print("\n[2] Cookie 解析（离线）")
    cks = parse_cookie_string("a=1; b=2;c=has=eq")
    check("解析 3 项", len(cks) == 3, str([c["name"] for c in cks]))
    check("值内等号保留", cks[2]["value"] == "has=eq")
    check("空串得空列表", parse_cookie_string("") == [])

    print("\n[3] Selenium 连通性")
    if not wait_selenium_ready(cfg.selenium_url, timeout=20):
        print(f"  ❌ Selenium 不可达：{cfg.selenium_url}")
        print("     确认容器在跑且与本源在同一网络。")
        return 1
    check("Grid ready", True, cfg.selenium_url)

    driver = Driver(cfg.selenium_url)
    try:
        driver.start()

        print("\n[4] 页面加载")
        driver.get("https://www.zfrontier.com/app/flow/ZwPnZ6lzj5xy")
        title = driver.title()
        check("标题包含站点名", "zFrontier" in title, title[:40])
        check("csrf_token 可读", bool(driver.js("return !!window.csrf_token")))
        check("Vue 已挂载", bool(driver.js("return !!document.querySelector('#app').__vue__")))

        print("\n[5] 同源 fetch（只读接口）")
        api = Api(driver, delay_min=1, delay_max=2)
        check("无验证码浮层", not api.has_captcha())

        detail = api.flow_detail("ZwPnZ6lzj5xy")
        flow = detail.get("flow") or {}
        check("详情返回 flow", bool(flow), f"id={flow.get('id')}")
        check("有 lottery 字段", "lottery" in flow)
        if flow.get("lottery"):
            lot = flow["lottery"]
            check("lottery.status 可读", lot.get("status") is not None, f"status={lot.get('status')}")
            check("lottery_at 可读", bool(lot.get("lottery_at")), str(lot.get("lottery_at")))
            check("filters 可读", "filters" in lot, str(lot.get("filters")))
        check("has_zan 字段存在", "has_zan" in flow, f"has_zan={flow.get('has_zan')}")
        check("stat.cnt_reply 可读", bool((flow.get("stat") or {}).get("cnt_reply")))

        if flow.get("id"):
            rl = api.flow_reply_list(flow["id"], page=1)
            lst = rl.get("list") or []
            check("回复列表可读", isinstance(lst, list), f"{len(lst)} 条")
            check("has_more 字段存在", "has_more" in rl)
            if lst:
                first = lst[0]
                check("回复含 uid", "uid" in first, f"uid={first.get('uid')}")
                check("回复含 floor", "floor" in first, f"floor={first.get('floor')}")

        if flow.get("user", {}).get("hash_id"):
            ud = api.user_detail(flow["user"]["hash_id"])
            u = ud.get("user") or {}
            check("用户详情可读", bool(u), u.get("nickname", ""))
            check("followed 字段存在", "followed" in u, f"followed={u.get('followed')}")

        print(f"\n[6] 本次请求数：{api.request_count}")

        print("\n[7] 登录态（可选）")
        if cfg.cookie:
            try:
                sess = Session(driver, cfg.cookie)
                user = sess.establish()
                check("登录成功", bool(user), f"uid={user.get('userId')}")
                rules = sess.check_publish_rule()
                if rules is not None:
                    ip = rules.get("identity_pass")
                    check("identity_pass 可读", ip is not None, f"identity_pass={ip}")
                    if ip == 0:
                        print("     ⚠️ identity_pass=0：账号未绑手机号，无法回复。")
                else:
                    print("     ⚠️ publish/rules 未返回数据（可能需要更完整的登录态）")
            except SessionError as exc:
                check("登录成功", False, str(exc))
        else:
            print("  ⏭  ZF_COOKIE 未配置，跳过")

        print("\n[8] 异常类型可用性（离线）")
        for cls in (ApiError, RateLimited, SessionExpired, DriverError):
            check(f"{cls.__name__} 可导入", True)

    finally:
        driver.quit()

    print("\n" + "=" * 52)
    print("P0-2 全部通过" if FAILED == 0 else f"P0-2 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
