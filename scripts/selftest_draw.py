"""
开奖结果检测自检。

重点验证：
  - 中奖判定用**数字 uid** 而非昵称（昵称可改、可重名）
  - 未开奖时不会误判为「未中奖」
  - rewardUser 为 null 时视为未开奖
  - 只检测「已参与完成」的帖子，没参与的不浪费请求
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Database  # noqa: E402
from app.zf.draw import (  # noqa: E402
    OUTCOME_LOST,
    OUTCOME_PENDING,
    OUTCOME_WON,
    STATUS_ENDED,
    STATUS_PENDING,
    check_finished_flows,
    parse_draw,
)

FAILED = 0
MY_UID = 3998041


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def make_flow(status, winners=None):
    """构造一个 flow 详情。winners 是 [(uid, nickname, prize)]。"""
    items = []
    for i, (uid, nick, prize) in enumerate(winners or [], start=1):
        items.append(
            {
                "id": 44000 + i,
                "name": prize,
                "rewardUser": (
                    None if uid is None else {"id": uid, "nickname": nick}
                ),
            }
        )
    return {
        "hash_id": "TEST1",
        "lottery": {
            "status": status,
            "prizesGroup": [{"name": "奖品", "cnt": len(items), "list": items}],
        },
    }


class FakeApi:
    def __init__(self, flows):
        self.flows = flows
        self.calls = []

    def flow_detail(self, hash_):
        self.calls.append(hash_)
        return {"flow": self.flows.get(hash_, {})}


def main() -> int:
    print("\n[1] 未开奖")
    f = make_flow(STATUS_PENDING, [(None, None, "奖品A")])
    r = parse_draw(f, MY_UID)
    check("未开奖不算结束", r.ended is False)
    check("未开奖 won=False", r.won is False)
    check("outcome=pending", r.outcome == OUTCOME_PENDING, r.outcome)
    check("无中奖者", r.winners == [])

    print("\n[2] 已开奖但不是我")
    f = make_flow(STATUS_ENDED, [(123456, "别人", "奖品A")])
    r = parse_draw(f, MY_UID)
    check("已结束", r.ended is True)
    check("未中奖", r.won is False)
    check("outcome=lost", r.outcome == OUTCOME_LOST, r.outcome)
    check("记录中奖者", len(r.winners) == 1 and r.winners[0]["uid"] == 123456)

    print("\n[3] 已开奖且是我")
    f = make_flow(STATUS_ENDED, [(MY_UID, "乍暖还寒", "Z1轴60颗")])
    r = parse_draw(f, MY_UID)
    check("中奖", r.won is True)
    check("outcome=won", r.outcome == OUTCOME_WON, r.outcome)
    check("奖品名正确", r.prize_name == "Z1轴60颗", str(r.prize_name))

    print("\n[4] 多人中奖，我在其中")
    f = make_flow(
        STATUS_ENDED,
        [(111, "甲", "奖A"), (MY_UID, "我", "奖B"), (222, "乙", "奖C")],
    )
    r = parse_draw(f, MY_UID)
    check("中奖", r.won is True)
    check("奖品是奖B", r.prize_name == "奖B", str(r.prize_name))
    check("记录全部中奖者", len(r.winners) == 3, str(len(r.winners)))

    print("\n[5] 用 uid 而非昵称判定（关键）")
    # 别人用了和我一样的昵称，但 uid 不同 → 不应判我中奖
    f = make_flow(STATUS_ENDED, [(999, "乍暖还寒", "奖A")])
    r = parse_draw(f, MY_UID)
    check("同昵称不同 uid 不算中奖", r.won is False)
    # 我用不同昵称但 uid 相同 → 应判中奖
    f = make_flow(STATUS_ENDED, [(MY_UID, "改了名", "奖B")])
    r = parse_draw(f, MY_UID)
    check("同 uid 不同昵称仍算中奖", r.won is True)

    print("\n[6] status 滞后但有中奖者")
    f = make_flow(STATUS_PENDING, [(MY_UID, "我", "奖A")])
    r = parse_draw(f, MY_UID)
    check("有中奖者即视为已开奖", r.ended is True)
    check("判为中奖", r.won is True)

    print("\n[7] 异常数据不崩")
    check("无 lottery", parse_draw({"hash_id": "X"}, MY_UID).ended is False)
    check("lottery 非 dict", parse_draw({"lottery": "x"}, MY_UID).won is False)
    f = {"hash_id": "X", "lottery": {"status": 2, "prizesGroup": "bad"}}
    check("prizesGroup 非列表", parse_draw(f, MY_UID).ended is True)
    f = {"hash_id": "X", "lottery": {"status": 2, "prizesGroup": [{"list": [None, 1]}]}}
    check("list 含脏数据", parse_draw(f, MY_UID).won is False)

    print("\n[8] 批量检测只查已参与的帖")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "t.db"))
        db.connect()

        # A: 已参与、未开奖
        db.upsert_flow({"hash": "A", "created_at": "2026-11-18 10:00:00", "title": "A帖"})
        db.update_lottery("A", is_lottery=1, lottery_status=1)
        db.ensure_participation("A")
        db.mark_state("A", "done")

        # B: 已参与、已开奖中奖
        db.upsert_flow({"hash": "B", "created_at": "2026-11-18 09:00:00", "title": "B帖"})
        db.update_lottery("B", is_lottery=1, lottery_status=2)
        db.ensure_participation("B")
        db.mark_state("B", "done")

        # C: 已参与、已开奖未中
        db.upsert_flow({"hash": "C", "created_at": "2026-11-18 08:00:00", "title": "C帖"})
        db.update_lottery("C", is_lottery=1, lottery_status=2)
        db.ensure_participation("C")
        db.mark_state("C", "done")

        # D: 未参与（不应被查询）
        db.upsert_flow({"hash": "D", "created_at": "2026-11-18 07:00:00", "title": "D帖"})
        db.update_lottery("D", is_lottery=1, lottery_status=2)

        rows = db.flows_awaiting_result()
        hashes = {r["hash"] for r in rows}
        check("待检测含 A/B/C", {"A", "B", "C"} <= hashes, str(hashes))
        check("不含未参与帖 D", "D" not in hashes)

        api = FakeApi(
            {
                "A": make_flow(STATUS_PENDING, [(None, None, "奖")]),
                "B": make_flow(STATUS_ENDED, [(MY_UID, "我", "大奖"), (222, "乙", "小奖")]),
                "C": make_flow(STATUS_ENDED, [(333, "丙", "奖")]),
            }
        )
        stats = check_finished_flows(db, api, MY_UID)
        check("检测 3 条", stats["checked"] == 3, str(stats["checked"]))
        check("中奖 1", stats["won"] == 1, str(stats["won"]))
        check("未中奖 1", stats["lost"] == 1, str(stats["lost"]))
        check("未开奖 1", stats["pending"] == 1, str(stats["pending"]))
        check("未请求未参与的 D", "D" not in api.calls, str(api.calls))

        # 落库校验
        b = db.get_participation("B")
        check("B 记录为 won", b["outcome"] == OUTCOME_WON, str(b["outcome"]))
        check("B 记录奖品", b["prize"] == "大奖", str(b["prize"]))
        c_row = db.get_participation("C")
        check("C 记录为 lost", c_row["outcome"] == OUTCOME_LOST, str(c_row["outcome"]))
        a_row = db.get_participation("A")
        check("A 仍未记录结果", a_row["outcome"] is None)

        # 统计
        st = db.outcome_stats()
        check("统计 won=1", st["won"] == 1, str(st))
        check("统计 lost=1", st["lost"] == 1, str(st))
        check("统计 awaiting=1", st["awaiting"] == 1, str(st))

        # 已检测过的不再重复查
        rows2 = db.flows_awaiting_result()
        check(
            "已记录结果的不再待检",
            {r["hash"] for r in rows2} == {"A"},
            str({r["hash"] for r in rows2}),
        )

        db.close()

    print("\n" + "=" * 52)
    print("draw 全部通过" if FAILED == 0 else f"draw 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
