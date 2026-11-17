"""
P0-4 自检：抽奖判定与缓存。

第 1 部分用真实详情 JSON 的字段结构做离线解析测试；
第 2 部分连真实站点验证「哪些是抽奖、哪些不是」的判定正确。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Database  # noqa: E402
from app.zf.judge import (  # noqa: E402
    STATUS_PENDING,
    extract_flow_meta,
    parse_lottery,
)

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


# 实测抓取的真实 lottery 结构（ZwPnZ6lzj5xy）
REAL_LOTTERY = {
    "status": 1,
    "lottery_at": "2026-12-01 23:51",
    "version": 2,
    "status_str": "待抽奖",
    "officially": 0,
    "filters": {"reply": True},
    "time_str": "2026-12-01 23:51",
    "can_edit": False,
    "author": {"id": 3839009, "nickname": "M Z", "hash_id": "BOEeXlzZBG9RX"},
    "prizesGroup": [
        {
            "name": "Z1轴60颗",
            "cnt": 6,
            "list": [
                {"id": 44024, "name": "Z1轴60颗", "rewardUser": None},
                {"id": 44025, "name": "Z1轴60颗", "rewardUser": None},
            ],
        }
    ],
}


def main() -> int:
    print("\n[1] 非抽奖帖识别")
    info = parse_lottery({"id": 1, "title": "普通帖"})
    check("无 lottery 字段 → 非抽奖", info.is_lottery is False)
    check("非抽奖时 is_active=False", info.is_active is False)
    check("非抽奖时 status 为 None", info.status is None)

    check("lottery 为 None → 非抽奖", parse_lottery({"lottery": None}).is_lottery is False)
    check("lottery 为字符串 → 非抽奖", parse_lottery({"lottery": "x"}).is_lottery is False)
    check("lottery 为空 dict → 是抽奖帖", parse_lottery({"lottery": {}}).is_lottery is True)

    print("\n[2] 真实抽奖结构解析")
    info = parse_lottery({"lottery": REAL_LOTTERY})
    check("识别为抽奖帖", info.is_lottery is True)
    check("status=1", info.status == STATUS_PENDING, f"status={info.status}")
    check("仍在抽奖期", info.is_active is True)
    check("开奖时间正确", info.lottery_at == "2026-12-01 23:51", str(info.lottery_at))
    check("filters.reply 不误判为需关注", info.needs_follow is False)
    check("奖品解析为 1 组", len(info.prizes) == 1, str(info.prizes))
    check("奖品名正确", info.prizes[0]["name"] == "Z1轴60颗")
    check("奖品数量正确", info.prizes[0]["cnt"] == 6)
    check("奖品摘要可读", "Z1轴60颗" in info.prizes_summary(), info.prizes_summary())
    check("奖品 JSON 可序列化", json.loads(info.prizes_json())[0]["name"] == "Z1轴60颗")

    print("\n[3] 需关注标记")
    lot_follow = dict(REAL_LOTTERY)
    lot_follow["filters"] = {"reply": True, "follow": True}
    check("filters.follow=true → needs_follow", parse_lottery({"lottery": lot_follow}).needs_follow is True)

    print("\n[4] 已开奖识别")
    for st in (2, 3, 0):
        lot = dict(REAL_LOTTERY)
        lot["status"] = st
        info = parse_lottery({"lottery": lot})
        check(f"status={st} 不算有效抽奖", info.is_active is False, f"is_active={info.is_active}")
        check(f"status={st} 仍标记为抽奖帖", info.is_lottery is True)

    print("\n[5] 旧版 prizes 平铺结构")
    old = {"status": 1, "lottery_at": "2026-11-20 10:00", "prizes": [
        {"name": "旧奖品A", "cnt": 1}, {"name": "旧奖品B", "cnt": 2},
    ]}
    info = parse_lottery({"lottery": old})
    check("旧结构奖品可解析", len(info.prizes) == 2, str(info.prizes))
    check("旧结构摘要", "旧奖品A" in info.prizes_summary())

    print("\n[6] extract_flow_meta")
    info = parse_lottery({"lottery": REAL_LOTTERY})
    info.raw = {
        "lottery": REAL_LOTTERY,
        "flow": {
            "id": 258582,
            "title": "  Y2x轴第三批补货  ",
            "created_at": "2026-11-17 12:47:25",
            "user": {"id": 3839009, "hash_id": "BOEeXlzZBG9RX", "nickname": "M Z"},
        },
    }
    meta = extract_flow_meta(info)
    check("flow_id", meta["flow_id"] == 258582)
    check("标题去空白", meta["title"] == "Y2x轴第三批补货")
    check("作者 uid", meta["author_uid"] == 3839009)
    check("作者 hash", meta["author_hash"] == "BOEeXlzZBG9RX")

    print("\n[7] 缓存行为（内存库）")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "t.db"))
        db.connect()

        db.upsert_flow({"hash": "LOT1", "created_at": "2026-11-17 10:00:00", "title": "抽奖"})
        db.update_lottery("LOT1", is_lottery=1, lottery_status=1, lottery_at="2026-12-01 23:51")
        db.ensure_participation("LOT1")

        db.upsert_flow({"hash": "PLAIN1", "created_at": "2026-11-17 09:00:00", "title": "普通"})
        db.update_lottery("PLAIN1", is_lottery=0)

        need = db.flows_need_detail()
        need_hashes = {f["hash"] for f in need}
        check("非抽奖帖不再查详情", "PLAIN1" not in need_hashes, f"待查 {len(need)} 条")
        check("有效抽奖帖仍复查", "LOT1" in need_hashes)

        # 已开奖后移出复查队列
        db.update_lottery("LOT1", is_lottery=1, lottery_status=2)
        need_hashes = {f["hash"] for f in db.flows_need_detail()}
        check("已开奖后移出复查队列", "LOT1" not in need_hashes)
        check("仍不查非抽奖帖", "PLAIN1" not in need_hashes)

        db.close()

    print("\n[8] 联网判定（真实站点）")
    try:
        from app import config as cfg_mod
        from app.zf.api import Api
        from app.zf.driver import Driver, wait_selenium_ready

        cfg = cfg_mod.load()
        if not wait_selenium_ready(cfg.selenium_url, timeout=30):
            check("Selenium 可达", False, cfg.selenium_url)
        else:
            driver = Driver(cfg.selenium_url)
            try:
                driver.start()
                driver.get("https://www.zfrontier.com/app/tag/2007")
                api = Api(driver, delay_min=1, delay_max=2)
                checker = __import__(
                    "app.zf.judge", fromlist=["DetailChecker"]
                ).DetailChecker(api)

                # 已知：这 5 条是抽奖（实测），3 条不是
                known_lottery = ["ZwPnZ6lzj5xy", "Y5L19vMxqKzB", "7G9RMd35mP5p"]
                known_plain = ["mpqrQK865ja5", "bz5K5jK0pGxk", "ZYZodOLwlxkX"]

                for h in known_lottery:
                    info = checker.check(h)
                    check(
                        f"{h} 判定为抽奖",
                        info.is_lottery,
                        f"status={info.status} 奖品={info.prizes_summary()[:20]}",
                    )
                for h in known_plain:
                    info = checker.check(h)
                    check(f"{h} 判定为非抽奖", not info.is_lottery)

                check("详情请求计数", checker.detail_count == 6, f"{checker.detail_count} 次")
            finally:
                driver.quit()
    except ImportError as exc:
        print(f"  ⏭  缺少依赖，跳过联网：{exc}")

    print("\n" + "=" * 52)
    print("P0-4 全部通过" if FAILED == 0 else f"P0-4 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
