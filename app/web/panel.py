"""
Web 面板。

设计约束（来自需求 FR-9）：
  1. **只读为主**：面板本身不对站点发起任何操作，
     唯一的写动作是「写一个触发文件」，由主循环发现后执行；
  2. 无鉴权：仅经反向代理 + 回环绑定暴露，不自行实现登录；
  3. 必须能在主程序未运行时也打开（DB 只读查询即可）。

为什么用触发文件而不是直接调用 Runner：
  调度行情景下，面板触发与定时触发可能撞车。用文件作为单一信号源，
  由主循环串行消费，天然保证不会并发执行（并发会成倍放大写操作）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template_string, request

from ..db import Database
from . import envfile
from .login_session import LoginSession

log = logging.getLogger(__name__)

TRIGGER_FILE = "/tmp/zf_lucky_trigger"
PAUSE_FILE = "/tmp/zf_lucky_paused"
VNC_FLAG_FILE = "/tmp/zf_vnc_enabled"
# 用户勾选「运行并观看」时写入，主循环结束后据此关闭网关
WATCH_FLAG_FILE = "/tmp/zf_watch_vnc"
VNC_GATE_SCRIPT = "/app/scripts/vnc_gate.py"
ENV_PATH = "/app/.env"
LOG_DIR = "/app/logs"
# noVNC 直达路径（与 nginx 中 location 保持一致）
VNC_PATH = "/vnc-x7k2m9/vnc.html?autoconnect=1&resize=scale"

app = Flask(__name__)
_db: Optional[Database] = None
_cfg = None
_login: Optional[LoginSession] = None
# 登录态探测结果缓存（避免每次刷新面板都拉起浏览器）
_session_cache: Optional[Dict[str, Any]] = None
_session_cache_at: float = 0.0


def init(db_path: str, cfg=None) -> None:
    """由主程序在启动 Flask 前调用。"""
    global _db, _cfg
    _db = Database(db_path)
    _db.connect()
    _cfg = cfg


def get_login_session() -> LoginSession:
    """懒加载登录会话（它依赖 selenium，只在用到时才创建）。"""
    global _login
    if _login is None:
        url = getattr(_cfg, "selenium_url", "http://bili-selenium:4444")
        _login = LoginSession(url)
    return _login


# ---------------- VNC 网关 ----------------

def check_session_live(cfg) -> Dict[str, Any]:
    """
    真实探测登录态是否可用。

    这是「就绪」的唯一可信判据。
    只看 .env 里有没有配 Cookie 是不够的——Cookie 可能早已过期，
    那样面板会显示「就绪」，用户以为一切正常，实际每轮都跑不起来。

    ⚠️ 必须避开主流程运行期间：
      探测会开一个真实浏览器会话，而 Selenium 的槽位有限（本机 5 个）。
      主流程正在跑时再开一个，既浪费资源，也会拖慢主流程（实测把
      100 条判定拖到 3 分钟以上）。因此运行中直接返回缓存，不做真实探测。
    """
    global _session_cache, _session_cache_at
    import time

    now = time.time()
    ttl = 60

    # 主流程运行中：不探测，直接回上次结果（或标记为未知）
    if _runner_alive():
        if _session_cache is not None:
            return _session_cache
        return {
            "checked": False,
            "live": True,  # 正在跑说明会话至少启动时是好的
            "reason": "主流程运行中，跳过探测",
            "busy": True,
        }

    if _session_cache is not None and (now - _session_cache_at) < ttl:
        return _session_cache

    if not getattr(cfg, "cookie", ""):
        result = {"checked": True, "live": False, "reason": "未配置 Cookie"}
        _session_cache, _session_cache_at = result, now
        return result

    driver = None
    try:
        from ..zf.driver import Driver
        from ..zf.session import Session, SessionError

        driver = Driver(cfg.selenium_url)
        driver.start()
        sess = Session(driver, cfg.cookie)
        user = sess.establish()
        rules = sess.check_publish_rule() or {}
        identity_pass = rules.get("identity_pass")

        live = bool(user) and not user.get("degraded")
        result = {
            "checked": True,
            "live": live,
            "uid": user.get("userId"),
            "nickname": user.get("nickname") or "",
            "identity_pass": identity_pass,
            "reason": "" if live else "会话有效但未取到用户身份",
        }
        if live and identity_pass == 0:
            result["live"] = False
            result["reason"] = "账号未绑定手机号，站点会拒绝回复"
    except SessionError as exc:
        result = {"checked": True, "live": False, "reason": str(exc)}
    except Exception as exc:
        result = {"checked": True, "live": False, "reason": f"探测失败：{exc}"}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    _session_cache, _session_cache_at = result, now
    return result


def vnc_status() -> Dict[str, Any]:
    """
    查询网关状态。

    用子进程执行 vnc_gate.py status，避免把端口探测逻辑写两份。
    """
    import subprocess

    try:
        proc = subprocess.run(
            [__import__("sys").executable, VNC_GATE_SCRIPT, "status"],
            capture_output=True, text=True, timeout=10,
        )
        running = proc.returncode == 0
        return {"running": running, "detail": proc.stdout.strip()}
    except Exception as exc:
        return {"running": False, "detail": f"检测失败：{exc}"}


def vnc_toggle(enable: bool) -> Dict[str, Any]:
    """开关网关。关闭时会立即断开所有已建立的连接。"""
    import subprocess

    action = "start" if enable else "stop"
    try:
        proc = subprocess.run(
            [__import__("sys").executable, VNC_GATE_SCRIPT, action],
            capture_output=True, text=True, timeout=20,
        )
        ok = proc.returncode == 0

        # 记录开关状态，供容器重启后恢复
        if ok:
            try:
                if enable:
                    with open(VNC_FLAG_FILE, "w") as fh:
                        fh.write("1")
                elif os.path.exists(VNC_FLAG_FILE):
                    os.unlink(VNC_FLAG_FILE)
            except OSError as exc:
                log.warning("记录 VNC 开关状态失败：%s", exc)

        return {
            "ok": ok,
            "running": enable and ok,
            "msg": (proc.stdout or proc.stderr).strip() or ("已开启" if enable else "已关闭"),
        }
    except Exception as exc:
        return {"ok": False, "running": False, "msg": str(exc)}


def _get_db() -> Database:
    if _db is None:
        raise RuntimeError("面板未初始化")
    return _db


# ---------------- 数据整形 ----------------

# 面板展示用的状态文案
STATE_LABEL = {
    None: ("未参与", "muted"),
    "pending": ("待执行", "info"),
    "done": ("已完成", "ok"),
    "failed": ("失败", "warn"),
    "give_up": ("已放弃", "err"),
}


def _fmt_time(value: Any) -> str:
    """把 'YYYY-MM-DD HH:MM:SS' 裁成 'MM-DD HH:MM'，面板更紧凑。"""
    if not value:
        return ""
    text = str(value)
    if len(text) >= 16:
        return text[5:16]
    return text


def _safe_json(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def build_view() -> Dict[str, Any]:
    """组装面板要展示的全部数据。"""
    db = _get_db()
    now = datetime.now()

    # 上次运行
    last = db.last_run() or {}
    # 数据库里的 running 只代表「启动时写入了记录」，进程可能已被杀。
    # 用锁文件是否被持有来交叉验证，避免面板长期误显示「运行中」。
    last_running = last.get("result") == "running" and _runner_alive()

    # 中奖统计
    try:
        outcome_stats = db.outcome_stats()
    except Exception:
        outcome_stats = {"won": 0, "lost": 0, "awaiting": 0}

    # 帖子列表
    rows = db.list_for_panel(limit=300)
    items: List[Dict[str, Any]] = []
    counts = {"done": 0, "failed": 0, "pending": 0, "give_up": 0, "none": 0}

    for r in rows:
        state = r.get("state")
        label, cls = STATE_LABEL.get(state, (state or "未知", "muted"))
        counts["done" if state == "done" else
               "failed" if state == "failed" else
               "give_up" if state == "give_up" else
               "pending" if state == "pending" else "none"] += 1

        prizes = _safe_json(r.get("prizes") or "")
        prize_text = " / ".join(
            f"{p.get('name')}" + (f" x{p.get('cnt')}" if p.get("cnt") else "")
            for p in prizes[:2]
        )
        if len(prizes) > 2:
            prize_text += f" 等{len(prizes)}项"

        # 抽奖是否已结束
        status = r.get("lottery_status")
        expired = status is not None and status != 1

        # 开奖结果：won / lost / None（未检测）
        outcome = r.get("outcome")
        if outcome == "won":
            state_label, state_cls = "🎉 中奖", "won"
        elif outcome == "lost":
            state_label, state_cls = "未中奖", "muted2"
        elif state == "done":
            state_label, state_cls = ("已参与·待开奖" if not expired else "已参与"), cls
        elif expired and state != "done":
            state_label, state_cls = "已开奖", "muted"
        else:
            state_label, state_cls = label, cls

        items.append(
            {
                "hash": r.get("hash"),
                "title": (r.get("title") or "")[:60],
                "author": r.get("author_nick") or "",
                "created": _fmt_time(r.get("created_at")),
                "lottery_at": _fmt_time(r.get("lottery_at")),
                "prizes": prize_text,
                "state": state,
                "outcome": outcome,
                "prize_won": r.get("prize") or "",
                "state_label": state_label,
                "state_cls": state_cls,
                "state_note": r.get("fail_reason") or "",
                "retry": r.get("retry_cnt") or 0,
                "needs_follow": bool(r.get("needs_follow")),
                "expired": expired,
                "updated": _fmt_time(r.get("updated_at")),
                "followed_at": _fmt_time(r.get("followed_at")),
                "replied_at": _fmt_time(r.get("replied_at")),
                "liked_at": _fmt_time(r.get("liked_at")),
            }
        )

    # 告警
    alerts: List[str] = []
    if last.get("result") == "captcha":
        alerts.append(
            "出现滑块验证码，任务已暂停。请通过 noVNC 手动完成验证"
            "（127.0.0.1:7900），完成后任务会自动恢复。"
        )
    elif last.get("result") == "login_invalid":
        alerts.append(
            "登录态已失效。请重新从浏览器导出 Cookie，更新 .env 后重启容器。"
        )
    elif last.get("result") == "rate_limited":
        alerts.append("被站点限流。建议提高请求间隔，或次日再试。")
    elif last.get("result") == "aborted":
        alerts.append(f"上轮异常中止：{last.get('note') or '未知原因'}")

    if os.path.exists(PAUSE_FILE):
        alerts.append("任务处于手动暂停状态，定时与手动触发都不会执行。")

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "counts": counts,
        "outcome": outcome_stats,
        "total": len(items),
        "last": last,
        "last_running": last_running,
        "alerts": alerts,
        "paused": os.path.exists(PAUSE_FILE),
        "run_at": getattr(_cfg, "run_at", "09:00") if _cfg else "09:00",
        "cookie_ok": bool(getattr(_cfg, "cookie", "")) if _cfg else False,
        "cookie_desc": envfile.mask(getattr(_cfg, "cookie", "") or "") if _cfg else "",
        "vnc": vnc_status(),
        "login": _login_safe_status(),
        "vnc_path": VNC_PATH,
        "session": check_session_live(_cfg) if _cfg else {"checked": False, "live": False,
                                                          "reason": "未初始化"},
    }


def _runner_alive() -> bool:
    """
    判断主流程是否真的在跑。

    判据是单实例锁文件里的 pid 是否存活——
    run_logs 里的 result='running' 只说明「开始过」，进程被杀死后不会更新。
    """
    try:
        with open("/tmp/zf_lucky.lock") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _login_safe_status() -> Dict[str, Any]:
    """登录会话状态；从未创建过时返回未运行，避免无谓地拉起 selenium。"""
    if _login is None:
        return {"running": False, "check": {"logged_in": False, "msg": "未启动"}}
    try:
        return _login.status()
    except Exception as exc:
        return {"running": False, "check": {"logged_in": False, "msg": str(exc)}}


# ---------------- 路由 ----------------

@app.route("/")
def index():
    try:
        view = build_view()
    except Exception as exc:  # 面板自身不能因数据问题白屏
        log.exception("面板渲染失败")
        return render_template_string(
            "<h1>面板错误</h1><pre>{{ e }}</pre>", e=str(exc)
        ), 500
    return render_template_string(PAGE, v=view)


@app.route("/api/status")
def api_status():
    try:
        return jsonify(build_view())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    """
    写触发文件，由主循环串行执行。

    可选 watch_vnc=true：运行期间自动开启浏览器观看，
    让用户能实时看到每一步操作（关注/回复/点赞），便于核对是否正确。
    任务结束后由主循环关闭网关。
    """
    if os.path.exists(PAUSE_FILE):
        return jsonify({"ok": False, "msg": "任务已暂停，请先恢复"}), 409

    body = request.get_json(silent=True) or {}
    watch = bool(body.get("watch_vnc", False))

    try:
        with open(TRIGGER_FILE, "w") as fh:
            fh.write(datetime.now().isoformat())
    except OSError as exc:
        return jsonify({"ok": False, "msg": f"无法创建触发文件：{exc}"}), 500

    vnc_msg = ""
    if watch:
        # 预置标记：主循环跑完会依据它关闭网关
        try:
            with open(WATCH_FLAG_FILE, "w") as fh:
                fh.write("1")
        except OSError:
            pass
        v = vnc_toggle(True)
        vnc_msg = "，浏览器观看已开启" if (v.get("running") or v.get("ok")) else "，但浏览器观看开启失败"

    return jsonify({
        "ok": True,
        "msg": f"已提交，主进程将在数秒内开始{vnc_msg}",
        "watch": watch,
    })


@app.route("/api/pause", methods=["POST"])
def api_pause():
    enable = bool((request.get_json(silent=True) or {}).get("enable", True))
    try:
        if enable:
            with open(PAUSE_FILE, "w") as fh:
                fh.write(datetime.now().isoformat())
        elif os.path.exists(PAUSE_FILE):
            os.unlink(PAUSE_FILE)
    except OSError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
    return jsonify({"ok": True, "paused": enable})


# ---------------- VNC 与登录 ----------------

@app.route("/api/check", methods=["POST"])
def api_check():
    """强制重新探测登录态（绕过 60 秒缓存）。"""
    global _session_cache, _session_cache_at
    import time as _t
    _session_cache, _session_cache_at = None, 0.0
    try:
        return jsonify(check_session_live(_cfg))
    except Exception as exc:
        return jsonify({"checked": True, "live": False, "reason": str(exc)}), 500


@app.route("/api/logs")
def api_logs():
    """读取日志尾部，支持级别与关键字过滤。"""
    from . import logs as logs_mod

    try:
        n = int(request.args.get("n", 300))
    except (TypeError, ValueError):
        n = 300
    data = logs_mod.tail(
        LOG_DIR,
        n=n,
        level=request.args.get("level", ""),
        keyword=request.args.get("keyword", ""),
    )
    return jsonify(data)


@app.route("/api/logs/download")
def api_logs_download():
    """下载指定日志文件（默认最新）。"""
    from flask import send_file

    from . import logs as logs_mod

    files = logs_mod.list_log_files(LOG_DIR)
    if not files:
        return jsonify({"ok": False, "msg": "没有日志文件"}), 404
    name = request.args.get("file") or files[0]["name"]
    # 防目录穿越：只允许日志目录下的已知文件
    if name not in {f["name"] for f in files}:
        return jsonify({"ok": False, "msg": "非法文件名"}), 400
    return send_file(os.path.join(LOG_DIR, name), as_attachment=True, download_name=name)


@app.route("/logs")
def logs_page():
    """独立的日志查看页（轮询刷新，适合边跑边看）。"""
    view = build_view()
    return render_template_string(LOGS_PAGE, v=view)


@app.route("/api/vnc", methods=["GET", "POST"])
def api_vnc():
    """查询或切换 noVNC 公网入口。"""
    if request.method == "GET":
        return jsonify(vnc_status())
    enable = bool((request.get_json(silent=True) or {}).get("enable", True))
    result = vnc_toggle(enable)
    # 关闭网关时顺带关掉登录会话，避免留下一个看不见却开着的浏览器
    if not enable:
        try:
            get_login_session().stop()
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    """启动登录会话并自动开启 VNC 网关。"""
    try:
        res = get_login_session().start()
        vnc_on = False
        if res.get("ok"):
            # 没开网关的话，用户看不到浏览器，登录流程无法进行
            v = vnc_toggle(True)
            vnc_on = bool(v.get("running") or v.get("ok"))
        res["vnc"] = vnc_on
        return jsonify(res)
    except Exception as exc:
        log.exception("启动登录会话失败")
        return jsonify({"ok": False, "msg": str(exc)}), 500


@app.route("/api/login/status")
def api_login_status():
    sess = get_login_session()
    st = sess.status()
    if st["running"]:
        try:
            st["check"] = sess.check_logged_in()
        except Exception as exc:
            st["check"] = {"logged_in": False, "msg": str(exc)}
    else:
        st["check"] = {"logged_in": False, "msg": "登录会话未运行"}
    return jsonify(st)


@app.route("/api/login/finish", methods=["POST"])
def api_login_finish():
    """
    用户确认已登录 → 导出 cookie → 写入 .env → 关闭会话与网关。

    写入后需要重启容器，新 Cookie 才会被主流程读到。
    """
    sess = get_login_session()
    try:
        check = sess.check_logged_in()
        if not check.get("logged_in"):
            return jsonify({
                "ok": False,
                "msg": "尚未检测到登录状态。请确认已在浏览器中完成登录。",
            }), 409

        cookie_str = sess.export_cookie_string()
        ok, detail = envfile.update_env_var(ENV_PATH, "ZF_COOKIE", cookie_str)
        if not ok:
            return jsonify({"ok": False, "msg": f"写入 .env 失败：{detail}"}), 500

        # 收尾：会话与网关都关掉，减少暴露面
        sess.stop()
        vnc_toggle(False)

        return jsonify({
            "ok": True,
            "msg": f"已保存 Cookie（{envfile.mask(cookie_str)}）。"
                   "重启容器后生效。",
            "need_restart": True,
            "cookie": envfile.mask(cookie_str),
            "user": check.get("nickname") or check.get("uid"),
        })
    except Exception as exc:
        log.exception("导出 Cookie 失败")
        return jsonify({"ok": False, "msg": str(exc)}), 500


@app.route("/api/login/cancel", methods=["POST"])
def api_login_cancel():
    """放弃登录：关闭会话与网关，不动 .env。"""
    try:
        get_login_session().stop()
        vnc_toggle(False)
        return jsonify({"ok": True, "msg": "已取消"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """
    请求重启容器。

    容器内无法重启自身，因此写一个标记文件，
    由宿主机侧（运维命令或 systemd timer）消费，或用户手动执行。
    """
    try:
        with open("/tmp/zf_restart_requested", "w") as fh:
            fh.write(datetime.now().isoformat())
    except OSError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
    return jsonify({
        "ok": True,
        "msg": "已请求重启。请在宿主机执行：cd /root/zf_lucky && docker compose restart",
    })


@app.route("/api/reset/<hash_>", methods=["POST"])
def api_reset(hash_: str):
    """
    强制重跑单帖：清空参与进度，下一轮会重新处理。

    用于「你本人手动点过赞、程序因此跳过」这类需要覆盖的场景。
    """
    try:
        db = _get_db()
        if db.get_flow(hash_) is None:
            return jsonify({"ok": False, "msg": "帖子不存在"}), 404
        db.ensure_participation(hash_)
        db.reset_participation(hash_)
        # 顺手写触发文件，让用户不用再点一次
        try:
            with open(TRIGGER_FILE, "w") as fh:
                fh.write(datetime.now().isoformat())
        except OSError:
            pass
        return jsonify({"ok": True, "msg": f"已重置 {hash_}，将重新处理"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500


@app.route("/healthz")
def healthz():
    try:
        _get_db().connect().execute("SELECT 1")
        return "ok", 200
    except Exception:
        return "db error", 500


def run_server(port: int, host: str = "0.0.0.0") -> None:
    """阻塞启动（供独立进程或后台线程使用）。"""
    app.run(host=host, port=port, threaded=True, use_reloader=False)


# ---------------- 页面模板 ----------------

PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>zf_lucky · 抽签福利</title>
<style>
  :root{
    --bg:#0f1115; --card:#171a21; --line:#252a33; --fg:#e6e9ef;
    --muted:#8b93a1; --ok:#3fb950; --warn:#d29922; --err:#f85149;
    --info:#58a6ff; --accent:#02c39a;
  }
  @media (prefers-color-scheme:light){
    :root{--bg:#f6f7f9;--card:#fff;--line:#e3e6eb;--fg:#1f2328;
          --muted:#6e7781;--ok:#1a7f37;--warn:#9a6700;--err:#cf222e;--info:#0969da;--accent:#0b8f74;}
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}
  .wrap{max-width:1240px;margin:0 auto;padding:20px}
  h1{font-size:17px;margin:0 0 4px;font-weight:600;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12px}
  .bar{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
       background:var(--card);border:1px solid var(--line);border-radius:10px;
       padding:12px 16px;margin:14px 0}
  .stat{display:flex;flex-direction:column;gap:1px;min-width:66px}
  .stat b{font-size:17px;font-weight:600}
  .stat span{font-size:11px;color:var(--muted)}
  .grow{flex:1}
  button{background:transparent;border:1px solid var(--line);color:var(--fg);
         padding:6px 13px;border-radius:7px;cursor:pointer;font-size:13px;
         transition:.15s}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#04120e;font-weight:600}
  button.primary:hover{opacity:.87;color:#04120e}
  button:disabled{opacity:.45;cursor:not-allowed}
  .alerts{margin:0 0 14px}
  .alert{border-left:3px solid var(--warn);background:rgba(210,153,34,.1);
         padding:9px 13px;border-radius:6px;margin-bottom:7px;font-size:13px}
  .alert.err{border-color:var(--err);background:rgba(248,81,73,.1)}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);
        font-size:13px;vertical-align:top}
  th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;
     letter-spacing:.5px;background:rgba(127,127,127,.05)}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(127,127,127,.04)}
  .t{font-weight:500;display:block;max-width:330px;overflow:hidden;
     text-overflow:ellipsis;white-space:nowrap}
  .meta{color:var(--muted);font-size:11px;margin-top:2px}
  .pill{display:inline-block;padding:1.5px 8px;border-radius:999px;font-size:11px;
        font-weight:600;white-space:nowrap}
  .pill.ok{background:rgba(63,185,80,.15);color:var(--ok)}
  .pill.warn{background:rgba(210,153,34,.15);color:var(--warn)}
  .pill.err{background:rgba(248,81,73,.15);color:var(--err)}
  .pill.info{background:rgba(88,166,255,.15);color:var(--info)}
  .pill.muted{background:rgba(139,147,161,.15);color:var(--muted)}
  .pill.won{background:rgba(255,196,0,.18);color:#e0a800;font-weight:700}
  .pill.muted2{background:rgba(139,147,161,.12);color:var(--muted)}
  tr.row-won td{background:rgba(255,196,0,.07)}
  .tag{font-size:10px;color:var(--muted);border:1px solid var(--line);
       padding:0 5px;border-radius:4px;margin-left:4px}
  .empty{padding:44px;text-align:center;color:var(--muted)}
  .reason{color:var(--err);font-size:11px;margin-top:2px;max-width:270px}
  .note{color:var(--muted);font-size:11px}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
         background:var(--card);border:1px solid var(--accent);color:var(--fg);
         padding:10px 18px;border-radius:9px;font-size:13px;opacity:0;
         transition:.25s;pointer-events:none;z-index:99}
  .toast.show{opacity:1}
  @media(max-width:760px){
    .hide-sm{display:none}
    .wrap{padding:12px}
    th,td{padding:7px 8px}
  }
</style>
</head>
<body>
<div class="wrap">

  <h1>zf_lucky · 抽签福利自动参与</h1>
  <div class="sub">
    扫描情报专栏 → 识别抽奖帖 → 关注 + 回复 + 点赞。
    当前时间 {{ v.now }} ｜ 定时 {{ v.run_at }}
    {% if v.session.live %}
      ｜ 已登录 <b>{{ v.session.nickname }}</b>（uid {{ v.session.uid }}）
    {% endif %}
    ｜ <a href="{{ v.vnc_path }}" target="_blank" rel="noopener"
          style="color:var(--accent)">观看浏览器 ↗</a>
    ｜ <a href="/logs" target="_blank" rel="noopener"
          style="color:var(--accent)">实时日志 ↗</a>
  </div>

  {% if v.session.checked and not v.session.live %}
  <div class="alerts" style="margin-top:12px">
    <div class="alert err">
      登录不可用：{{ v.session.reason }}
      <br>请点下方「登录并获取 Cookie」重新登录，完成后重启容器生效。
    </div>
  </div>
  {% endif %}

  <div class="bar">
    <div class="stat"><b style="color:var(--win)">{{ v.outcome['won'] }}</b><span>中奖</span></div>
    <div class="stat"><b>{{ v.outcome['lost'] }}</b><span>未中奖</span></div>
    <div class="stat"><b>{{ v.outcome['awaiting'] }}</b><span>待开奖</span></div>
    <div class="stat"><b>{{ v.counts['done'] }}</b><span>已完成</span></div>
    <div class="stat"><b>{{ v.counts['pending'] }}</b><span>待执行</span></div>
    <div class="stat"><b>{{ v.counts['failed'] }}</b><span>失败</span></div>
    <div class="stat"><b>{{ v.counts['give_up'] }}</b><span>已放弃</span></div>
    <div class="stat"><b>{{ v.counts['none'] }}</b><span>未参与</span></div>
    <div class="grow"></div>

    <div class="stat" style="min-width:96px">
      <b style="font-size:12px">
        {% if v.paused %}<span style="color:var(--warn)">已暂停</span>
        {% elif not v.cookie_ok %}<span style="color:var(--err)">未配置 Cookie</span>
        {% elif v.session.live %}<span style="color:var(--ok)">已登录</span>
        {% elif v.session.checked %}<span style="color:var(--err)">登录失效</span>
        {% else %}<span style="color:var(--muted)">未检测</span>{% endif %}
      </b>
      <span>运行状态</span>
    </div>

    <a href="{{ v.vnc_path }}" target="_blank" rel="noopener" style="text-decoration:none"><button>观看浏览器</button></a>
    <a href="/logs" target="_blank" rel="noopener" style="text-decoration:none"><button>日志</button></a>
    <button class="primary" onclick="doRun()" {% if v.paused or not v.cookie_ok or (v.session.checked and not v.session.live) %}disabled{% endif %}>立即运行</button>
    <button onclick="togglePause()">{% if v.paused %}恢复{% else %}暂停{% endif %}</button>
  </div>

  {% if v['alerts'] %}
  <div class="alerts">
    {% for a in v['alerts'] %}
    <div class="alert {{ 'err' if '失效' in a or '异常' in a else '' }}">{{ a }}</div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- 登录与浏览器观看 -->
  <div class="bar" style="margin-top:0">
    <div class="stat" style="min-width:110px">
      <b style="font-size:12px">
        {% if v['login'].running %}
          <span style="color:var(--ok)">登录中</span>
        {% else %}
          <span style="color:var(--muted)">未启动</span>
        {% endif %}
      </b>
      <span>登录会话</span>
    </div>
    <div class="stat" style="min-width:110px">
      <b style="font-size:12px">
        {% if v['vnc'].running %}
          <span style="color:var(--ok)">已开启</span>
        {% else %}
          <span style="color:var(--muted)">已关闭</span>
        {% endif %}
      </b>
      <span>浏览器观看</span>
    </div>
    <div class="grow"></div>
    {% if v['login'].running %}
      <a href="{{ v.vnc_path }}" target="_blank" rel="noopener"
         style="text-decoration:none"><button class="primary">打开浏览器画面</button></a>
      <button onclick="loginFinish()">我登录好了</button>
      <button onclick="loginCancel()">取消</button>
    {% else %}
      <button onclick="loginStart()">登录并获取 Cookie</button>
    {% endif %}
  </div>

  {% if v['login'].running %}
  <div class="alert" style="margin-bottom:14px">
    浏览器已打开登录页。请点上方「打开浏览器画面」，在弹出的窗口里扫码或输入账号密码完成登录，
    成功后点「我登录好了」——程序会把 Cookie 写入配置并提示重启。
    {% if v['login'].check and v['login'].check.logged_in %}
    <br><b style="color:var(--ok)">已检测到登录：{{ v['login'].check.nickname }}</b>
    {% endif %}
  </div>
  {% endif %}

  {% if v['last'] %}
  <div class="sub" style="margin-bottom:12px">
    上次运行：
    {% set r = v.last.result %}
    <span class="pill {{ 'ok' if r=='success' else 'warn' if r in ('running','aborted') else 'err' }}">
      {{ r }}
    </span>
    {{ v.last.started_at or '' }}
    {% if v.last.note %}· {{ v.last.note }}{% endif %}
  </div>
  {% endif %}

  {% if v['items'] %}
  <table>
    <thead>
      <tr>
        <th>帖子</th>
        <th class="hide-sm">作者</th>
        <th class="hide-sm">开奖</th>
        <th class="hide-sm">奖品</th>
        <th>状态</th>
        <th class="hide-sm">操作时间</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
    {% for it in v['items'] %}
      <tr class="{{ 'row-won' if it.outcome == 'won' else '' }}">
        <td>
          <a class="t" href="https://www.zfrontier.com/app/flow/{{ it.hash }}"
             target="_blank" rel="noopener"
             style="color:inherit;text-decoration:none">{{ it.title or it.hash }}</a>
          <div class="meta">
            {{ it.created }}
            {% if it.needs_follow %}<span class="tag">需关注</span>{% endif %}
            {% if it.retry %}<span class="tag">重试{{ it.retry }}</span>{% endif %}
          </div>
        </td>
        <td class="hide-sm">{{ it.author }}</td>
        <td class="hide-sm">{{ it.lottery_at }}</td>
        <td class="hide-sm">{{ it.prizes }}</td>
        <td>
          <span class="pill {{ it.state_cls }}">{{ it.state_label }}</span>
          {% if it.outcome == 'won' and it.prize_won %}
          <div class="prize-won">🏆 {{ it.prize_won }}</div>
          {% endif %}
          {% if it.state_note %}<div class="reason">{{ it.state_note[:80] }}</div>{% endif %}
        </td>
        <td class="hide-sm note">
          {% if it.followed_at %}关 {{ it.followed_at }}<br>{% endif %}
          {% if it.replied_at %}回 {{ it.replied_at }}<br>{% endif %}
          {% if it.liked_at %}赞 {{ it.liked_at }}{% endif %}
          {% if not it.followed_at and not it.replied_at and not it.liked_at %}—{% endif %}
        </td>
        <td>
          {% if not it.expired %}
          <button onclick="doReset('{{ it.hash }}')" title="清空进度并重跑">重跑</button>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    还没有记录。<br>
    <span style="font-size:12px">
      {% if not v.cookie_ok %}
        请先在 .env 中填入 ZF_COOKIE，然后重启容器。
      {% else %}
        点击「立即运行」开始第一轮扫描。
      {% endif %}
    </span>
  </div>
  {% endif %}

</div>

<div class="toast" id="toast"></div>

<script>
function toast(msg){
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function(){ t.classList.remove('show'); }, 2600);
}
function post(url, body){
  return fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {})
  }).then(function(r){ return r.json(); });
}
function doRun(){
  post('/api/run', {}).then(function(d){
    toast(d.msg || (d.ok ? '已提交' : '失败'));
    if (d.ok) setTimeout(function(){ location.reload(); }, 1800);
  }).catch(function(e){ toast('请求失败：' + e); });
}
function togglePause(){
  var paused = {{ 'true' if v.paused else 'false' }};
  post('/api/pause', {enable: !paused}).then(function(d){
    toast(d.paused ? '已暂停' : '已恢复');
    setTimeout(function(){ location.reload(); }, 700);
  }).catch(function(e){ toast('请求失败：' + e); });
}
function doReset(h){
  if (!confirm('重置 ' + h + ' 并重新处理？\n（用于你手动点过赞、程序因此跳过的情况）')) return;
  post('/api/reset/' + h, {}).then(function(d){
    toast(d.msg || 'ok');
    if (d.ok) setTimeout(function(){ location.reload(); }, 1200);
  }).catch(function(e){ toast('请求失败：' + e); });
}

function vncToggle(enable){
  post('/api/vnc', {enable: enable}).then(function(d){
    toast(d.msg || (enable ? '已开启' : '已关闭'));
    setTimeout(function(){ location.reload(); }, 900);
  }).catch(function(e){ toast('请求失败：' + e); });
}

function loginStart(){
  toast('正在启动浏览器…');
  post('/api/login/start', {}).then(function(d){
    toast(d.msg || (d.ok ? '已启动' : '失败'));
    if (d.ok) setTimeout(function(){ location.reload(); }, 2500);
  }).catch(function(e){ toast('请求失败：' + e); });
}

function loginFinish(){
  toast('正在导出 Cookie…');
  post('/api/login/finish', {}).then(function(d){
    if (d.ok){
      toast(d.msg);
      setTimeout(function(){ location.reload(); }, 2200);
    } else {
      toast(d.msg || '失败');
    }
  }).catch(function(e){ toast('请求失败：' + e); });
}

function loginCancel(){
  post('/api/login/cancel', {}).then(function(d){
    toast(d.msg || '已取消');
    setTimeout(function(){ location.reload(); }, 800);
  }).catch(function(e){ toast('请求失败：' + e); });
}

// 登录会话运行中时定期检测是否已登录。
// 间隔取 10 秒而非 4 秒：检测本身会发请求，轮询过密会把接口打到限流
// （实测 4 秒一次会让 /v2/user/card 返回 500「操作太频繁了」，
//   进而被误判为未登录，导致用户登录成功却一直看到「尚未登录」）。
{% if v['login'].running %}
setInterval(function(){
  fetch('/api/login/status').then(function(r){ return r.json(); }).then(function(d){
    if (d.check && d.check.logged_in){
      var t = document.getElementById('toast');
      if (t && !t.classList.contains('show')){
        toast('已检测到登录，可以点「我登录好了」');
      }
    }
  }).catch(function(){});
}, 10000);
{% endif %}
</script>
</body>
</html>
"""

