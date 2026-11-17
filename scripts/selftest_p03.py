"""
P0-3 自检：扫描器与停止条件。

第 1 部分用**实测真实数据**（12 天 60 条，含 16 对倒挂）离线验证算法；
第 2 部分连真实站点验证扫描能跑通。

停止条件是整个扫描的关键，写错会漏帖，所以这里用真实倒挂样本做回归。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.zf.scanner import (  # noqa: E402
    ScannedFlow,
    compute_stop_boundary,
    should_stop,
)

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def mk(dt: datetime, h: str = "x") -> ScannedFlow:
    return ScannedFlow(
        hash=h, flow_id=1, title="t", created_at=dt,
        author_uid=1, author_hash="u", author_nick="n",
    )


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# 实测抓取的真实顺序（2026-11-17 采集，60 条中的前 20 条）
REAL_ORDER = [
    "2026-11-17 12:47:25",
    "2026-11-17 11:09:07",
    "2026-11-16 20:10:48",   # ← 倒挂：比下一条旧 12.3h
    "2026-11-17 08:30:33",
    "2026-11-16 23:50:50",
    "2026-11-16 15:02:10",   # ← 倒挂 0.8h
    "2026-11-16 15:51:17",
    "2026-11-16 15:11:17",
    "2026-11-16 13:55:43",   # ← 倒挂 0.8h
    "2026-11-16 14:41:54",
    "2026-11-16 14:33:33",
    "2026-11-16 11:26:13",
    "2026-11-15 15:22:02",
    "2026-11-15 20:55:29",   # ← 倒挂
    "2026-11-15 17:44:39",
    "2026-11-14 18:07:27",
    "2026-11-14 15:23:47",
    "2026-11-14 14:54:50",
    "2026-11-14 12:53:23",
    "2026-11-14 10:41:38",
]


def main() -> int:
    print("\n[1] 边界计算")
    now = parse("2026-11-17 09:00:00")
    # 硬边界 = now - 16 天 = 2026-11-01 09:00

    b = compute_stop_boundary(now, 14, 2, None)
    check("首次运行 = now-16天", b == parse("2026-11-01 09:00:00"), str(b))

    # 水位较新时，增量边界更晚 → 扫描更浅，这是增量优化生效的关键
    wm = parse("2026-11-10 00:00:00")
    b = compute_stop_boundary(now, 14, 2, wm)
    check("增量生效：取水位-2天", b == parse("2026-11-08 00:00:00"), str(b))
    check(
        "增量边界比硬边界更浅（少扫）",
        b > parse("2026-11-01 09:00:00"),
        f"{b} > 11-01 09:00",
    )

    # 水位很新（昨天）→ 只需回看 2 天
    wm_new = parse("2026-11-16 00:00:00")
    b = compute_stop_boundary(now, 14, 2, wm_new)
    check("水位为昨天 → 只回看 2 天", b == parse("2026-11-14 00:00:00"), str(b))

    # 水位很旧 → 被硬边界兜住，不会扫更深
    wm_old = parse("2026-10-01 00:00:00")
    b = compute_stop_boundary(now, 14, 2, wm_old)
    check(
        "水位很旧时受硬边界兜底",
        b == parse("2026-11-01 09:00:00"),
        str(b),
    )

    # 水位恰好在硬边界附近（软边界更早）→ 硬边界胜出
    wm_edge = parse("2026-11-03 00:00:00")
    b = compute_stop_boundary(now, 14, 2, wm_edge)
    check(
        "软边界早于硬边界时取硬边界",
        b == parse("2026-11-01 09:00:00"),
        f"soft=11-01 00:00, hard=11-01 09:00 → {b}",
    )

    print("\n[2] 停止条件（真实倒挂数据）")
    items = [mk(parse(t), f"h{i}") for i, t in enumerate(REAL_ORDER)]

    # 边界设在 11-16 12:00：前 8 条都晚于它，第 9 条(11-16 13:55)也晚于它
    boundary = parse("2026-11-16 12:00:00")
    check("条数不足时不停", should_stop(items[:5], boundary, 10) is False)

    # 只取末尾 3 条（全部早于边界），但 confirm_count=10 > len，走「全部早于」分支
    tail3 = items[-3:]
    check(
        "末尾全早于边界且不足 confirm 数 → 停",
        should_stop(tail3, boundary, 10) is True,
    )

    # 关键回归：如果只看「第一条早于边界就停」会漏帖
    # 构造：位置2旧、位置3新，边界在两者之间
    tricky = [
        mk(parse("2026-11-17 12:47:25"), "a"),
        mk(parse("2026-11-16 20:10:48"), "b"),   # 早于边界
        mk(parse("2026-11-17 08:30:33"), "c"),   # 晚于边界，不应被漏
        mk(parse("2026-11-15 10:00:00"), "d"),
    ]
    edge = parse("2026-11-16 23:00:00")
    check(
        "单条早于边界不足以停止（防倒挂漏帖）",
        should_stop(tricky, edge, 2) is False,
        "末尾2条中 c 晚于边界",
    )

    # confirm_count=1 时，末尾 d 早于边界 → 会停，这正是要避免的配置
    check(
        "confirm_count=1 过于激进（仅作对照）",
        should_stop(tricky, edge, 1) is True,
    )

    print("\n[3] 停止条件边界值")
    check("空列表不停", should_stop([], boundary, 10) is False)
    # confirm_count=0 的语义：只看末尾一条（不应写成 items[-0:] 的退化行为）
    # 取两条都有新有旧的数据，末尾一条晚于边界 → 不停
    pair_new_tail = [
        mk(parse("2026-11-16 10:00:00"), "old"),
        mk(parse("2026-11-17 10:00:00"), "new"),  # 末尾这条晚于边界
    ]
    check(
        "confirm=0 时只看末尾一条（末尾新→不停）",
        should_stop(pair_new_tail, parse("2026-11-16 23:00:00"), 0) is False,
    )
    check(
        "confirm=0 时只看末尾一条（末尾旧→停）",
        should_stop(pair_new_tail, parse("2026-11-17 23:00:00"), 0) is True,
    )
    check(
        "全部早于边界 → 停",
        should_stop(
            [mk(parse("2026-10-01 00:00:00")), mk(parse("2026-10-02 00:00:00"))],
            boundary,
            10,
        )
        is True,
    )

    print("\n[4] ScannedFlow 解析")
    f = ScannedFlow.from_js(
        {
            "hash": "ABC123",
            "hashId": 100,
            "title": "  标题  ",
            "createdAt": "2026-11-17 10:00:00",
            "userId": 7,
            "userHash": "UH",
            "userNick": "人",
        }
    )
    check("字段解析正确", f is not None and f.hash == "ABC123" and f.title == "标题")
    check("时间解析正确", f.created_at == parse("2026-11-17 10:00:00"))
    check("缺 hash 返回 None", ScannedFlow.from_js({"createdAt": "2026-11-17 10:00:00"}) is None)
    check("坏时间返回 None", ScannedFlow.from_js({"hash": "A", "createdAt": "??"}) is None)
    check("to_db 时间格式", f.to_db()["created_at"] == "2026-11-17 10:00:00")

    # 真实数据下的完整判定：模拟扫到 20 条后，边界设 11-13
    print("\n[5] 真实序列完整判定")
    b2 = parse("2026-11-13 00:00:00")
    check("末尾未全部早于 11-13 → 继续翻", should_stop(items, b2, 10) is False)
    b3 = parse("2026-11-15 00:00:00")
    tail10 = items[-10:]
    all_old = all(i.created_at < b3 for i in tail10)
    check(
        "末尾10条全早于 11-15 → 停",
        should_stop(items, b3, 10) is all_old,
        f"实际={should_stop(items, b3, 10)}",
    )

    print("\n[6] 联网扫描（真实站点）")
    try:
        from app import config as cfg_mod
        from app.zf.driver import Driver, wait_selenium_ready
        from app.zf.scanner import Scanner

        cfg = cfg_mod.load()
        if not wait_selenium_ready(cfg.selenium_url, timeout=30):
            check("Selenium 可达", False, cfg.selenium_url)
        else:
            check("Selenium 可达", True)
            driver = Driver(cfg.selenium_url)
            try:
                driver.start()
                sc = Scanner(driver, tag_id=2007, confirm_count=10, max_scroll=3)
                # 故意用很老的边界，强制只滚 3 轮就返回（避免真跑满）
                boundary = datetime.now() - timedelta(days=30)
                flows = sc.scan(boundary)
                check("扫描到帖子", len(flows) > 0, f"{len(flows)} 条")
                check("hash 非空", all(f.hash for f in flows))
                check("时间可解析", all(f.created_at for f in flows))
                check("无重复", len({f.hash for f in flows}) == len(flows))
                if flows:
                    print(f"     最新：{flows[0].created_at} {flows[0].title[:20]}")
                    print(f"     最旧：{flows[-1].created_at} {flows[-1].title[:20]}")
                    span = (flows[0].created_at - flows[-1].created_at).days
                    check("覆盖多天", span >= 1, f"{span} 天")
                    check("作者字段可读", any(f.author_uid for f in flows))
            finally:
                driver.quit()
    except ImportError as exc:
        print(f"  ⏭  缺少依赖，跳过联网测试：{exc}")

    print("\n" + "=" * 52)
    print("P0-3 全部通过" if FAILED == 0 else f"P0-3 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
