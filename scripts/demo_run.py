"""
演示脚本：在真实浏览器里完整走一遍 关注 → 回复 → 点赞。

用途：让人能通过 noVNC 亲眼检查自动化动作是否正确。

与正式运行的区别：
  - **不写数据库**，不影响抽奖参与状态
  - 目标可以是任意帖子（推荐用非抽奖帖，避免污染抽奖记录）
  - 每一关键动作前会停顿，方便目视观察

用法：
  python -m scripts.demo_run <flow_hash> [--no-like] [--no-follow]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

sys.path.insert(0, "/app")

from app import config as cfg_mod
from app.corpus import Corpus
from app.zf.api import Api
from app.zf.driver import Driver, wait_selenium_ready
from app.zf.participant import Participant
from app.zf.session import Session


class DemoDb:
    """空壳数据库：让 Participant 的断点续跑逻辑全部走"未完成"分支。"""

    def get_participation(self, hash_):
        return {}

    def mark_followed(self, hash_):
        pass

    def mark_replied(self, hash_, reply_id, floor):
        pass

    def mark_liked(self, hash_):
        pass

    def mark_state(self, hash_, state, reason=None):
        pass

    def bump_retry(self, hash_, reason):
        return 0

    def mark_failed_no_retry(self, hash_, reason):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hash")
    ap.add_argument("--no-like", action="store_true", help="不点赞")
    ap.add_argument("--no-follow", action="store_true", help="不关注")
    ap.add_argument("--no-reply", action="store_true", help="不回复（只看页面）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = cfg_mod.load()

    if not wait_selenium_ready(cfg.selenium_url, timeout=60):
        print("Selenium 不可达")
        return 1

    d = Driver(cfg.selenium_url)
    d.start()
    try:
        s = Session(d, cfg.cookie)
        user = s.establish()
        print(f"\n>>> 已登录：{user.get('nickname')}（uid={user.get('userId')}）")
        print(f">>> 目标帖子：https://www.zfrontier.com/app/flow/{args.hash}")

        api = Api(d, delay_min=1, delay_max=2)
        info = api.flow_detail(args.hash)
        flow = info.get("flow") or {}
        fid = flow.get("id")
        # 作者 hash 必须走 extract_flow_meta：
        # flow.user 里没有 hash_id 字段，只有 view_url，
        # 直接取 hash_id 会得到 None 导致关注失败。
        from app.zf.judge import extract_flow_meta, parse_lottery

        lot = parse_lottery(flow)
        lot.raw = {"lottery": flow.get("lottery") or {}, "flow": flow}
        meta = extract_flow_meta(lot)
        author_hash = meta.get("author_hash")
        author_uid = meta.get("author_uid")

        print(f">>> flow_id={fid} | 标题：{(flow.get('title') or '')[:36]}")
        print(f">>> 作者：{meta.get('author_nick')} | uid={author_uid} | hash={author_hash}")

        p = Participant(
            driver=d,
            api=api,
            db=DemoDb(),
            corpus=Corpus(cfg.reply_corpus),
            my_uid=user.get("userId") or 0,
            max_retry=1,
            write_budget=10,
            delay_min=2,
            delay_max=4,
        )

        # 1) 打开帖子页
        print("\n========== 步骤 1/3：打开帖子页 ==========")
        p._open_flow(args.hash)
        print(">>> 页面已就绪，请看 noVNC 画面")
        time.sleep(4)

        # 2) 关注
        if not args.no_follow:
            print("\n========== 步骤 2/3：关注作者 ==========")
            print(">>> 即将点击作者名 → 进主页 → 点关注")
            time.sleep(3)
            try:
                p.ensure_followed(author_hash, int(author_uid or 0))
                print(">>> 关注完成")
            except Exception as exc:
                print(f">>> 关注失败：{exc}")
            p._open_flow(args.hash)
            time.sleep(3)

        # 3) 回复
        if not args.no_reply:
            print("\n========== 步骤 3/3：发送回复 ==========")
            print(">>> 即将点击输入框 → 打字 → 点发送")
            time.sleep(3)
            try:
                info = p.send_reply(fid, args.hash)
                print(f">>> 回复成功：floor={info.get('floor')} 内容={info.get('content')!r}")
            except Exception as exc:
                print(f">>> 回复失败：{exc}")
            time.sleep(3)

        # 4) 点赞
        if not args.no_like:
            print("\n========== 附加：点赞 ==========")
            print(">>> 即将点击点赞图标")
            time.sleep(3)
            try:
                p.like_flow(fid, args.hash)
                print(">>> 点赞完成")
            except Exception as exc:
                print(f">>> 点赞失败：{exc}")

        print("\n>>> 演示结束，浏览器保持打开 20 秒供观察")
        time.sleep(20)
        return 0
    finally:
        d.quit()


if __name__ == "__main__":
    sys.exit(main())
