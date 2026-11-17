"""
SQLite 数据层。

四张表（设计见 REQUIREMENTS.md §7）：
  flows          见过的所有帖子，含抽奖判定结果
  participations 每条帖子的参与进度与结果
  run_logs       每轮运行记录
  kv             键值（覆盖率水位等）

设计要点：
  1. 判定结果永久缓存：is_lottery 一旦写入就不再重复查询，除非需要复查；
  2. 参与状态与帖子信息分表：即使换账号，flows 的历史判定依然有效；
  3. 所有写入用参数化，杜绝拼接；
  4. 用 WAL 模式提升并发读性能（面板读、主进程写）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    hash            TEXT PRIMARY KEY,
    flow_id         INTEGER,
    title           TEXT,
    created_at      TEXT,
    author_uid      INTEGER,
    author_hash     TEXT,
    author_nick     TEXT,
    is_lottery      INTEGER,      -- NULL=未判定 0=非抽奖 1=抽奖
    lottery_status  INTEGER,      -- 1=待抽奖
    lottery_at      TEXT,
    prizes          TEXT,         -- JSON 摘要
    needs_follow    INTEGER DEFAULT 0,
    first_seen_at   TEXT,
    last_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_flows_created  ON flows(created_at);
CREATE INDEX IF NOT EXISTS idx_flows_lottery  ON flows(is_lottery);
CREATE INDEX IF NOT EXISTS idx_flows_status   ON flows(lottery_status);

CREATE TABLE IF NOT EXISTS participations (
    hash         TEXT PRIMARY KEY,
    followed_at  TEXT,
    replied_at   TEXT,
    reply_id     INTEGER,
    reply_floor  INTEGER,
    liked_at     TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',
    retry_cnt    INTEGER NOT NULL DEFAULT 0,
    fail_reason  TEXT,
    updated_at   TEXT,
    FOREIGN KEY (hash) REFERENCES flows(hash)
);

CREATE INDEX IF NOT EXISTS idx_part_state ON participations(state);

CREATE TABLE IF NOT EXISTS run_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT,
    finished_at      TEXT,
    scanned_cnt      INTEGER DEFAULT 0,
    detail_cnt       INTEGER DEFAULT 0,
    participated_cnt INTEGER DEFAULT 0,
    failed_cnt       INTEGER DEFAULT 0,
    result           TEXT,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

# 参与状态
STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_GIVE_UP = "give_up"

VALID_STATES = (STATE_PENDING, STATE_DONE, STATE_FAILED, STATE_GIVE_UP)


def now_str() -> str:
    """统一的时间字符串（秒级），与本项目所有时间字段一致。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """SQLite 封装。线程内使用，不做连接池。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- 生命周期 ----------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, timeout=30)
            self._conn.row_factory = sqlite3.Row
            # WAL：面板读与主进程写不互相阻塞
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            log.info("数据库就绪：%s", self.path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _cursor(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # ---------- flows ----------

    def upsert_flow(self, flow: Dict[str, Any]) -> bool:
        """
        写入/更新一条帖子。

        :return: True 表示新插入，False 表示已存在（只更新可变字段）
        """
        hash_ = flow["hash"]
        row = self.get_flow(hash_)
        is_new = row is None

        if is_new:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO flows (
                        hash, flow_id, title, created_at,
                        author_uid, author_hash, author_nick,
                        first_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        hash_,
                        flow.get("flow_id"),
                        flow.get("title"),
                        flow.get("created_at"),
                        flow.get("author_uid"),
                        flow.get("author_hash"),
                        flow.get("author_nick"),
                        now_str(),
                    ),
                )
        else:
            # 只更新列表能提供的字段，不碰判定结果（那由判定阶段负责）
            with self._cursor() as cur:
                cur.execute(
                    """
                    UPDATE flows SET
                        flow_id     = COALESCE(?, flow_id),
                        title       = COALESCE(?, title),
                        created_at  = COALESCE(?, created_at),
                        author_uid  = COALESCE(?, author_uid),
                        author_hash = COALESCE(?, author_hash),
                        author_nick = COALESCE(?, author_nick)
                    WHERE hash = ?
                    """,
                    (
                        flow.get("flow_id"),
                        flow.get("title"),
                        flow.get("created_at"),
                        flow.get("author_uid"),
                        flow.get("author_hash"),
                        flow.get("author_nick"),
                        hash_,
                    ),
                )
        return is_new

    def get_flow(self, hash_: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM flows WHERE hash = ?", (hash_,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_lottery(
        self,
        hash_: str,
        is_lottery: int,
        lottery_status: Optional[int] = None,
        lottery_at: Optional[str] = None,
        prizes: Optional[str] = None,
        needs_follow: int = 0,
    ) -> None:
        """写入判定结果。这是唯一会改 is_lottery 的入口。"""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE flows SET
                    is_lottery      = ?,
                    lottery_status  = ?,
                    lottery_at      = ?,
                    prizes          = ?,
                    needs_follow    = ?,
                    last_checked_at = ?
                WHERE hash = ?
                """,
                (
                    is_lottery,
                    lottery_status,
                    lottery_at,
                    prizes,
                    needs_follow,
                    now_str(),
                    hash_,
                ),
            )

    def touch_checked(self, hash_: str) -> None:
        """只更新检查时间（用于复查 status 但不改判定）。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE flows SET last_checked_at = ? WHERE hash = ?",
                (now_str(), hash_),
            )

    def flows_need_detail(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        需要查详情的帖子：
          - 从未判定过（is_lottery IS NULL）
          - 或已判定为抽奖但仍在抽奖期（status=1），需复查是否已开奖
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM flows
                WHERE is_lottery IS NULL
                   OR (is_lottery = 1 AND lottery_status = 1)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def flows_to_participate(self, max_retry: int) -> List[Dict[str, Any]]:
        """
        待参与的帖子：有效抽奖帖 且 尚无完成记录 且 未放弃。

        失败但未超重试次数的会被返回（自动重试）。
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT f.*, p.state AS part_state, p.retry_cnt
                FROM flows f
                LEFT JOIN participations p ON f.hash = p.hash
                WHERE f.is_lottery = 1
                  AND f.lottery_status = 1
                  AND (
                        p.state IS NULL
                     OR p.state = 'pending'
                     OR (p.state = 'failed' AND p.retry_cnt < ?)
                  )
                ORDER BY f.lottery_at ASC
                """,
                (max_retry,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ---------- participations ----------

    def get_participation(self, hash_: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM participations WHERE hash = ?", (hash_,))
            row = cur.fetchone()
            return dict(row) if row else None

    def ensure_participation(self, hash_: str) -> None:
        """
        确保存在一条 pending 记录（幂等）。

        依赖 flows 中已有该 hash（participations 有外键约束）。
        若帖子尚未入库，这里给出明确错误——静默吞掉会让后续
        mark_* 系列全部无声失效，比直接报错难查得多。
        """
        if self.get_flow(hash_) is None:
            raise ValueError(
                f"帖子 {hash_} 尚未写入 flows 表，无法创建参与记录"
                "（应先 upsert_flow）"
            )
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO participations (hash, state, retry_cnt, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(hash) DO NOTHING
                """,
                (hash_, STATE_PENDING, now_str()),
            )

    def mark_followed(self, hash_: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE participations SET followed_at = ?, updated_at = ? WHERE hash = ?",
                (now_str(), now_str(), hash_),
            )

    def mark_replied(self, hash_: str, reply_id: Optional[int], floor: Optional[int]) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE participations
                SET replied_at = ?, reply_id = ?, reply_floor = ?, updated_at = ?
                WHERE hash = ?
                """,
                (now_str(), reply_id, floor, now_str(), hash_),
            )

    def mark_liked(self, hash_: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE participations SET liked_at = ?, updated_at = ? WHERE hash = ?",
                (now_str(), now_str(), hash_),
            )

    def mark_state(
        self, hash_: str, state: str, fail_reason: Optional[str] = None
    ) -> None:
        if state not in VALID_STATES:
            raise ValueError(f"非法状态：{state}")
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE participations
                SET state = ?, fail_reason = ?, updated_at = ?
                WHERE hash = ?
                """,
                (state, fail_reason, now_str(), hash_),
            )

    def bump_retry(self, hash_: str, fail_reason: Optional[str]) -> int:
        """
        失败时累加重试次数，返回累加后的次数。

        达到上限后由调用方决定是否置为 give_up。
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE participations
                SET retry_cnt = retry_cnt + 1,
                    state = 'failed',
                    fail_reason = ?,
                    updated_at = ?
                WHERE hash = ?
                """,
                (fail_reason, now_str(), hash_),
            )
            cur.execute(
                "SELECT retry_cnt FROM participations WHERE hash = ?", (hash_,)
            )
            row = cur.fetchone()
            return int(row["retry_cnt"]) if row else 0

    def reset_participation(self, hash_: str) -> None:
        """强制重跑：清空参与进度，回到 pending。"""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE participations SET
                    state = 'pending',
                    retry_cnt = 0,
                    fail_reason = NULL,
                    followed_at = NULL,
                    replied_at = NULL,
                    reply_id = NULL,
                    reply_floor = NULL,
                    liked_at = NULL,
                    updated_at = ?
                WHERE hash = ?
                """,
                (now_str(), hash_),
            )

    # ---------- run_logs ----------

    def start_run(self) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO run_logs (started_at, result) VALUES (?, 'running')",
                (now_str(),),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        scanned_cnt: int = 0,
        detail_cnt: int = 0,
        participated_cnt: int = 0,
        failed_cnt: int = 0,
        result: str = "success",
        note: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE run_logs SET
                    finished_at = ?, scanned_cnt = ?, detail_cnt = ?,
                    participated_cnt = ?, failed_cnt = ?, result = ?, note = ?
                WHERE id = ?
                """,
                (
                    now_str(),
                    scanned_cnt,
                    detail_cnt,
                    participated_cnt,
                    failed_cnt,
                    result,
                    note,
                    run_id,
                ),
            )

    def last_run(self) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None

    # ---------- kv ----------

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, value, now_str()),
            )

    def get_watermark(self) -> Optional[str]:
        """覆盖率水位：上一轮成功扫描到的最早 created_at。"""
        return self.get_kv("coverage_watermark")

    def set_watermark(self, value: str) -> None:
        self.set_kv("coverage_watermark", value)

    # ---------- 面板查询 ----------

    def list_for_panel(self, limit: int = 200) -> List[Dict[str, Any]]:
        """面板用的联表视图。"""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT f.hash, f.title, f.created_at, f.author_nick,
                       f.is_lottery, f.lottery_status, f.lottery_at,
                       f.prizes, f.needs_follow,
                       p.state, p.retry_cnt, p.fail_reason,
                       p.followed_at, p.replied_at, p.liked_at, p.updated_at
                FROM flows f
                LEFT JOIN participations p ON f.hash = p.hash
                WHERE f.is_lottery = 1
                ORDER BY f.lottery_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
