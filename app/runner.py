"""
主流程编排。

一轮完整运行：
  1. 前置检查（Selenium、登录态、发帖权限）
  2. 扫描情报专栏（按时间边界停止）
  3. 落库（新帖入库，老帖跳过）
  4. 逐帖判定抽奖（只查未判定/待复查的）
  5. 逐帖参与（有效抽奖 且 未完成）
  6. 更新覆盖率水位

中止条件：限流、验证码、登录失效、写预算耗尽——
这四种情况继续跑只会更糟，必须整体停下让用户介入。
单帖的普通失败不中止，只记录，下一轮重试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .corpus import Corpus
from .db import Database
from .zf.api import Api, ApiError, CaptchaRequired, RateLimited, SessionExpired
from .zf.driver import Driver, DriverError, wait_selenium_ready
from .zf.judge import DetailChecker, judge_pending
from .zf.participant import Participant
from .zf.scanner import ScannedFlow, Scanner, compute_stop_boundary
from .zf.session import Session, SessionError

log = logging.getLogger(__name__)

# 运行结果
RESULT_SUCCESS = "success"
RESULT_ABORTED = "aborted"
RESULT_CAPTCHA = "captcha"
RESULT_RATE_LIMITED = "rate_limited"
RESULT_LOGIN_INVALID = "login_invalid"


@dataclass
class RunSummary:
    """一轮运行的结果汇总。"""

    result: str = RESULT_SUCCESS
    scanned: int = 0
    detail_checked: int = 0
    participated: int = 0
    failed: int = 0
    note: str = ""
    errors: List[Dict[str, str]] = field(default_factory=list)

    def to_note(self) -> str:
        parts = [f"扫描{self.scanned}", f"判定{self.detail_checked}",
                 f"参与{self.participated}", f"失败{self.failed}"]
        text = " ".join(parts)
        if self.note:
            text += f" | {self.note}"
        return text


class Runner:
    """一轮完整运行的执行者。"""

    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.driver: Optional[Driver] = None

    # ---------- 前置检查 ----------

    def _preflight(self) -> Session:
        """
        检查依赖与登录态。任一项失败即抛异常，不进入业务逻辑。

        放在最前面，避免扫了一堆页面才发现没登录。
        """
        if not wait_selenium_ready(self.cfg.selenium_url, timeout=60):
            raise DriverError(
                f"Selenium 不可达：{self.cfg.selenium_url}。"
                "请确认容器在运行且与本服务在同一 docker 网络。"
            )

        self.driver = Driver(self.cfg.selenium_url)
        self.driver.start()

        session = Session(self.driver, self.cfg.cookie)
        session.establish()

        # 检查是否允许回复（站点要求绑手机号）
        rules = session.check_publish_rule()
        if rules is not None:
            identity_pass = rules.get("identity_pass")
            if identity_pass is not None and int(identity_pass) == 0:
                raise SessionError(
                    "账号未绑定手机号（identity_pass=0），站点会拒绝回复。"
                    "请先在 zFrontier 绑定手机号后再运行。"
                )
            log.info("发帖权限检查通过（identity_pass=%s）", identity_pass)
        else:
            log.warning("未能读取 publish/rules，跳过发帖权限检查")

        return session

    # ---------- 各阶段 ----------

    def _scan(self, now: datetime) -> List[ScannedFlow]:
        watermark_str = self.db.get_watermark()
        watermark = None
        if watermark_str:
            try:
                watermark = datetime.strptime(watermark_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                log.warning("水位格式非法，忽略：%r", watermark_str)

        boundary = compute_stop_boundary(
            now,
            self.cfg.scan_window_days,
            self.cfg.scan_buffer_days,
            watermark,
        )
        log.info(
            "扫描边界 %s（窗口 %d 天 + 边距 %d 天，水位 %s）",
            boundary.strftime("%Y-%m-%d %H:%M"),
            self.cfg.scan_window_days,
            self.cfg.scan_buffer_days,
            watermark_str or "无（首次运行）",
        )

        scanner = Scanner(
            self.driver,
            tag_id=self.cfg.TAGS_INFO,
            confirm_count=self.cfg.scan_confirm_count,
            max_scroll=self.cfg.scan_max_scroll,
            max_items=self.cfg.scan_max_items,
        )
        return scanner.scan(boundary)

    def _persist(self, flows: List[ScannedFlow]) -> int:
        new_count = 0
        for flow in flows:
            if self.db.upsert_flow(flow.to_db()):
                new_count += 1
        log.info("落库完成：%d 条，其中新增 %d 条", len(flows), new_count)
        return new_count

    def _participate(self, session: Session, summary: RunSummary) -> Optional[str]:
        """
        逐帖参与。返回中止原因（None 表示正常完成）。
        """
        todos = self.db.flows_to_participate(max_retry=self.cfg.max_retry)
        if not todos:
            log.info("没有待参与的抽奖帖")
            return None

        log.info("待参与：%d 条", len(todos))
        participant = Participant(
            api=Api(
                self.driver,
                delay_min=self.cfg.request_delay_min,
                delay_max=self.cfg.request_delay_max,
            ),
            db=self.db,
            corpus=Corpus(self.cfg.reply_corpus),
            my_uid=session.uid or 0,
            max_retry=self.cfg.max_retry,
            write_budget=self.cfg.max_write_per_run,
        )

        for idx, row in enumerate(todos, 1):
            log.info(
                "— 第 %d/%d 帖：%s | %s",
                idx, len(todos), row["hash"], (row.get("title") or "")[:28],
            )
            res = participant.participate(row)

            if res.success:
                summary.participated += 1
            elif res.aborted:
                # 中止不是「这帖失败」，不累加 failed
                summary.note = res.aborted
                return res.aborted
            else:
                summary.failed += 1
                summary.errors.append({"hash": res.hash, "error": res.fail_reason or ""})

        return None

    # ---------- 主入口 ----------

    def run(self) -> RunSummary:
        summary = RunSummary()
        run_id = self.db.start_run()
        now = datetime.now()
        log.info("=" * 56)
        log.info("开始运行 #%d（%s）", run_id, now.strftime("%Y-%m-%d %H:%M:%S"))
        log.info("=" * 56)

        session: Optional[Session] = None
        try:
            # 1. 前置检查
            session = self._preflight()

            # 2. 扫描
            flows = self._scan(now)
            summary.scanned = len(flows)
            if not flows:
                summary.note = "未扫描到任何帖子"
                log.warning(summary.note)
                return summary

            # 3. 落库
            self._persist(flows)

            # 4. 判定
            checker = DetailChecker(
                Api(
                    self.driver,
                    delay_min=self.cfg.request_delay_min,
                    delay_max=self.cfg.request_delay_max,
                )
            )
            judge_res = judge_pending(self.db, checker)
            summary.detail_checked = judge_res.checked
            log.info(
                "判定完成：查 %d 条，抽奖 %d 条，有效 %d 条",
                judge_res.checked, judge_res.lottery_found, judge_res.active_found,
            )
            if judge_res.aborted:
                summary.note = f"judge {judge_res.aborted}"
                summary.result = self._result_from_abort(judge_res.aborted)
                return summary

            # 5. 参与
            abort = self._participate(session, summary)
            if abort:
                summary.result = self._result_from_abort(abort)

            # 6. 更新水位（只在正常走完时更新，避免中止时缩小覆盖范围）
            if summary.result == RESULT_SUCCESS and flows:
                oldest = min(f.created_at for f in flows)
                self.db.set_watermark(oldest.strftime("%Y-%m-%d %H:%M:%S"))
                log.info("水位更新为 %s", oldest.strftime("%Y-%m-%d %H:%M"))

            return summary

        except CaptchaRequired as exc:
            summary.result = RESULT_CAPTCHA
            summary.note = str(exc)
            log.error("出现验证码，本轮中止：%s", exc)
            return summary

        except RateLimited as exc:
            summary.result = RESULT_RATE_LIMITED
            summary.note = str(exc)
            log.error("被限流，本轮中止：%s", exc)
            return summary

        except SessionError as exc:
            summary.result = RESULT_LOGIN_INVALID
            summary.note = str(exc)
            log.error("登录态不可用，本轮中止：%s", exc)
            return summary

        except (DriverError, ApiError) as exc:
            summary.result = RESULT_ABORTED
            summary.note = str(exc)
            log.error("运行中止：%s", exc)
            return summary

        except Exception as exc:
            summary.result = RESULT_ABORTED
            summary.note = f"{type(exc).__name__}: {exc}"
            log.exception("运行出现未预期错误")
            return summary

        finally:
            self.db.finish_run(
                run_id,
                scanned_cnt=summary.scanned,
                detail_cnt=summary.detail_checked,
                participated_cnt=summary.participated,
                failed_cnt=summary.failed,
                result=summary.result,
                note=summary.to_note(),
            )
            if self.driver is not None:
                self.driver.quit()
                self.driver = None
            log.info(
                "运行结束：%s | %s",
                summary.result,
                summary.to_note(),
            )
            log.info("=" * 56)

    @staticmethod
    def _result_from_abort(abort: str) -> str:
        if abort.startswith("captcha"):
            return RESULT_CAPTCHA
        if abort.startswith("rate_limited"):
            return RESULT_RATE_LIMITED
        if abort.startswith("session_expired"):
            return RESULT_LOGIN_INVALID
        return RESULT_ABORTED
