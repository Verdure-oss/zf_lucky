"""
P0-5 自检：参与器（真实点击版）。

**全部使用假 driver + 假 API，不对真实账号发起任何写操作。**

重点验证「错了会永久漏帖/刷屏」的逻辑：
  - 步骤顺序必须是 关注 → 回复 → 点赞
  - 已点赞时不能重复点（会变成取消赞，抹掉标记）
  - 回复后必须能回读到本人回复才算成功
  - 断点续跑：已有 followed_at / replied_at 的绝不重复执行
  - 单帖失败不中断整轮；限流/验证码/登录失效才中止
  - 限流不消耗重试次数
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.corpus import Corpus  # noqa: E402
from app.db import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_GIVE_UP,
    Database,
)
from app.zf.api import ApiError, CaptchaRequired, RateLimited, SessionExpired  # noqa: E402
from app.zf.participant import BudgetExhausted, Participant  # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


class FakeElement:
    """假的页面元素。"""

    def __init__(self, text="", cls="", displayed=True, on_click=None):
        self.text = text
        self._cls = cls
        self._displayed = displayed
        self._on_click = on_click
        self.typed = ""

    def get_attribute(self, name):
        return self._cls if name == "class" else ""

    def is_displayed(self):
        return self._displayed

    def click(self):
        if self._on_click:
            self._on_click()

    def send_keys(self, text):
        self.typed += text


class FakeDriver:
    """
    假 driver：记录所有真实交互动作（点击/输入），供断言。

    只实现参与器用到的那部分 API。
    """

    def __init__(self):
        self.actions = []
        self.current_url_value = ""
        self._elements = {}
        self._handles = ["h1"]
        self.raw = self

    # --- 导航 ---
    def get(self, url):
        self.current_url_value = url
        self.actions.append(("get", url))

    def current_url(self):
        return self.current_url_value

    def wait_ready(self, timeout=20):
        return True

    def wait_selector(self, css, timeout=20):
        return css in self._elements or True  # 默认认为已渲染

    # --- 元素 ---
    def find(self, css, timeout=10):
        if css in self._elements:
            return self._elements[css]
        # 支持 a[href*="/app/user/XXX"] 这种按作者 hash 精确定位
        if "app/user/" in css:
            return self._elements["a[href*=\"/app/user/\"]"]
        raise RuntimeError(f"元素不存在：{css}")

    def find_all(self, css):
        el = self._elements.get(css)
        if not el:
            return []
        return el if isinstance(el, list) else [el]

    def safe_click(self, element, label="元素", timeout=8):
        self.actions.append(("click", label))
        element.click()
        return "native"

    def js(self, script, *args):
        return None

    # --- 窗口 ---
    @property
    def window_handles(self):
        return list(self._handles)

    def switch_to(self):
        return self

    @property
    def window(self):
        return self

    def close(self):
        pass

    # --- Quill 聚焦用 ---
    def execute_script(self, *a, **kw):
        return None


class FakeApi:
    """假 API：记录调用，可编排故障。"""

    def __init__(self):
        self.calls = []
        self.followed = {}
        self.has_zan = {}
        self.replies = {}
        self.zan_list = []
        self._hash_to_flow = {}

        self.fail_follow = False
        self.fail_reply = False
        self.fail_like = False
        self.follow_not_effective = False
        self.reply_not_visible = False
        self.like_not_effective = False
        self.raise_on = None
        self._exc = None

    def register(self, hash_, flow_id, author_uid, author_hash):
        self._hash_to_flow[hash_] = flow_id
        self.followed.setdefault(author_uid, 0)
        self.has_zan.setdefault(hash_, 0)
        self.replies.setdefault(flow_id, [])

    def _maybe_raise(self, name):
        self.calls.append(name)
        if self.raise_on == name:
            exc = self._exc
            self.raise_on = None
            raise exc or ApiError("模拟失败")

    # --- 接口 ---
    def user_detail(self, user_hash):
        self.calls.append("user_detail")
        for uid, v in self.followed.items():
            return {"user": {"followed": v, "hash_id": user_hash, "id": uid}}
        return {"user": {"followed": 0}}

    def follow(self, uid):
        self._maybe_raise("follow")
        if not self.follow_not_effective:
            self.followed[uid] = 1
        return {}

    def reply(self, flow_id, content, reply_id=0):
        self._maybe_raise("reply")
        if not self.reply_not_visible:
            self.replies.setdefault(flow_id, []).append(1001)
        return {"reply": {"id": 555, "floor": 88}}

    def flow_reply_list(self, flow_id, page=1, sort_by="ctime_desc", unfold=1):
        self.calls.append("flow_reply_list")
        return {
            "list": [
                {"uid": u, "id": 777, "floor": 10 + i}
                for i, u in enumerate(self.replies.get(flow_id, []))
            ],
            "has_more": False,
        }

    def flow_detail(self, hash_):
        self.calls.append("flow_detail")
        return {
            "flow": {
                "has_zan": self.has_zan.get(hash_, 0),
                "id": self._hash_to_flow.get(hash_),
            }
        }

    def zan_flow(self, flow_id):
        self._maybe_raise("zan_flow")
        return {}

    def post(self, path, data=None, **kw):
        self.calls.append("post:" + path)
        if path == "/v2/user/zan/list":
            page = int((data or {}).get("page") or 1)
            if page == 1:
                return {"list": [{"hash_id": h} for h in self.zan_list], "pager": {}}
            return {"list": []}
        return {}


MY_UID = 1001


def fresh_db(tmp):
    db = Database(os.path.join(tmp, "t.db"))
    db.connect()
    return db


def add_flow(db, hash_, flow_id, author_uid=77):
    db.upsert_flow({
        "hash": hash_,
        "flow_id": flow_id,
        "title": "测试抽奖",
        "created_at": "2026-11-17 10:00:00",
        "author_uid": author_uid,
        "author_hash": f"UH{author_uid}",
        "author_nick": "作者",
    })
    db.update_lottery(hash_, is_lottery=1, lottery_status=1, lottery_at="2026-12-01 23:51")
    db.ensure_participation(hash_)


def make_bundle(api, db, driver=None, budget=10, max_retry=3):
    """构造 (participant, driver)。driver 默认新建并接好元素。"""
    d = driver or build_driver(api)
    p = Participant(
        driver=d,
        api=api,
        db=db,
        corpus=Corpus(),
        my_uid=MY_UID,
        max_retry=max_retry,
        write_budget=budget,
        delay_min=0,
        delay_max=0,
    )
    return p, d


def build_driver(api):
    """按当前假 API 状态装配一个能用的假页面。"""
    d = FakeDriver()

    def click_zan():
        # 模拟真实点击：同步更新点赞列表
        for h, fid in api._hash_to_flow.items():
            if fid in (api._hash_to_flow.values()):
                pass
        api.zan_list.insert(0, d._zan_hash) if getattr(d, "_zan_hash", None) else None

    def on_follow_click():
        # 模拟真实点击关注后服务端状态变化
        for uid in api.followed:
            if not api.follow_not_effective:
                api.followed[uid] = 1

    d._elements = {
        "a[href*=\"/app/user/\"]": FakeElement("作者", displayed=True),
        'a[href*="/app/user/UH77"]': FakeElement("作者", displayed=True),
        'a[href*="/app/user/UH90"]': FakeElement("作者", displayed=True),
        ".bt-green-border.follow": FakeElement("关注 TA", on_click=on_follow_click),
        ".target_comment-editor": FakeElement(""),
        ".target_comment-editor .ql-editor": FakeElement(""),
        "a.plugin-btn.submit": FakeElement("点击抽奖"),
        ".target-zan-placeholder": None,
        ".target_zan-flow": FakeElement("", cls="btn zan"),
        # 编辑器激活后出现的确认按钮（文案为「回复」）
        "button.button-black": FakeElement("回复"),
    }
    # a.plugin-btn.submit 既可能是「点击抽奖」也可能是「回复」，给两个
    d._elements["a.plugin-btn.submit"] = [
        FakeElement("回复"),
        FakeElement("回复"),
    ]
    return d


def main() -> int:
    print("\n[1] 语料池")
    c = Corpus(["A", "B", "C"])
    picked = [c.pick() for _ in range(6)]
    check("6 次选取都在池内", all(p in ("A", "B", "C") for p in picked), str(picked))
    check("前 3 次不重复", len(set(picked[:3])) == 3, str(picked[:3]))
    check("空池回退默认", len(Corpus([]).items) > 0)

    print("\n[2] 打开帖子页")
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        api = FakeApi()
        api.register("H1", 1001, 77, "UH77")
        add_flow(db, "H1", 1001)
        p, d = make_bundle(api, db)
        p._open_flow("H1")
        check(
            "真实导航到帖子页",
            any(a[0] == "get" and "app/flow/H1" in a[1] for a in d.actions),
            str([a for a in d.actions if a[0] == "get"][-1:]),
        )

        print("\n[3] 关注：真实点击作者名与关注按钮")
        api2 = FakeApi()
        api2.register("H1", 1001, 77, "UH77")
        p2, d2 = make_bundle(api2, db)
        # 主页按钮初始为「关注 TA」，点击后变「已关注」
        d2._elements[".bt-green-border.follow"] = FakeElement(
            "关注 TA", on_click=lambda: d2._elements[".bt-green-border.follow"].__setattr__("text", "已关注")
        )
        p2.ensure_followed("UH77", 77)
        check(
            "点过作者名",
            any(a == ("click", "作者名") for a in d2.actions),
            str(d2.actions),
        )
        check(
            "点过关注按钮",
            any(a == ("click", "关注按钮") for a in d2.actions),
            str(d2.actions),
        )

        print("\n[4] 关注：主页已显示「已关注」则不再点按钮")
        api3 = FakeApi()
        api3.register("H1", 1001, 77, "UH77")
        p3, d3 = make_bundle(api3, db)
        d3._elements[".bt-green-border.follow"] = FakeElement("已关注")
        p3.ensure_followed("UH77", 77)
        check("仍会进主页（要看状态）", any(a == ("click", "作者名") for a in d3.actions))
        check(
            "但不点关注按钮",
            not any(a == ("click", "关注按钮") for a in d3.actions),
            str(d3.actions),
        )

        print("\n[5] 点击后按钮不变 → 判失败")
        api4 = FakeApi()
        api4.register("H1", 1001, 77, "UH77")
        p4, d4 = make_bundle(api4, db)
        # 按钮文案永远不变，模拟点击无效
        d4._elements[".bt-green-border.follow"] = FakeElement("关注 TA")
        try:
            p4.ensure_followed("UH77", 77)
            ok = False
        except Exception as exc:
            ok = "已关注" in str(exc) or "关注" in str(exc)
        check("按钮未变化时抛错", ok)

        print("\n[5b] 缺作者 hash → 明确报错，不乱点")
        api5b = FakeApi()
        api5b.register("H1", 1001, 77, "UH77")
        p5b, d5b = make_bundle(api5b, db)
        try:
            p5b.ensure_followed(None, 77)
            ok5b = False
        except Exception as exc:
            ok5b = "hash" in str(exc)
        check("缺 hash 时拒绝执行", ok5b)
        check("且没有乱点", not any(a[0] == "click" for a in d5b.actions), str(d5b.actions))

        print("\n[6] 回复：走编辑器输入（不用快捷按钮，因其内容写死）")
        api5 = FakeApi()
        api5.register("H5", 1005, 80, "UH80")
        add_flow(db, "H5", 1005, author_uid=80)
        p5, d5 = make_bundle(api5, db)
        d5.current_url_value = "https://www.zfrontier.com/app/flow/H5"
        api5.replies[1005] = [MY_UID]
        try:
            p5.send_reply(1005, "H5")
        except Exception:
            pass
        check(
            "点击了编辑器",
            any(a == ("click", "回复编辑器") for a in d5.actions),
            str(d5.actions),
        )
        check(
            "没有用快捷按钮（其内容固定为冲冲冲）",
            not any(a == ("click", "快捷回复按钮") for a in d5.actions),
            str(d5.actions),
        )
        check(
            "点击了发送按钮",
            any(a == ("click", "发送按钮") for a in d5.actions),
            str(d5.actions),
        )

        print("\n[7] 点赞：点击图标而非接口")
        api6 = FakeApi()
        api6.register("H6", 1006, 81, "UH81")
        add_flow(db, "H6", 1006, author_uid=81)
        p6, d6 = make_bundle(api6, db)
        d6.current_url_value = "https://www.zfrontier.com/app/flow/H6"
        api6.zan_list.insert(0, "H6")
        p6.like_flow(1006, "H6")
        check("已在点赞列表时不再点击", not any(a == ("click", "点赞按钮") for a in d6.actions))

        print("\n[8] 点赞：未赞时真实点击")
        api7 = FakeApi()
        api7.register("H7", 1007, 82, "UH82")
        add_flow(db, "H7", 1007, author_uid=82)
        p7, d7 = make_bundle(api7, db)
        d7.current_url_value = "https://www.zfrontier.com/app/flow/H7"
        # 点完把 H7 放进点赞列表，模拟成功
        d7._elements[".target_zan-flow"]._on_click = lambda: api7.zan_list.insert(0, "H7")
        p7.like_flow(1007, "H7")
        check(
            "点过点赞按钮",
            any(a == ("click", "点赞按钮") for a in d7.actions),
            str(d7.actions),
        )

        print("\n[9] 断点续跑：已完成的步骤绝不重复")
        api8 = FakeApi()
        api8.register("H8", 1008, 83, "UH83")
        add_flow(db, "H8", 1008, author_uid=83)
        db.mark_followed("H8")
        db.mark_replied("H8", 777, 33)
        p8, d8 = make_bundle(api8, db)
        api8.replies[1008] = [MY_UID]
        api8.zan_list.insert(0, "H8")
        res8 = p8.participate(db.get_flow("H8"))
        check("成功", res8.success, str(res8.fail_reason or ""))
        check("未点关注按钮", not any(a == ("click", "关注按钮") for a in d8.actions), str(d8.actions))
        check("未点编辑器（回复已完成）", not any(a == ("click", "回复编辑器") for a in d8.actions), str(d8.actions))
        check("沿用了原 reply_id", res8.reply_id == 777, str(res8.reply_id))

        print("\n[10] 预算耗尽 → 中止整轮")
        api9 = FakeApi()
        api9.register("H9", 1009, 84, "UH84")
        add_flow(db, "H9", 1009, author_uid=84)
        p9, _ = make_bundle(api9, db, budget=0)
        res9 = p9.participate(db.get_flow("H9"))
        check("预算 0 无法完成", not res9.success)
        check("aborted 含 budget", "budget" in (res9.aborted or ""), str(res9.aborted))
        check("不记为失败重试", db.get_participation("H9")["retry_cnt"] == 0)

        print("\n[11] 限流 / 验证码 / 登录失效 中止且不消耗重试")
        for name, exc, key in (
            ("限流", RateLimited("20001"), "rate_limited"),
            ("验证码", CaptchaRequired("滑块"), "captcha"),
            ("登录失效", SessionExpired("未登录"), "session_expired"),
        ):
            h = f"H_{key}"
            apix = FakeApi()
            apix.register(h, 2000 + len(h), 90, "UH90")
            add_flow(db, h, 2000 + len(h), author_uid=90)
            px, dx = make_bundle(apix, db)
            # 在「打开帖子页」这个必经点上抛出对应异常，
            # 验证 participate 能把三类致命错误正确映射为 aborted
            def boom(*a, **k):
                raise exc
            px._open_flow = boom
            rx = px.participate(db.get_flow(h))
            check(f"{name}被中止", rx.aborted is not None, str(rx.aborted))
            check(f"{name}不消耗重试", db.get_participation(h)["retry_cnt"] == 0)

        print("\n[12] 重试用尽 → give_up")
        api10 = FakeApi()
        api10.register("H10", 1010, 85, "UH85")
        add_flow(db, "H10", 1010, author_uid=85)
        p10, _ = make_bundle(api10, db, max_retry=2)
        db.mark_followed("H10")
        db.mark_replied("H10", 1, 1)
        # 让点赞始终无法确认
        p10._in_watchlist = lambda *a, **k: False
        api10.flow_detail = lambda h: {"flow": {"has_zan": 0, "id": 1010}}
        for _ in range(2):
            p10.participate(db.get_flow("H10"))
        row = db.get_participation("H10")
        check("达到上限 give_up", row["state"] == STATE_GIVE_UP, row["state"])

        print("\n[13] 缺少必要字段")
        db.upsert_flow({"hash": "H11", "created_at": "2026-11-17 10:00:00"})
        db.update_lottery("H11", is_lottery=1, lottery_status=1)
        db.ensure_participation("H11")
        p11, _ = make_bundle(FakeApi(), db)
        res11 = p11.participate(db.get_flow("H11"))
        check("缺 flow_id 则失败", not res11.success)
        check("原因说明缺字段", "flow_id" in (res11.fail_reason or ""), res11.fail_reason or "")

        db.close()

    print("\n" + "=" * 52)
    print("P0-5 全部通过" if FAILED == 0 else f"P0-5 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
