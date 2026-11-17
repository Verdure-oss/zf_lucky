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
VNC_GATE_SCRIPT = "/app/scripts/vnc_gate.py"
ENV_PATH = "/app/.env"

app = Flask(__name__)
_db: Optional[Database] = None
_cfg = None
_login: Optional[LoginSession] = None


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
    last_running = last.get("result") == "running"

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

        items.append(
            {
                "hash": r.get("hash"),
                "title": (r.get("title") or "")[:60],
                "author": r.get("author_nick") or "",
                "created": _fmt_time(r.get("created_at")),
                "lottery_at": _fmt_time(r.get("lottery_at")),
                "prizes": prize_text,
                "state": state,
                "state_label": "已开奖" if expired and state != "done" else label,
                "state_cls": "muted" if expired and state != "done" else cls,
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
    }


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
    """写触发文件，由主循环串行执行。"""
    if os.path.exists(PAUSE_FILE):
        return jsonify({"ok": False, "msg": "任务已暂停，请先恢复"}), 409
    try:
        with open(TRIGGER_FILE, "w") as fh:
            fh.write(datetime.now().isoformat())
    except OSError as exc:
        return jsonify({"ok": False, "msg": f"无法创建触发文件：{exc}"}), 500
    return jsonify({"ok": True, "msg": "已提交，主进程将在数秒内开始"})


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
  </div>

  <div class="bar">
    <div class="stat"><b>{{ v.counts['done'] }}</b><span>已完成</span></div>
    <div class="stat"><b>{{ v.counts['pending'] }}</b><span>待执行</span></div>
    <div class="stat"><b>{{ v.counts['failed'] }}</b><span>失败</span></div>
    <div class="stat"><b>{{ v.counts['give_up'] }}</b><span>已放弃</span></div>
    <div class="stat"><b>{{ v.counts['none'] }}</b><span>未参与</span></div>
    <div class="grow"></div>

    <div class="stat">
      <b style="font-size:12px">
        {% if v.paused %}<span style="color:var(--warn)">已暂停</span>
        {% elif not v.cookie_ok %}<span style="color:var(--err)">未配置 Cookie</span>
        {% else %}<span style="color:var(--ok)">就绪</span>{% endif %}
      </b>
      <span>运行状态</span>
    </div>

    <button class="primary" onclick="doRun()" {% if v.paused or not v.cookie_ok %}disabled{% endif %}>立即运行</button>
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
      <a href="/vnc/vnc.html?autoconnect=1&resize=scale" target="_blank" rel="noopener"
         style="text-decoration:none"><button class="primary">打开浏览器画面</button></a>
      <button onclick="loginFinish()">我登录好了</button>
      <button onclick="loginCancel()">取消</button>
    {% else %}
      <button onclick="loginStart()">登录并获取 Cookie</button>
      {% if v['vnc'].running %}
      <button onclick="vncToggle(false)">关闭观看</button>
      {% endif %}
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
      <tr>
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
    if (d.ok) setTimeout(function(){
      window.open('/vnc/vnc.html?autoconnect=1&resize=scale', '_blank');
      location.reload();
    }, 3500);
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

// 登录会话运行中时，每 4 秒自动检测一次是否已登录
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
}, 4000);
{% endif %}
</script>
</body>
</html>
"""
