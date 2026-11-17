"""
P1 自检：Web 面板。

用 Flask test client 离线验证，不启动真实服务器、不依赖 selenium。
重点验证：
  - 数据整形（状态文案、时间裁剪、奖品拼接）
  - 空库/异常数据不白屏
  - 触发文件与暂停文件的语义
  - 强制重跑会重置参与记录
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def main() -> int:
    try:
        from app.web import panel
    except ImportError as exc:
        print(f"缺少依赖（flask）：{exc}")
        print("请在容器内运行：docker exec zf-lucky python -m scripts.selftest_p1")
        return 1

    print("\n[1] 状态文案与时间格式化")
    check("done → 已完成", panel.STATE_LABEL["done"][0] == "已完成")
    check("failed → 失败", panel.STATE_LABEL["failed"][0] == "失败")
    check("give_up → 已放弃", panel.STATE_LABEL["give_up"][0] == "已放弃")
    check("None → 未参与", panel.STATE_LABEL[None][0] == "未参与")

    check("时间裁剪到 MM-DD HH:MM", panel._fmt_time("2026-11-17 12:47:25") == "11-17 12:47",
          panel._fmt_time("2026-11-17 12:47:25"))
    check("空值返回空串", panel._fmt_time(None) == "")
    check("短时间不越界", panel._fmt_time("2026-11-17") == "2026-11-17")

    check("奖品 JSON 解析", len(panel._safe_json('[{"name":"a"}]')) == 1)
    check("非法 JSON 返回空", panel._safe_json("not json") == [])
    check("空串返回空", panel._safe_json("") == [])

    print("\n[2] 空库渲染")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "p.db")
        panel.init(db_path, None)
        panel.TRIGGER_FILE = os.path.join(tmp, "trigger")
        panel.PAUSE_FILE = os.path.join(tmp, "paused")

        view = panel.build_view()
        check("空库 total=0", view["total"] == 0)
        check("空库无告警", view["alerts"] == [])
        check("空库未暂停", view["paused"] is False)

        client = panel.app.test_client()

        r = client.get("/")
        check("首页 200", r.status_code == 200, str(r.status_code))
        body = r.get_data(as_text=True)
        check("空状态提示可见", "还没有记录" in body)
        check("未配置 Cookie 有提示", "ZF_COOKIE" in body)

        r = client.get("/healthz")
        check("healthz 200", r.status_code == 200)

        r = client.get("/api/status")
        check("api/status 200", r.status_code == 200)
        check("api/status 返回 JSON", r.get_json().get("total") == 0)

        print("\n[3] 有数据时的渲染")
        db = panel._get_db()
        db.upsert_flow({
            "hash": "AAA", "flow_id": 1, "title": "抽奖帖A",
            "created_at": "2026-11-17 10:00:00",
            "author_uid": 9, "author_hash": "U9", "author_nick": "作者A",
        })
        db.update_lottery("AAA", is_lottery=1, lottery_status=1,
                          lottery_at="2026-12-01 23:51",
                          prizes='[{"name":"Z1轴60颗","cnt":6}]',
                          needs_follow=1)
        db.ensure_participation("AAA")
        db.mark_followed("AAA")
        db.mark_replied("AAA", 11, 2)
        db.mark_liked("AAA")
        db.mark_state("AAA", "done")

        db.upsert_flow({
            "hash": "BBB", "flow_id": 2, "title": "抽奖帖B",
            "created_at": "2026-11-16 10:00:00",
            "author_uid": 8, "author_hash": "U8", "author_nick": "作者B",
        })
        db.update_lottery("BBB", is_lottery=1, lottery_status=2,
                          lottery_at="2026-11-17 10:00:00")
        db.ensure_participation("BBB")

        db.upsert_flow({
            "hash": "CCC", "flow_id": 3, "title": "抽奖帖C",
            "created_at": "2026-11-15 10:00:00",
            "author_uid": 7, "author_hash": "U7", "author_nick": "作者C",
        })
        db.update_lottery("CCC", is_lottery=1, lottery_status=1,
                          lottery_at="2026-11-30 10:00:00")
        db.ensure_participation("CCC")
        db.bump_retry("CCC", "回读失败")

        view = panel.build_view()
        check("统计：1 完成", view["counts"]["done"] == 1, str(view["counts"]))
        check("统计：1 失败", view["counts"]["failed"] == 1, str(view["counts"]))

        by_hash = {i["hash"]: i for i in view["items"]}
        check("已完成状态文案", by_hash["AAA"]["state_label"] == "已完成")
        check("已完成记录三步时间",
              bool(by_hash["AAA"]["followed_at"]) and bool(by_hash["AAA"]["replied_at"])
              and bool(by_hash["AAA"]["liked_at"]))
        check("奖品拼接可读", "Z1轴60颗" in by_hash["AAA"]["prizes"], by_hash["AAA"]["prizes"])
        check("需关注标记", by_hash["AAA"]["needs_follow"] is True)
        check("已开奖帖标记 expired", by_hash["BBB"]["expired"] is True)
        check("已开奖且未完成 → 显示已开奖", by_hash["BBB"]["state_label"] == "已开奖",
              by_hash["BBB"]["state_label"])
        check("失败原因可见", "回读失败" in by_hash["CCC"]["state_note"])
        check("重试次数可见", by_hash["CCC"]["retry"] == 1)

        r = client.get("/")
        body = r.get_data(as_text=True)
        check("帖子标题渲染", "抽奖帖A" in body)
        check("外部链接正确", "app/flow/AAA" in body)

        print("\n[4] 触发与暂停")
        r = client.post("/api/run", json={})
        check("触发成功", r.status_code == 200 and r.get_json()["ok"] is True)
        check("触发文件已创建", os.path.exists(panel.TRIGGER_FILE))

        r = client.post("/api/pause", json={"enable": True})
        check("暂停成功", r.get_json()["paused"] is True)
        check("暂停文件已创建", os.path.exists(panel.PAUSE_FILE))

        view = panel.build_view()
        check("暂停后 view.paused=True", view["paused"] is True)
        check("暂停有告警", any("暂停" in a for a in view["alerts"]), str(view["alerts"]))

        os.unlink(panel.TRIGGER_FILE)
        r = client.post("/api/run", json={})
        check("暂停时拒绝触发", r.status_code == 409, str(r.status_code))

        r = client.post("/api/pause", json={"enable": False})
        check("恢复成功", r.get_json()["paused"] is False)
        check("暂停文件已删除", not os.path.exists(panel.PAUSE_FILE))

        print("\n[5] 强制重跑")
        r = client.post("/api/reset/CCC", json={})
        check("重跑成功", r.get_json()["ok"] is True)
        row = db.get_participation("CCC")
        check("状态回到 pending", row["state"] == "pending", row["state"])
        check("重试次数清零", row["retry_cnt"] == 0)
        check("失败原因清空", row["fail_reason"] is None)

        r = client.post("/api/reset/NOTEXIST", json={})
        check("不存在的帖返回 404", r.status_code == 404, str(r.status_code))

        print("\n[6] 告警来源")
        db.start_run()
        db.finish_run(1, result="captcha", note="captcha: 滑块")
        view = panel.build_view()
        check("验证码告警出现", any("验证码" in a for a in view["alerts"]), str(view["alerts"]))

        db.start_run()
        db.finish_run(2, result="login_invalid", note="会话失效")
        view = panel.build_view()
        check("登录失效告警出现", any("登录态" in a for a in view["alerts"]), str(view["alerts"]))

        db.close()

    print("\n" + "=" * 52)
    print("P1 全部通过" if FAILED == 0 else f"P1 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
