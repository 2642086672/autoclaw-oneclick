#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoClaw 反代 · 每日定时维护（一次性执行版）。

和 supervisor.py 的分工：
  supervisor.py  —— 常驻，每 30 秒保活。只做"活着没有"级别的判断。
  maintain.py    —— 定时（每天 12:00 / 17:00）跑一次，做完整自检 + 修复 + 真实请求验证。

为什么要分开：常驻守护为了省资源只看端口通不通；它看不出
"端口在、但 token 已经过期"、"进程在、但上游报非法模型"这类问题。
定时维护会真发一次请求，把这类问题抓出来。

退出码：0 全部正常 / 1 有项目被修复过 / 2 有项目修不好（需要人工看日志）
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RELAY_DIR = Path(r"C:\Users\Administrator\.autoclaw-relay")
NODE = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
PORT = 18766
LOG = RELAY_DIR / "maintain.log"
HEADERS = Path(os.path.expanduser("~")) / ".openclaw-autoclaw" / "request-headers.json"

# AutoClaw 客户端候选路径（2.x 是 AutoClaw2.exe）
CLIENT_CANDIDATES = [
    r"D:\autoclaw\AutoClaw2\AutoClaw2.exe",
    r"C:\Users\Administrator\AppData\Local\Programs\AutoClaw2\AutoClaw2.exe",
]

NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200 | 0x08000000

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

REPORT = []      # [(级别, 文本)]  级别: ok / fixed / fail
FIXED = False
FAILED = False


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def mark(level, text):
    global FIXED, FAILED
    if level == "fixed":
        FIXED = True
    elif level == "fail":
        FAILED = True
    REPORT.append((level, text))
    log(f"[{level}] {text}")


# ---------------------------------------------------------------- 检查项

def port_alive(port=PORT, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        return f'"{pid}"' in (r.stdout or "")
    except Exception:
        return False


def read_pid(p):
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def client_running():
    for name in ("AutoClaw2.exe", "AutoClaw.exe"):
        try:
            r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            if name.lower() in (r.stdout or "").lower():
                return True
        except Exception:
            pass
    return False


def client_exe():
    for p in CLIENT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def token_left():
    """读 JWT exp，返回剩余秒数；读不到返回 None。"""
    try:
        h = json.loads(HEADERS.read_text(encoding="utf-8"))
        tok = (h.get("headers") or h)["X-Authorization"].split(" ", 1)[1]
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = json.loads(base64.urlsafe_b64decode(seg))["exp"]
        return exp - time.time()
    except Exception:
        return None


# ---------------------------------------------------------------- 修复动作

def _spawn_via_wmi(exe_path, args="", workdir=None):
    """
    用 WMI 拉起进程，脱离当前进程树。

    为什么必须这样：maintain.py 是被计划任务拉起的，任务结束时 Windows 会把
    整个作业对象里的子进程一起收走 —— 直接 Popen 出来的客户端/反代
    会在维护脚本退出的瞬间被杀（日志写"已拉起"，但几秒后进程就没了）。
    WMI 创建的进程挂在 wmiprvse.exe 名下，不属于任务的作业对象。

    workdir：WMI 支持 CurrentDirectory，不指定的话子进程 CWD 是 System32，
    相对路径（server.mjs）会找不到。
    """
    cmdline = (f'"{exe_path}" {args}'.strip()).replace("'", "''")
    q = "'" + cmdline + "'"
    if workdir:
        wd = "'" + workdir.replace("'", "''") + "'"
        ps = ("$r = ([wmiclass]'root\\cimv2:Win32_Process').Create(%s, %s); "
              "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { -1 }") % (q, wd)
    else:
        ps = ("$r = ([wmiclass]'root\\cimv2:Win32_Process').Create(%s); "
              "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { -1 }") % q
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=90, creationflags=NO_WINDOW)
        pid = int((r.stdout or "-1").strip().splitlines()[-1])
        return pid if pid > 0 else None
    except Exception as e:
        log(f"WMI 拉起失败：{e}")
        return None


def fix_client():
    exe = client_exe()
    if not exe:
        mark("fail", "找不到 AutoClaw2.exe，客户端无法拉起")
        return
    pid = _spawn_via_wmi(exe)
    if not pid:
        mark("fail", f"AutoClaw 客户端拉起失败：{exe}")
        return
    time.sleep(20)   # 等它落 enc
    if client_running():
        mark("fixed", f"AutoClaw 客户端已拉起（pid {pid}）：{exe}")
    else:
        mark("fail", f"客户端拉起后仍未驻留（pid {pid}），可能被安全软件拦截")


