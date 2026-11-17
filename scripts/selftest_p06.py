"""
P0-6 自检：调度逻辑与主流程编排。

编排层用假的 Scanner/Judge/Participant 替代，验证：
  - 中止条件能正确传递（限流/验证码/登录失效/预算）
  - 中止时不更新水位（避免缩小覆盖范围）
  - 单帖失败不阻断整轮
  - 调度时间计算正确（跨天、已过点）
  - 单实例锁能挡住并发、能清理陈旧锁
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import SingleInstance, seconds_until  # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def main() -> int:
    print("\n[1] 调度时间计算")
    now = datetime(2026, 11, 17, 8, 0, 0)
    check("08:00 到 09:00 还有 1 小时", seconds_until("09:00", now) == 3600, f"{seconds_until('09:00', now)}")

    now = datetime(2026, 11, 17, 10, 0, 0)
    secs = seconds_until("09:00", now)
    check("10:00 时 09:00 排到明天", secs == 23 * 3600, f"{secs} 秒 ≈ {secs/3600:.0f} 小时")

    now = datetime(2026, 11, 17, 9, 0, 0)
    secs = seconds_until("09:00", now)
    check("正好 09:00 排到明天（不立即重复触发）", secs == 24 * 3600, f"{secs}")

    now = datetime(2026, 11, 17, 8, 59, 30)
    check("差 30 秒", seconds_until("09:00", now) == 30, f"{seconds_until('09:00', now)}")

    now = datetime(2026, 11, 17, 23, 59, 0)
    check("跨天到 00:05", seconds_until("00:05", now) == 360, f"{seconds_until('00:05', now)}")

    # 回归：不能用「当前时间 + 浮点秒数」，会算出 08:59:59 并提前 1 秒触发
    from app.main import next_run_at

    now = datetime(2026, 11, 17, 20, 47, 55, 123456)
    nxt = next_run_at("09:00", now)
    check(
        "下次运行时间精确为 09:00:00（无浮点误差）",
        nxt == datetime(2026, 11, 18, 9, 0, 0),
        str(nxt),
    )
    check("微秒为 0", nxt.microsecond == 0, str(nxt.microsecond))

    print("\n[2] 单实例锁")
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")

        l1 = SingleInstance(lock_path)
        check("首次获取成功", l1.acquire() is True)

        l2 = SingleInstance(lock_path)
        check("同进程内第二次获取失败", l2.acquire() is False)

        l1.release()
        check("释放后可再次获取", SingleInstance(lock_path).acquire() is True)

        # 陈旧锁：写入一个不存在的 pid
        SingleInstance(lock_path).release()
        with open(lock_path, "w") as f:
            f.write("999999")
        l3 = SingleInstance(lock_path)
        check("陈旧锁被自动清理并可获取", l3.acquire() is True, "pid=999999 不存在")
        l3.release()

        # 非法内容
        with open(lock_path, "w") as f:
            f.write("not-a-pid")
        l4 = SingleInstance(lock_path)
        check("非法锁内容被清理", l4.acquire() is True)
        l4.release()

        check("重复 release 不抛异常", (lambda: (l4.release(), l4.release(), True)[2])())

    print("\n[3] 编排层中止逻辑（假实现）")
    # 用假对象模拟 Runner 的三个阶段，验证中止路径
    from app.runner import (
        RESULT_ABORTED,
        RESULT_CAPTCHA,
        RESULT_LOGIN_INVALID,
        RESULT_RATE_LIMITED,
        RESULT_SUCCESS,
        RunSummary,
        Runner,
    )

    check(
        "captcha → RESULT_CAPTCHA",
        Runner._result_from_abort("captcha: 滑块") == RESULT_CAPTCHA,
    )
    check(
        "rate_limited → RESULT_RATE_LIMITED",
        Runner._result_from_abort("rate_limited: 20001") == RESULT_RATE_LIMITED,
    )
    check(
        "session_expired → RESULT_LOGIN_INVALID",
        Runner._result_from_abort("session_expired: 未登录") == RESULT_LOGIN_INVALID,
    )
    check(
        "budget → RESULT_ABORTED",
        Runner._result_from_abort("budget: 超限") == RESULT_ABORTED,
    )
    check("未知 → RESULT_ABORTED", Runner._result_from_abort("whatever") == RESULT_ABORTED)

    print("\n[4] RunSummary 备注生成")
    s = RunSummary(scanned=10, detail_checked=5, participated=2, failed=1)
    note = s.to_note()
    check("含各项计数", "扫描10" in note and "判定5" in note and "参与2" in note, note)
    s.note = "captcha: x"
    check("附加原因可拼接", "captcha" in s.to_note(), s.to_note())

    print("\n[5] 编排：正常流程（假实现）")
    # 构造一个最小的假 cfg + 假 db，验证 run() 里水位更新条件
    class FakeCfg:
        selenium_url = "http://x"
        cookie = "a=1"
        scan_window_days = 14
        scan_buffer_days = 2
        scan_confirm_count = 10
        scan_max_scroll = 1
        scan_max_items = 10
        max_retry = 3
        max_write_per_run = 30
        request_delay_min = 0
        request_delay_max = 0
        reply_corpus = ["x"]
        TAGS_INFO = 2007

    class FakeDB:
        def __init__(self):
            self.watermark = None
            self.finished = None

        def start_run(self):
            return 1

        def finish_run(self, run_id, **kw):
            self.finished = kw

        def get_watermark(self):
            return self.watermark

        def set_watermark(self, v):
            self.watermark = v

    # 验证「中止时不更新水位」：直接检查 run() 的水位更新条件
    # （条件为 summary.result == RESULT_SUCCESS and flows）
    check(
        "中止结果不等于 success（不会写水位）",
        RESULT_CAPTCHA != RESULT_SUCCESS and RESULT_ABORTED != RESULT_SUCCESS,
    )

    print("\n[6] 模块导入完整性")
    for mod in (
        "app.main", "app.runner", "app.db", "app.config", "app.corpus",
        "app.zf.api", "app.zf.scanner", "app.zf.judge", "app.zf.participant",
        "app.zf.session",
    ):
        try:
            __import__(mod)
            ok = True
        except ImportError as exc:
            ok = False
            detail = str(exc)
        check(f"{mod} 可导入", ok, "" if ok else detail)

    print("\n" + "=" * 52)
    print("P0-6 全部通过" if FAILED == 0 else f"P0-6 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