# ---------------- 日志查看页 ----------------

LOGS_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>zf_lucky · 日志</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#252a33;--fg:#e6e9ef;--muted:#8b93a1;
        --ok:#3fb950;--warn:#d29922;--err:#f85149;--info:#58a6ff;--accent:#02c39a}
  @media (prefers-color-scheme:light){
    :root{--bg:#f6f7f9;--card:#fff;--line:#e3e6eb;--fg:#1f2328;--muted:#6e7781;
          --ok:#1a7f37;--warn:#9a6700;--err:#cf222e;--info:#0969da;--accent:#0b8f74;--win:#b8860b}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}
  .wrap{max-width:1400px;margin:0 auto;padding:16px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
  h1{font-size:16px;margin:0;font-weight:600}
  a{color:var(--accent);text-decoration:none}
  .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
       background:var(--card);border:1px solid var(--line);border-radius:9px;
       padding:10px 12px;margin-bottom:12px}
  select,input{background:var(--bg);border:1px solid var(--line);color:var(--fg);
               padding:5px 9px;border-radius:6px;font-size:13px}
  input{width:200px}
  button{background:transparent;border:1px solid var(--line);color:var(--fg);
         padding:5px 12px;border-radius:6px;cursor:pointer;font-size:13px}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.on{background:var(--accent);border-color:var(--accent);color:#04120e;font-weight:600}
  .meta{color:var(--muted);font-size:12px;margin-left:auto}
  #log{background:var(--card);border:1px solid var(--line);border-radius:9px;
       padding:10px 12px;height:calc(100vh - 190px);overflow:auto;
       font:12px/1.62 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       white-space:pre-wrap;word-break:break-all}
  .ln{display:flex;gap:8px;padding:1px 0}
  .ts{color:var(--muted);flex-shrink:0}
  .lv{flex-shrink:0;width:58px;font-weight:600}
  .lv.DEBUG{color:var(--muted)} .lv.INFO{color:var(--info)}
  .lv.WARNING{color:var(--warn)} .lv.ERROR,.lv.CRITICAL{color:var(--err)}
  .msg{flex:1;min-width:0}
  .row-ERROR{background:rgba(248,81,73,.07)}
  .row-WARNING{background:rgba(210,153,34,.06)}
  .hint{color:var(--muted);font-size:12px;padding:20px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>执行日志</h1>
    <a href="/">← 返回面板</a>
    <a href="{{ v.vnc_path }}" target="_blank" rel="noopener">浏览器画面 ↗</a>
    <span class="meta" id="meta"></span>
  </header>

  <div class="bar">
    <label>级别
      <select id="level" onchange="load()">
        <option value="">全部</option>
        <option value="INFO">INFO 及以上</option>
        <option value="WARNING">WARNING 及以上</option>
        <option value="ERROR">仅 ERROR</option>
      </select>
    </label>
    <label>关键字 <input id="kw" placeholder="如 hash / 抽奖 / 失败" oninput="debounceLoad()"></label>
    <label>行数
      <select id="n" onchange="load()">
        <option>200</option>
        <option selected>500</option>
        <option>1000</option>
        <option>2000</option>
      </select>
    </label>
    <button id="auto" class="on" onclick="toggleAuto()">自动刷新</button>
    <button onclick="load()">立即刷新</button>
    <button onclick="scrollBottom()">跳到最新</button>
    <a href="/api/logs/download" style="margin-left:auto"><button>下载日志</button></a>
  </div>

  <div id="log"><div class="hint">加载中…</div></div>
</div>

<script>
var autoOn = true, timer = null, tId = null;

function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function atBottom(el){
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60;
}
function scrollBottom(){
  var el = document.getElementById('log');
  el.scrollTop = el.scrollHeight;
}

function load(){
  var level = document.getElementById('level').value;
  var kw = document.getElementById('kw').value.trim();
  var n = document.getElementById('n').value;
  var el = document.getElementById('log');
  var stick = atBottom(el);

  var url = '/api/logs?n=' + encodeURIComponent(n)
          + '&level=' + encodeURIComponent(level)
          + '&keyword=' + encodeURIComponent(kw);

  fetch(url).then(function(r){ return r.json(); }).then(function(d){
    if (!d.lines || !d.lines.length){
      el.innerHTML = '<div class="hint">暂无匹配日志</div>';
      return;
    }
    var html = d.lines.map(function(x){
      var lv = x.level || '';
      var cls = lv ? 'row-' + lv : '';
      return '<div class="ln ' + cls + '">'
        + '<span class="ts">' + esc(x.ts || '') + '</span>'
        + '<span class="lv ' + esc(lv) + '">' + esc(lv) + '</span>'
        + '<span class="msg">' + esc(x.msg) + '</span>'
        + '</div>';
    }).join('');
    el.innerHTML = html;
    if (stick) scrollBottom();

    var m = '文件 ' + (d.file || '-') + ' ｜ 显示 ' + d.matched + ' / ' + d.total + ' 行';
    if (d.truncated) m += ' ｜ 已截断';
    m += ' ｜ ' + new Date().toLocaleTimeString();
    document.getElementById('meta').textContent = m;
  }).catch(function(e){
    el.innerHTML = '<div class="hint">加载失败：' + esc(e) + '</div>';
  });
}

function debounceLoad(){
  clearTimeout(tId);
  tId = setTimeout(load, 350);
}

function toggleAuto(){
  autoOn = !autoOn;
  var b = document.getElementById('auto');
  b.className = autoOn ? 'on' : '';
  b.textContent = autoOn ? '自动刷新' : '已暂停';
  schedule();
}

function schedule(){
  clearInterval(timer);
  if (autoOn) timer = setInterval(load, 3000);
}

load();
schedule();
</script>
</body>
</html>
"""