def fix_token():
    try:
        r = subprocess.run([PY, "token-sync.py", "--once"], cwd=str(RELAY_DIR),
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        mark("fixed", f"凭据已刷新：{out[-1] if out else '无输出'}")
    except Exception as e:
        mark("fail", f"凭据刷新失败：{e}")


def fix_relay():
    """
    起反代。因为本脚本由计划任务拉起，必须用 WMI 脱离作业对象，
    否则任务结束时 node 会被一起收走（表现为"已拉起"但端口马上又没了）。

    两个坑：
    1) 不能 `cmd.exe /c start-relay.cmd` 走 WMI —— 那个 cmd 会等 node 一起结束，
       WMI 调用跟着挂住，脚本被计划任务超时杀掉（0xC000013A）。
    2) WMI 子进程继承的是 wmiprvse.exe 的环境，不是我们的。所以环境变量
       必须落进一个 .cmd 里由它自己 set，不能靠 os.environ。

    折中：生成一个 start-relay-bg.cmd，里面 set 好所有变量后 `start /b` node，
    cmd 立刻退出（node 因为 /b + start 已脱离），WMI 调用不阻塞。
    """
    # 先把凭据刷一遍，避免拿过期 token 起来
    try:
        subprocess.run([PY, "token-sync.py", "--once"], cwd=str(RELAY_DIR),
                       capture_output=True, timeout=90, creationflags=NO_WINDOW)
    except Exception as e:
        log(f"启动前刷新凭据失败：{e}")

    key_file = RELAY_DIR / "api-key.txt"
    key = key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""

    launcher = RELAY_DIR / "start-relay-bg.cmd"
    launcher.write_text(f"""@echo off
setlocal
cd /d "{RELAY_DIR}"
set AUTOCLAW_BIND_HOST=127.0.0.1
set AUTOCLAW_PORT={PORT}
set AUTOCLAW_CONTRACT=2x
set AUTOCLAW_CLOUD_LANE=cn
set AUTOCLAW_CLIENT_VERSION=2.2.0.308
set AUTOCLAW_X_CHANNEL=official
set AUTOCLAW_X_TRACE_ID=autoclaw-desktop
set AUTOCLAW_HARNESS_MARKER=0
set AUTOCLAW_MAX_OUTPUT_TOKENS=0
set AUTOCLAW_INBOUND_KEYS={key}
set AUTOCLAW_LOG_FILE={RELAY_DIR}\\server.log
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
start "" /b "{NODE}" server.mjs
endlocal
""", encoding="utf-8")

    # 用 /c 执行 launcher；里面 start /b 后立刻结束，cmd 不会挂住
    pid = _spawn_via_wmi("cmd.exe", f'/c "{launcher}"', workdir=str(RELAY_DIR))
    if not pid:
        mark("fail", "反代拉起失败（WMI 调用异常）")
        return

    for _ in range(60):
        if port_alive():
            mark("fixed", f"反代已拉起（端口 {PORT}）")
            return
        time.sleep(0.5)
    mark("fail", f"反代拉起后仍未监听，查 {RELAY_DIR / 'server.log'}")


def fix_token_sync():
    pid = _spawn_via_wmi(PY, f'"{RELAY_DIR / "token-sync.py"}"', workdir=str(RELAY_DIR))
    if not pid:
        mark("fail", "token-sync 拉起失败")
        return
    (RELAY_DIR / "token-sync.pid").write_text(str(pid), encoding="utf-8")
    mark("fixed", f"token-sync 已拉起（pid {pid}）")


# ---------------------------------------------------------------- 真实请求验证

def verify_request():
    """真发一次请求。这是唯一能同时验证 token / 线路 / 模型可用性的手段。"""
    key_file = RELAY_DIR / "api-key.txt"
    if not key_file.is_file():
        mark("fail", "api-key.txt 不存在，无法验证")
        return
    key = key_file.read_text(encoding="utf-8").strip()
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "回复OK"}],
        "stream": False,
    }).encode()
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
            ms = int((time.time() - t0) * 1000)
        if '"content"' in raw:
            mark("ok", f"真实请求通过（{ms}ms）")
        else:
            mark("fail", f"请求返回异常内容：{raw[:150]}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        mark("fail", f"请求失败 HTTP {e.code}：{detail}")
    except Exception as e:
        mark("fail", f"请求失败：{e}")


# ---------------------------------------------------------------- 主流程

def run():
    log("════════ 定时维护开始 ════════")

    # 1) 客户端：凭据的唯一续期来源
    if client_running():
        mark("ok", "AutoClaw 客户端在运行")
    else:
        fix_client()

    # 2) 凭据：先看剩余，再决定要不要刷
    left = token_left()
    if left is None:
        fix_token()
    elif left < 0:
        mark("fail", f"凭据已过期 {-left/3600:.1f} 小时，尝试刷新")
        fix_token()
    elif left < 3600:
        mark("fixed", f"凭据仅剩 {left/60:.0f} 分钟，刷新")
        fix_token()
    else:
        mark("ok", f"凭据剩余 {left/3600:.1f} 小时")

    # 3) 反代端口
    if port_alive():
        mark("ok", f"反代在监听 {PORT}")
    else:
        fix_relay()

    # 4) 续期器
    if pid_alive(read_pid(RELAY_DIR / "token-sync.pid")):
        mark("ok", "token-sync 在运行")
    else:
        fix_token_sync()

    # 5) 真实请求：能抓出"端口通但 token 废了"这种
    if port_alive():
        verify_request()
    else:
        mark("fail", "反代未监听，跳过请求验证")

    # 汇总
    n_ok = sum(1 for lv, _ in REPORT if lv == "ok")
    n_fix = sum(1 for lv, _ in REPORT if lv == "fixed")
    n_bad = sum(1 for lv, _ in REPORT if lv == "fail")
    summary = f"维护结束：正常 {n_ok} / 已修复 {n_fix} / 异常 {n_bad}"
    log(summary + "\n")

    code = 2 if FAILED else (1 if FIXED else 0)
    return code


if __name__ == "__main__":
    sys.exit(run())
