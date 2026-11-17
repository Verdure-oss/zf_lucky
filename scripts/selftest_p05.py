"""
P0-5 自检：参与器。

**本测试全部使用假 API，不对真实账号发起任何写操作。**
真实写操作（关注/回复/点赞）只在用户明确授权后，通过 main.py 触发。

重点是验证那些「错了会永久漏帖」的逻辑：
  - 步骤顺序必须是 关注 → 回复 → 点赞
  - 已点赞时不能重复点赞（会变成取消赞，抹掉标记）
  - 回复后必须能回读到本人回复才算出成功
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


class FakeApi:
    """假 API：记录调用顺序，可编排各种故障。"""

    def __init__(self):
        self.calls = []
        self.followed = {}        # uid -> 0/1
        self.has_zan = {}         # hash -> 0/1
        self.replies = {}         # flow_id -> [uid,...]
        self._hash_to_flow = {}   # hash -> flow_id

        # 故障开关
        self.fail_follow = False
        self.fail_reply = False
        self.fail_like = False
        self.follow_not_effective = False  # 关注请求成功但回读仍 0
        self.reply_not_visible = False     # 回复接口成功但列表查不到
        self.like_not_effective = False
        self.raise_on = None               # 在下一次调用时抛异常

    def _maybe_raise(self, name: str):
        self.calls.append(name)
        if self.raise_on == name:
            exc = getattr(self, "_exc", None)
            self.raise_on = None
            raise exc or ApiError("模拟失败")

    def register(self, hash_: str, flow_id: int, author_uid: int, author_hash: str):
        self._hash_to_flow[hash_] = flow_id
        self.followed.setdefault(author_uid, 0)
        self.has_zan.setdefault(hash_, 0)
        self.replies.setdefault(flow_id, [])

    # ---- 接口 ----

    def user_detail(self, user_hash):
        self.calls.append("user_detail")
        for uid, v in self.followed.items():
            return {"user": {"followed": v, "hash_id": user_hash}}
        return {"user": {"followed": 0, "hash_id": user_hash}}

    def follow(self, uid):
        self._maybe_raise("follow")
        if self.fail_follow:
            raise ApiError("关注失败")
        if not self.follow_not_effective:
            self.followed[uid] = 1
        return {}

    def reply(self, flow_id, content, reply_id=0):
        self._maybe_raise("reply")
        if self.fail_reply:
            raise ApiError("回复失败")
        if not self.reply_not_visible:
            self.replies.setdefault(flow_id, []).append(1001)
        return {"reply": {"id": 555, "floor": 88, "content": content}}

    def flow_reply_list(self, flow_id, page=1, sort_by="ctime_desc", unfold=1):
        self.calls.append("flow_reply_list")
        return {"list": [{"uid": u, "id": 1} for u in self.replies.get(flow_id, [])],
                "has_more": False}

    def flow_detail(self, hash_):
        self.calls.append("flow_detail")
        return {"flow": {"has_zan": self.has_zan.get(hash_, 0), "id": self._hash_to_flow.get(hash_)}}

    def zan_flow(self, flow_id):
        self._maybe_raise("zan_flow")
        if self.fail_like:
            raise ApiError("点赞失败")
        if not self.like_not_effective:
            for h, fid in self._hash_to_flow.items():
                if fid == flow_id:
                    self.has_zan[h] = 1
        return {}

    def unzan_flow(self, flow_id):
        self.calls.append("unzan_flow")
        return {}


MY_UID = 1001


def fresh_db(tmp: str) -> Database:
    db = Database(os.path.join(tmp, "t.db"))
    db.connect()
    return db


def add_flow(db: Database, hash_: str, flow_id: int, author_uid: int = 77):
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


def main() -> int:
    print("\n[1] 语料池")
    c = Corpus(["A", "B", "C"])
    picked = [c.pick() for _ in range(6)]
    check("6 次选取都在池内", all(p in ("A", "B", "C") for p in picked), str(picked))
    check("前 3 次不重复", len(set(picked[:3])) == 3, str(picked[:3]))
    check("空池回退默认", len(Corpus([]).items) > 0)
    long_text = Corpus(["x" * 500]).pick()
    check("超长被截断", len(long_text) <= 100, f"len={len(long_text)}")

    print("\n[2] 完整成功流程")
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(tmp)
        api = FakeApi()
        api.register("H1", 1001, 77, "UH77")
        add_flow(db, "H1", 1001)
        api = FakeApi()
        # 重新注册，保证 followed 初始为 0
        api.register("H1", 1001, 77, "UH77")
        p = Participant(api, db, Corpus(), MY_UID, max_retry=3, write_budget=10)
        res = p.participate(db.get_flow("H1"))

        check("参与成功", res.success, str(res.fail_reason or ""))
        check("三步都完成", res.steps == ["follow", "reply", "like"], str(res.steps))
        check("记录 reply_id", res.reply_id == 555)
        check("记录楼层", res.floor == 88)

        row = db.get_participation("H1")
        check("状态为 done", row["state"] == STATE_DONE)
        check("关注时间已记", bool(row["followed_at"]))
        check("回复时间已记", bool(row["replied_at"]))
        check("点赞时间已记", bool(row["liked_at"]))

        # 关键：顺序必须是 follow → reply → like
        order = [c for c in api.calls if c in ("follow", "reply", "zan_flow")]
        check("顺序：关注→回复→点赞", order == ["follow", "reply", "zan_flow"], str(order))
        check("写操作计数=3", p.write_used == 3, f"{p.write_used}")

        print("\n[3] 已点赞时不重复点赞")
        api2 = FakeApi()
        api2.register("H1", 1001, 77, "UH77")
        api2.has_zan["H1"] = 1  # 已点赞
        p2 = Participant(api2, db, Corpus(), MY_UID, write_budget=10)
        res2 = p2.participate(db.get_flow("H1"))
        check("已点赞不调用 zan_flow", "zan_flow" not in api2.calls, str(api2.calls))
        check("仍判定成功", res2.success, str(res2.fail_reason or ""))

        print("\n[4] 已关注时跳过关注")
        api3 = FakeApi()
        api3.register("H2", 1002, 78, "UH78")
        api3.followed[78] = 1
        add_flow(db, "H2", 1002, author_uid=78)
        p3 = Participant(api3, db, Corpus(), MY_UID, write_budget=10)
        res3 = p3.participate(db.get_flow("H2"))
        check("已关注不调用 follow", "follow" not in api3.calls, str(api3.calls))
        check("成功", res3.success, str(res3.fail_reason or ""))

        print("\n[5] 关注未生效 → 判失败")
        api4 = FakeApi()
        api4.register("H3", 1003, 79, "UH79")
        api4.follow_not_effective = True
        add_flow(db, "H3", 1003, author_uid=79)
        p4 = Participant(api4, db, Corpus(), MY_UID, max_retry=3, write_budget=10)
        res4 = p4.participate(db.get_flow("H3"))
        check("关注未生效则失败", not res4.success)
        check("失败原因提到关注", "关注" in (res4.fail_reason or ""), res4.fail_reason or "")
        check("没有继续回复", "reply" not in api4.calls, str(api4.calls))
        check("状态为 failed", db.get_participation("H3")["state"] == STATE_FAILED)

        print("\n[6] 回复不可见 → 判失败（防假成功）")
        api5 = FakeApi()
        api5.register("H4", 1004, 80, "UH80")
        api5.reply_not_visible = True
        add_flow(db, "H4", 1004, author_uid=80)
        p5 = Participant(api5, db, Corpus(), MY_UID, write_budget=10)
        res5 = p5.participate(db.get_flow("H4"))
        check("接口 ok 但列表查不到 → 失败", not res5.success)
        check("原因提到回读", "回读" in (res5.fail_reason or ""), res5.fail_reason or "")
        check("没有继续点赞", "zan_flow" not in api5.calls, str(api5.calls))

        print("\n[7] 点赞未生效 → 判失败")
        api6 = FakeApi()
        api6.register("H5", 1005, 81, "UH81")
        api6.like_not_effective = True
        add_flow(db, "H5", 1005, author_uid=81)
        p6 = Participant(api6, db, Corpus(), MY_UID, write_budget=10)
        res6 = p6.participate(db.get_flow("H5"))
        check("点赞未生效则失败", not res6.success)
        check("原因提到 has_zan", "has_zan" in (res6.fail_reason or ""), res6.fail_reason or "")

        print("\n[8] 限流中止且不消耗重试")
        api7 = FakeApi()
        api7.register("H6", 1006, 82, "UH82")
        add_flow(db, "H6", 1006, author_uid=82)
        p7 = Participant(api7, db, Corpus(), MY_UID, max_retry=3, write_budget=10)
        api7.raise_on = "follow"
        api7._exc = RateLimited("20001")
        res7 = p7.participate(db.get_flow("H6"))
        check("被中止", res7.aborted is not None, str(res7.aborted))
        row7 = db.get_participation("H6")
        check("不消耗重试次数", row7["retry_cnt"] == 0, f"retry={row7['retry_cnt']}")

        print("\n[9] 验证码中止且不消耗重试")
        api8 = FakeApi()
        api8.register("H7", 1007, 83, "UH83")
        add_flow(db, "H7", 1007, author_uid=83)
        p8 = Participant(api8, db, Corpus(), MY_UID, max_retry=3, write_budget=10)
        api8.raise_on = "follow"
        api8._exc = CaptchaRequired("滑块")
        res8 = p8.participate(db.get_flow("H7"))
        check("被中止", res8.aborted is not None)
        check("aborted 含 captcha", "captcha" in (res8.aborted or ""), str(res8.aborted))
        check("不消耗重试", db.get_participation("H7")["retry_cnt"] == 0)

        print("\n[10] 登录失效中止")
        api9 = FakeApi()
        api9.register("H8", 1008, 84, "UH84")
        add_flow(db, "H8", 1008, author_uid=84)
        p9 = Participant(api9, db, Corpus(), MY_UID, write_budget=10)
        api9.raise_on = "follow"
        api9._exc = SessionExpired("未登录")
        res9 = p9.participate(db.get_flow("H8"))
        check("含 session_expired", "session_expired" in (res9.aborted or ""), str(res9.aborted))

        print("\n[11] 重试用尽 → give_up")
        api10 = FakeApi()
        api10.register("H9", 1009, 85, "UH85")
        api10.fail_follow = True
        add_flow(db, "H9", 1009, author_uid=85)
        p10 = Participant(api10, db, Corpus(), MY_UID, max_retry=2, write_budget=50)
        for i in range(2):
            p10.participate(db.get_flow("H9"))
        row10 = db.get_participation("H9")
        check("达到上限后 give_up", row10["state"] == STATE_GIVE_UP, row10["state"])
        check("重试次数=2", row10["retry_cnt"] == 2, f"{row10['retry_cnt']}")

        print("\n[12] 写预算耗尽 → 中止整轮")
        api11 = FakeApi()
        api11.register("H10", 1010, 86, "UH86")
        add_flow(db, "H10", 1010, author_uid=86)
        p11 = Participant(api11, db, Corpus(), MY_UID, write_budget=1)
        res11 = p11.participate(db.get_flow("H10"))
        check("预算 1 不足以完成三步", not res11.success)
        check("aborted 含 budget", "budget" in (res11.aborted or ""), str(res11.aborted))
        check("不记为失败重试", db.get_participation("H10")["retry_cnt"] == 0)

        print("\n[13] 缺少必要字段")
        db.upsert_flow({"hash": "H11", "created_at": "2026-11-17 10:00:00"})
        db.update_lottery("H11", is_lottery=1, lottery_status=1)
        db.ensure_participation("H11")
        p12 = Participant(FakeApi(), db, Corpus(), MY_UID, write_budget=10)
        res12 = p12.participate(db.get_flow("H11"))
        check("缺 flow_id 则失败", not res12.success)
        check("原因说明缺字段", "flow_id" in (res12.fail_reason or ""), res12.fail_reason or "")

        db.close()

    print("\n" + "=" * 52)
    print("P0-5 全部通过" if FAILED == 0 else f"P0-5 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
