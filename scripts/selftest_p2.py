"""
P2 自检：VNC 网关 + 登录会话 + .env 安全写入。

全部离线可跑（不依赖 selenium），重点验证：
  - .env 原子写入、只改目标键、保留注释、备份、权限
  - VNC 网关的状态查询与上游不可达时的处理
  - 登录会话未启动时的状态语义
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web import envfile  # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


ORIGINAL = """# zf_lucky 配置
ZF_COOKIE=
RUN_AT=09:00
# 下面是注释
SCAN_WINDOW_DAYS=14
"""


def main() -> int:
    print("\n[1] .env 写入：替换已有键")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, ".env")
        open(p, "w").write(ORIGINAL)

        ok, msg = envfile.update_env_var(p, "ZF_COOKIE", "a=1; b=2")
        check("写入成功", ok, msg)
        text = open(p).read()
        check("目标键已更新", "ZF_COOKIE=a=1; b=2" in text)
        check("注释保留", "# 下面是注释" in text)
        check("其他键不变", "RUN_AT=09:00" in text and "SCAN_WINDOW_DAYS=14" in text)
        check("首行注释保留", "# zf_lucky 配置" in text)

        print("\n[2] 幂等与备份")
        before = len(os.listdir(tmp))
        ok, msg = envfile.update_env_var(p, "ZF_COOKIE", "a=1; b=2")
        check("同值写入不报错", ok)
        check("同值不重复写", "值未变化" in msg, msg)
        check("同值不产生备份", len(os.listdir(tmp)) == before, str(os.listdir(tmp)))

        ok, _ = envfile.update_env_var(p, "ZF_COOKIE", "c=3")
        baks = [f for f in os.listdir(tmp) if f.startswith(".env.bak.")]
        check("异值产生备份", len(baks) == 1, str(baks))
        check("新值生效", "ZF_COOKIE=c=3" in open(p).read())

        print("\n[3] 新增不存在的键")
        ok, _ = envfile.update_env_var(p, "NEW_KEY", "hello")
        text = open(p).read()
        check("新键追加成功", "NEW_KEY=hello" in text)
        check("仍保留其他内容", "RUN_AT=09:00" in text)

        print("\n[4] 权限收紧")
        mode = stat.S_IMODE(os.stat(p).st_mode)
        check(".env 权限为 0600", mode == 0o600, oct(mode))
        if baks:
            bmode = stat.S_IMODE(os.stat(os.path.join(tmp, baks[0])).st_mode)
            check("备份权限为 0600", bmode == 0o600, oct(bmode))

        print("\n[5] 非法键名")
        ok, msg = envfile.update_env_var(p, "bad key", "x")
        check("含空格键名被拒绝", not ok, msg)
        ok, msg = envfile.update_env_var(p, "1ABC", "x")
        check("数字开头被拒绝", not ok, msg)

        print("\n[6] 读取与脱敏")
        open(p, "w").write("ZF_COOKIE=x=1; y=2; z=3\n")
        check("读取键值", envfile.read_env_var(p, "ZF_COOKIE") == "x=1; y=2; z=3")
        check("缺键返回 None", envfile.read_env_var(p, "NOPE") is None)
        masked = envfile.mask("x=1; y=2; z=3")
        check("脱敏不含值", "=1" not in masked and "=2" not in masked, masked)
        check("脱敏含项名", "x, y, z" in masked, masked)
        check("空值脱敏", envfile.mask("") == "(未配置)")
        check("None 脱敏", envfile.mask(None) == "(未配置)")

    print("\n[7] 无 .env 时创建")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "new.env")
        ok, _ = envfile.update_env_var(p, "ZF_COOKIE", "k=v")
        check("文件被创建", os.path.exists(p))
        check("内容正确", open(p).read().strip() == "ZF_COOKIE=k=v")

    print("\n[8] VNC 网关脚本")
    import subprocess

    gate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "vnc_gate.py")
    check("脚本存在", os.path.exists(gate))
    proc = subprocess.run(
        [sys.executable, gate, "status"], capture_output=True, text=True, timeout=15
    )
    check("status 可执行", "gate=" in proc.stdout, proc.stdout.strip())
    check("未运行时返回非 0", proc.returncode != 0)

    print("\n[9] 面板路由存在")
    try:
        from app.web import panel

        routes = {r.rule for r in panel.app.url_map.iter_rules()}
        for path in ("/api/vnc", "/api/login/start", "/api/login/status",
                     "/api/login/finish", "/api/login/cancel", "/api/restart"):
            check(f"路由 {path}", path in routes)
    except ImportError as exc:
        print(f"  ⏭  缺少 flask：{exc}")

    print("\n" + "=" * 52)
    print("P2 全部通过" if FAILED == 0 else f"P2 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
