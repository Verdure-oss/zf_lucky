"""
配置与数据层自检。

这是 P0-1 的可执行验收：不进容器也能在宿主机跑，用临时目录验证
建表、写入、状态机、水位等核心逻辑是否真的работает。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as cfg_mod  # noqa: E402
from app.db import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_GIVE_UP,
    Database,
    now_str,
)


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
    return ok


def _expect_value_error(db: Database, hash_: str) -> bool:
    """确认对未入库的帖子创建参与记录会抛 ValueError。"""
    try:
        db.ensure_participation(hash_)
        return False
    except ValueError:
        return True


def main() -> int:
    failures = 0

    print("\n[1] 配置加载")
    cfg = cfg_mod.Config()
    ok = check("默认配置可构造", cfg.scan_window_days == 14)
    failures += 0 if ok else 1
    ok = check(
        "边界天数 = 窗口 + 边距",
        cfg.scan_boundary_days == 16,
        f"{cfg.scan_window_days}+{cfg.scan_buffer_days}={cfg.scan_boundary_days}",
    )
    failures += 0 if ok else 1

    os.environ["ZF_COOKIE"] = "a=1; b=2; c=3"
    os.environ["REQUEST_DELAY_MIN"] = "9"
    os.environ["REQUEST_DELAY_MAX"] = "3"
    cfg2 = cfg_mod.load()
    ok = check("区间反了会自动交换", cfg2.request_delay_min == 3 and cfg2.request_delay_max == 9)
    failures += 0 if ok else 1
    ok = check("Cookie 脱敏不含值", "=1" not in cfg_mod.mask_cookie(cfg2.cookie))
    failures += 0 if ok else 1
    ok = check("Cookie 脱敏含项名", "a, b, c" in cfg_mod.mask_cookie(cfg2.cookie))
    failures += 0 if ok else 1

    os.environ["RUN_AT"] = "25:99"
    problems = cfg_mod.load().validate()
    ok = check("非法时间被 validate 捕获", any("RUN_AT" in p for p in problems))
    failures += 0 if ok else 1
    os.environ.pop("RUN_AT", None)

    print("\n[2] 数据库建表")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        db.connect()
        tables = {
            r[0]
            for r in db.connect()
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        for t in ("flows", "participations", "run_logs", "kv"):
            ok = check(f"表 {t} 存在", t in tables)
            failures += 0 if ok else 1

        print("\n[3] flows 写入与更新")
        n1 = db.upsert_flow(
            {
                "hash": "AAA111",
                "flow_id": 100,
                "title": "测试帖",
                "created_at": "2026-11-17 10:00:00",
                "author_uid": 1,
                "author_hash": "UH1",
                "author_nick": "作者",
            }
        )
        ok = check("首次插入返回 True", n1 is True)
        failures += 0 if ok else 1
        n2 = db.upsert_flow({"hash": "AAA111", "title": "改名了"})
        ok = check("重复插入返回 False", n2 is False)
        failures += 0 if ok else 1
        row = db.get_flow("AAA111")
        ok = check("更新生效", row["title"] == "改名了")
        failures += 0 if ok else 1
        ok = check("未提供字段不被清空", row["author_uid"] == 1, f"author_uid={row['author_uid']}")
        failures += 0 if ok else 1
        ok = check("is_lottery 初始为 NULL", row["is_lottery"] is None)
        failures += 0 if ok else 1

        print("\n[4] 判定结果写入与查询")
        db.update_lottery("AAA111", is_lottery=1, lottery_status=1, lottery_at="2026-12-01 23:51")
        row = db.get_flow("AAA111")
        ok = check("is_lottery 写入", row["is_lottery"] == 1)
        failures += 0 if ok else 1
        ok = check("复查时间已记录", bool(row["last_checked_at"]))
        failures += 0 if ok else 1
        # 再插入一次，判定结果不应被列表数据覆盖
        db.upsert_flow({"hash": "AAA111", "title": "又改名"})
        row = db.get_flow("AAA111")
        ok = check("列表更新不覆盖判定结果", row["is_lottery"] == 1)
        failures += 0 if ok else 1

        need = db.flows_need_detail()
        ok = check(
            "抽奖帖仍在待复查列表（status=1）",
            any(f["hash"] == "AAA111" for f in need),
            f"共 {len(need)} 条",
        )
        failures += 0 if ok else 1

        db.update_lottery("AAA111", is_lottery=1, lottery_status=2)
        need = db.flows_need_detail()
        ok = check(
            "已开奖帖移出待复查列表",
            not any(f["hash"] == "AAA111" for f in need),
        )
        failures += 0 if ok else 1

        print("\n[5] 参与状态机")
        ok = check("帖子未入库时给出明确错误", _expect_value_error(db, "GHOST9"))
        failures += 0 if ok else 1

        db.ensure_participation("AAA111")
        db.ensure_participation("AAA111")  # 幂等
        p = db.get_participation("AAA111")
        ok = check("pending 记录存在且幂等", p["state"] == "pending")
        failures += 0 if ok else 1

        db.mark_followed("AAA111")
        db.mark_replied("AAA111", reply_id=999, floor=12)
        db.mark_liked("AAA111")
        p = db.get_participation("AAA111")
        ok = check(
            "关注/回复/点赞时间齐全",
            all(p[k] for k in ("followed_at", "replied_at", "liked_at")),
        )
        failures += 0 if ok else 1
        ok = check("reply_id 正确", p["reply_id"] == 999)
        failures += 0 if ok else 1

        db.mark_state("AAA111", STATE_DONE)
        p = db.get_participation("AAA111")
        ok = check("置为 done", p["state"] == STATE_DONE)
        failures += 0 if ok else 1

        try:
            db.mark_state("AAA111", "bogus")
            ok = check("非法状态被拒绝", False)
        except ValueError:
            ok = check("非法状态被拒绝", True)
        failures += 0 if ok else 1

        print("\n[6] 失败重试计数")
        db.upsert_flow(
            {"hash": "BBB222", "created_at": "2026-11-17 11:00:00", "title": "失败测试"}
        )
        db.ensure_participation("BBB222")
        c1 = db.bump_retry("BBB222", "网络错误")
        c2 = db.bump_retry("BBB222", "仍有错误")
        ok = check("重试次数累加", c1 == 1 and c2 == 2, f"{c1}→{c2}")
        failures += 0 if ok else 1
        p = db.get_participation("BBB222")
        ok = check("状态为 failed", p["state"] == STATE_FAILED)
        failures += 0 if ok else 1
        ok = check("失败原因被记录", p["fail_reason"] == "仍有错误")
        failures += 0 if ok else 1

        db.mark_state("BBB222", STATE_GIVE_UP)
        ok = check("可标记 give_up", db.get_participation("BBB222")["state"] == STATE_GIVE_UP)
        failures += 0 if ok else 1

        db.reset_participation("BBB222")
        p = db.get_participation("BBB222")
        ok = check(
            "强制重跑后清空进度",
            p["state"] == "pending" and p["retry_cnt"] == 0 and p["replied_at"] is None,
        )
        failures += 0 if ok else 1

        print("\n[7] 待参与筛选")
        # AAA111: status=2（已开奖）→ 不应出现
        # BBB222: 非抽奖 → 不应出现
        db.upsert_flow({"hash": "CCC333", "created_at": "2026-11-17 12:00:00"})
        db.update_lottery("CCC333", is_lottery=1, lottery_status=1, lottery_at="2026-11-20 10:00:00")
        todo = db.flows_to_participate(max_retry=3)
        hashes = {f["hash"] for f in todo}
        ok = check("有效抽奖帖进入待参与", "CCC333" in hashes)
        failures += 0 if ok else 1
        ok = check("已开奖帖不进入待参与", "AAA111" not in hashes)
        failures += 0 if ok else 1

        db.ensure_participation("CCC333")
        db.mark_state("CCC333", STATE_DONE)
        todo = db.flows_to_participate(max_retry=3)
        ok = check("已完成帖不再重复参与", "CCC333" not in {f["hash"] for f in todo})
        failures += 0 if ok else 1

        print("\n[8] run_logs 与水位")
        rid = db.start_run()
        db.finish_run(rid, scanned_cnt=10, detail_cnt=5, result="success")
        last = db.last_run()
        ok = check("运行记录可读", last["scanned_cnt"] == 10 and last["result"] == "success")
        failures += 0 if ok else 1

        db.set_watermark("2026-11-01 00:00:00")
        ok = check("水位读写", db.get_watermark() == "2026-11-01 00:00:00")
        failures += 0 if ok else 1

        print("\n[9] 面板查询")
        panel = db.list_for_panel()
        ok = check("面板只列抽奖帖", all(r["is_lottery"] == 1 for r in panel), f"{len(panel)} 条")
        failures += 0 if ok else 1

        # 回归：面板跑在独立线程，SQLite 连接不可跨线程共享。
        # 曾出现「SQLite objects created in a thread can only be used in
        # that same thread」，单线程 test client 测不出来。
        print("\n[10] 跨线程访问")
        import threading as _th

        results = {}

        def _worker(name):
            try:
                results[name] = len(db.list_for_panel())
            except Exception as exc:
                results[name] = f"ERR:{exc}"

        threads = [_th.Thread(target=_worker, args=(f"t{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = check(
            "4 个线程并发查询均成功",
            all(isinstance(v, int) for v in results.values()),
            str(results),
        )
        failures += 0 if ok else 1

        db.close()

    print("\n" + "=" * 52)
    if failures == 0:
        print("P0-1 全部通过")
    else:
        print(f"P0-1 有 {failures} 项失败")
    print("=" * 52)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
