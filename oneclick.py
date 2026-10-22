# -*- coding: utf-8 -*-
"""
AutoClaw 反代一键通

把 AutoClaw 的模型额度变成本地 OpenAI / Anthropic 兼容接口，供任意 AI 工具使用。

    python oneclick.py            出菜单，想干嘛按数字
    python oneclick.py --auto     全自动：检查 -> 部署 -> 起服务 -> 打印连接信息

首次用 --auto 跑一遍，之后开机自启会自己维护，基本不用管。
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
RELAY_DIR = Path(os.environ["USERPROFILE"]) / ".autoclaw-relay"
STATE_DIR = Path(os.environ["USERPROFILE"]) / ".openclaw-autoclaw"
HEADERS_FILE = STATE_DIR / "request-headers.json"
STARTUP_DIR = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
AUTOSTART_NAME = "AutoClaw-Relay-Supervisor.cmd"

PORT = 18766
LANE = "cn"                     # 国内官方渠道
CONTRACT = "2x"
CLIENT_VERSION = "2.2.0.308"    # X-Version 必须四段式，三段式会被拒

# 本机 node / python，找不到就用 PATH 里的
NODE_CANDIDATES = [
    os.path.expandvars(r"%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"),
    r"C:\Program Files\nodejs\node.exe",
    "node",
]
PY_CANDIDATES = [
    os.path.expandvars(r"%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    sys.executable,
    "python",
]

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
OFF = "\033[0m"


# ---------------------------------------------------------------- 基础工具
def find_exe(cands):
    for c in cands:
        if not c:
            continue
        if os.path.sep in c or (len(c) > 2 and c[1] == ":"):
            if os.path.isfile(c):
                return c
        else:
            p = shutil.which(c)
            if p:
                return p
    return None


NODE = find_exe(NODE_CANDIDATES)
PY = find_exe(PY_CANDIDATES)


def say(msg, color=""):
    print(f"{color}{msg}{OFF}" if color else msg)


def step(n, total, msg):
    say(f"[{n}/{total}] {msg}", DIM)


def ok(msg):
    say(f"    {GREEN}√{OFF} {msg}")


def warn(msg):
    say(f"    {YELLOW}!{OFF} {msg}")


def fail(msg):
    say(f"    {RED}×{OFF} {msg}")


def run(cmd, **kw):
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    return subprocess.run(cmd, env=env, **kw)


def port_listening(port):
    """不依赖 netstat，直接连一下。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def http_get(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 各步
def check_env():
    """返回问题清单（空 = 全过）。"""
    problems = []
    if not NODE:
        problems.append("没找到 Node.js。装一个：https://nodejs.org （选 20 以上 LTS）")
    else:
        r = run([NODE, "-v"])
        ok(f"Node.js  {r.stdout.strip()}  {DIM}{NODE}{OFF}")
    if not PY:
        problems.append("没找到 Python。装一个：https://python.org （3.10 以上）")
    else:
        r = run([PY, "-c", "import sys;print(sys.version.split()[0])"])
        ok(f"Python   {r.stdout.strip()}  {DIM}{PY}{OFF}")
        r2 = run([PY, "-c", "import cryptography;print(cryptography.__version__)"])
        if r2.returncode != 0:
            warn("缺 cryptography 库，正在安装...")
            r3 = run([PY, "-m", "pip", "install", "cryptography"], timeout=300)
            if r3.returncode == 0:
                ok("cryptography 已安装")
            else:
                problems.append("cryptography 装不上，手动执行：pip install cryptography")
        else:
            ok(f"cryptography {r2.stdout.strip()}")

    # AutoClaw 客户端
    try:
        sys.path.insert(0, str(ASSETS))
        import persona_extract
        bundle = persona_extract.locate_client()
        ok(f"AutoClaw  {DIM}{bundle}{OFF}")
    except Exception as e:
        problems.append(f"找不到 AutoClaw 客户端：{e}")
    return problems


def refresh_credentials():
    """解密 AutoClaw 凭证 -> 写 request-headers.json（反代读这个）。"""
    sc = ASSETS / "make_compat_auth.py"
    r = run([PY, str(sc)], cwd=str(ASSETS))
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[-300:]
    # make_compat_auth 写的是 auth-compat/auth.json，反代读的是 request-headers.json —— 桥一下
    auth_file = Path.home() / ".autoclaw-relay" / "auth-compat" / "auth.json"
    if not auth_file.is_file():
        return False, "auth-compat/auth.json 没生成"
    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    tok = auth.get("token") or ""
    if not tok:
        return False, "凭证里没有 token"
    if not tok.startswith("Bearer "):
        tok = "Bearer " + tok
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEADERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"headers": {"X-Authorization": tok, "X-Client-Type": "pc"}, "sessionHeaders": {}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HEADERS_FILE)
    return True, f"token 长度 {len(tok)}"


def ensure_persona():
    out = RELAY_DIR / "persona.txt"
    sc = ASSETS / "persona_extract.py"
    env = dict(os.environ)
    if NODE:
        env["NODE_EXE"] = NODE
    r = subprocess.run([PY, str(sc), "--out", str(out)], capture_output=True, text=True,
                       timeout=180, env=env)
    try:
        j = json.loads(r.stdout or "{}")
    except Exception:
        j = {}
    if j.get("ok"):
        return True, f"{j.get('source', '')}  长度 {j.get('len')}"
    if out.is_file():
        return True, "用已有 persona.txt（本次抽取失败，沿用旧的）"
    return False, j.get("error") or (r.stderr or "").strip()[-300:]


def deploy():
    """把反代程序放到运行目录。"""
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ASSETS / "server_2x.mjs", RELAY_DIR / "server.mjs")
    return True, str(RELAY_DIR / "server.mjs")


def load_key():
    f = RELAY_DIR / "api-key.txt"
    if f.is_file():
        k = f.read_text(encoding="utf-8").strip()
        if k:
            return k
    k = "sk-ac-" + secrets.token_hex(24)
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(k, encoding="utf-8")
    return k


def write_scripts(key):
    """生成启动 / 守护 / 续期三个脚本（把密钥和路径固化进去）。"""
    node = NODE or "node"
    py = PY or "python"

    (RELAY_DIR / "token-sync.py").write_text(TOKEN_SYNC_SRC, encoding="utf-8")
    (RELAY_DIR / "a_switch.py").write_text(
        (ASSETS / "a_switch.py").read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

    start = f"""@echo off
setlocal
set RELAY_DIR={RELAY_DIR}
set NODE={node}
set PY={py}
set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set all_proxy=
set ALL_PROXY=
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
set INBOUND_KEY={key}
cd /d "%RELAY_DIR%"
"%PY%" token-sync.py --once >nul 2>&1
start "autoclaw-token-sync" /min "%PY%" token-sync.py
set AUTOCLAW_BIND_HOST=127.0.0.1
set AUTOCLAW_PORT={PORT}
set AUTOCLAW_CONTRACT={CONTRACT}
set AUTOCLAW_CLOUD_LANE={LANE}
set AUTOCLAW_CLIENT_VERSION={CLIENT_VERSION}
set AUTOCLAW_X_CHANNEL=official
set AUTOCLAW_X_TRACE_ID=autoclaw-desktop
set AUTOCLAW_HARNESS_MARKER=0
set AUTOCLAW_MAX_OUTPUT_TOKENS=0
set AUTOCLAW_INBOUND_KEYS=%INBOUND_KEY%
set AUTOCLAW_LOG_FILE=%RELAY_DIR%\\server.log
start "autoclaw-relay" /min "%NODE%" server.mjs
endlocal
"""
    (RELAY_DIR / "start-relay.cmd").write_text(start, encoding="utf-8")

    sup = f"""@echo off
setlocal
set RELAY_DIR={RELAY_DIR}
set NODE={node}
set PY={py}
set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set all_proxy=
set ALL_PROXY=
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
title AutoClaw 反代守护
echo AutoClaw 反代守护运行中（每 5 分钟自查，可最小化，关窗口即停止守护）
:loop
  netstat -ano | findstr /R /C:":{PORT} .*LISTENING" >nul 2>&1
  if errorlevel 1 (
    echo [%time%] 反代未运行，拉起中...
    echo [%date% %time%] relay down >> "%RELAY_DIR%\\keepalive.log"
    call "%RELAY_DIR%\\start-relay.cmd" >nul 2>&1
  )
  set SPID=
  if exist "%RELAY_DIR%\\token-sync.pid" set /p SPID=<"%RELAY_DIR%\\token-sync.pid"
  if not defined SPID goto :start_sync
  tasklist /FI "PID eq %SPID%" 2>nul | findstr /I "python" >nul 2>&1
  if errorlevel 1 goto :start_sync
  goto :sleep
  :start_sync
  echo [%time%] 续期器未运行，拉起中...
  echo [%date% %time%] token-sync down >> "%RELAY_DIR%\\keepalive.log"
  cd /d "%RELAY_DIR%"
  start "autoclaw-token-sync" /min "%PY%" token-sync.py
  :sleep
  timeout /t 300 /nobreak >nul
  goto :loop
"""
    (RELAY_DIR / "supervisor.cmd").write_text(sup, encoding="utf-8")
    return True, "start-relay.cmd / supervisor.cmd / token-sync.py"


def install_autostart():
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = STARTUP_DIR / AUTOSTART_NAME
    shutil.copy2(RELAY_DIR / "supervisor.cmd", dst)
    return True, str(dst)


def remove_autostart():
    dst = STARTUP_DIR / AUTOSTART_NAME
    if dst.is_file():
        dst.unlink()
        return True, "已移除开机自启"
    return True, "本来就没有"


def spawn_detached(cmd, cwd=None):
    """
    拉起一个完全脱离父进程的进程。

    为什么不用 subprocess.run / start：那些子进程会继承父进程的句柄，
    父进程一退出（或被外层终止）就把它们一起带走。用 DETACHED_PROCESS +
    CREATE_NEW_PROCESS_GROUP + 关闭继承句柄，让服务自己活。
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    devnull = open(os.devnull, "w")
    try:
        return subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=devnull, stdout=devnull, stderr=devnull,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
        )
    finally:
        devnull.close()


def start_services():
    if port_listening(PORT):
        return True, "反代已经在跑了"
    spawn_detached(["cmd.exe", "/c", str(RELAY_DIR / "start-relay.cmd")], cwd=str(RELAY_DIR))
    for _ in range(30):
        if port_listening(PORT):
            return True, f"反代已启动（端口 {PORT}）"
        time.sleep(0.5)
    return False, f"启动超时，看日志 {RELAY_DIR / 'server.log'}"


def stop_services():
    killed = []
    # 反代：按端口找 pid
    r = run(["netstat", "-ano"], timeout=30)
    pids = set()
    for line in (r.stdout or "").splitlines():
        if f":{PORT}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    for pid in pids:
        run(["taskkill", "/F", "/PID", pid], timeout=30)
        killed.append(f"relay pid {pid}")
    # 续期器
    pf = RELAY_DIR / "token-sync.pid"
    if pf.is_file():
        pid = pf.read_text(encoding="utf-8").strip()
        if pid:
            run(["taskkill", "/F", "/PID", pid], timeout=30)
            killed.append(f"token-sync pid {pid}")
        try:
            pf.unlink()
        except Exception:
            pass
    return True, ("已停止：" + ", ".join(killed)) if killed else "本来就没在跑"


def status():
    """返回 (状态字典, 展示用文本)。"""
    st = {}
    st["反代"] = "运行中" if port_listening(PORT) else "未运行"
    st["监听地址"] = f"http://127.0.0.1:{PORT}"
    if port_listening(PORT):
        try:
            code, body = http_get(f"http://127.0.0.1:{PORT}/health")
            j = json.loads(body)
            st["上游线路"] = j.get("cloud", "")
            st["运行时长"] = f"{j.get('uptime_s', 0) // 60} 分钟"
        except Exception as e:
            st["健康检查"] = f"失败：{e}"
    pf = RELAY_DIR / "token-sync.pid"
    st["凭证续期器"] = "运行中" if pf.is_file() and pf.exists() else "未运行"
    k = load_key()
    st["API Key"] = k
    return st


def print_info():
    k = load_key()
    say("")
    say("=" * 60)
    say(f"  {GREEN}连接信息{OFF}（复制到你的 AI 工具里）")
    say("=" * 60)
    say(f"  OpenAI   baseUrl : http://127.0.0.1:{PORT}/v1")
    say(f"  Anthropic baseUrl: http://127.0.0.1:{PORT}")
    say(f"  API Key          : {k}")
    say(f"  模型             : glm-5.3-flash / glm-5.3 / auto / auto-fast")
    say(f"                    deepseek-v4.1-flash / deepseek-v4-pro")
    say("=" * 60)
    say(f"  不知道选哪个模型，先用 {GREEN}glm-5.3-flash{OFF}")
    say(f"  日志：{RELAY_DIR / 'server.log'}")
    say("=" * 60)


# ---------------------------------------------------------------- 一键流程
def do_auto():
    say("")
    say(f"{GREEN}AutoClaw 反代一键部署{OFF}")
    say("")

    total = 6
    step(1, total, "检查环境")
    problems = check_env()
    if problems:
        for p in problems:
            fail(p)
        say("")
        say(f"{RED}环境不全，先把上面的问题解决再跑。{OFF}")
        return 1

    step(2, total, "取账号凭证")
    good, msg = refresh_credentials()
    (ok if good else fail)(msg)
    if not good:
        say(f"    {YELLOW}提示：先打开 AutoClaw 桌面端登录一次，再重跑本工具。{OFF}")
        return 1

    step(3, total, "抽取 persona")
    good, msg = ensure_persona()
    (ok if good else fail)(msg)
    if not good:
        return 1

    step(4, total, "部署反代")
    good, msg = deploy()
    (ok if good else fail)(msg)

    step(5, total, "写配置与守护脚本")
    key = load_key()
    good, msg = write_scripts(key)
    (ok if good else fail)(msg)
    good, msg = install_autostart()
    (ok if good else fail)(msg)

    step(6, total, "启动服务")
    good, msg = start_services()
    (ok if good else fail)(msg)
    if not good:
        return 1

    say("")
    say(f"{GREEN}全部完成。{OFF}开机自启已装好，重启电脑不用管。")
    print_info()
    return 0


# ---------------------------------------------------------------- 菜单
def menu():
    while True:
        say("")
        say(f"{GREEN}AutoClaw 反代 · 管理{OFF}")
        say("  1) 一键部署（首次用这个）")
        say("  2) 启动服务")
        say("  3) 停止服务")
        say("  4) 看状态")
        say("  5) 显示连接信息")
        say("  6) 刷新账号凭证（报 401 时用）")
        say("  7) 换一把 API Key")
        say("  8) 移除开机自启")
        say("  0) 退出")
        c = input("\n  选一个：").strip()
        say("")
        if c == "1":
            do_auto()
        elif c == "2":
            good, msg = start_services()
            (ok if good else fail)(msg)
        elif c == "3":
            good, msg = stop_services()
            (ok if good else fail)(msg)
        elif c == "4":
            for k, v in status().items():
                say(f"  {k:12} : {v}")
        elif c == "5":
            print_info()
        elif c == "6":
            good, msg = refresh_credentials()
            (ok if good else fail)(msg)
            if good:
                good2, msg2 = start_services()
                (ok if good2 else fail)(msg2)
        elif c == "7":
            f = RELAY_DIR / "api-key.txt"
            if f.is_file():
                f.unlink()
            k = load_key()
            write_scripts(k)
            good, msg = stop_services()
            (ok if good else fail)(msg)
            good, msg = start_services()
            (ok if good else fail)(msg)
            ok(f"新 Key：{k}")
        elif c == "8":
            good, msg = remove_autostart()
            (ok if good else fail)(msg)
        elif c == "0":
            return 0
        else:
            warn("输入 0-8")


# ---------------------------------------------------------------- 入口
TOKEN_SYNC_SRC = '''# -*- coding: utf-8 -*-
"""凭证续期器：监听 AutoClaw 凭证文件变化，自动解密并同步给反代。零网络出站。"""
import base64, json, os, sys, time
from pathlib import Path

APPDATA = Path(os.environ["APPDATA"])
SRC = APPDATA / "AutoClaw-official"
HEADERS_FILE = Path.home() / ".openclaw-autoclaw" / "request-headers.json"
RELAY_DIR = Path.home() / ".autoclaw-relay"
HERE = Path(__file__).resolve().parent

_WANT_ONCE = "--once" in sys.argv
sys.argv = ["x"]
import importlib.util
spec = importlib.util.spec_from_file_location("a_switch", HERE / "a_switch.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decrypt_one():
    if not (SRC / "Local State").is_file():
        return None
    key = m._os_crypt_key(SRC)
    acc_root = SRC / "accounts"
    if not acc_root.is_dir():
        return None
    for acc_dir in acc_root.iterdir():
        enc = acc_dir / "account-credentials.enc"
        if not enc.is_file():
            continue
        blob = enc.read_bytes()
        if blob[:3] != b"v10":
            continue
        plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
        cred = json.loads(plain.decode("utf-8"))
        tok = cred.get("accessToken") or cred.get("token")
        if tok:
            return tok
    return None


def current_token():
    try:
        j = json.loads(HEADERS_FILE.read_text(encoding="utf-8"))
        h = j.get("headers") or j
        return (h.get("X-Authorization") or "").strip()
    except Exception:
        return ""


def jwt_exp(tok):
    raw = tok.split(" ", 1)[1] if " " in tok else tok
    try:
        part = raw.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp", 0))
    except Exception:
        return 0


def sync(force=False):
    tok = decrypt_one()
    if not tok:
        print("[token-sync] 解密失败", flush=True)
        return False
    if not tok.startswith("Bearer "):
        tok = "Bearer " + tok
    if not force and current_token() == tok:
        return False
    HEADERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEADERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"headers": {"X-Authorization": tok, "X-Client-Type": "pc"},
                               "sessionHeaders": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HEADERS_FILE)
    print(f"[token-sync] 已同步，剩余 {round((jwt_exp(tok) - time.time()) / 60, 1)} 分钟", flush=True)
    return True


def fingerprint():
    fp = {}
    acc = SRC / "accounts"
    if acc.is_dir():
        for sub in acc.iterdir():
            enc = sub / "account-credentials.enc"
            if enc.is_file():
                fp[str(enc)] = enc.stat().st_mtime
    ls = SRC / "Local State"
    fp[str(ls)] = ls.stat().st_mtime if ls.exists() else 0
    return fp


def main():
    if _WANT_ONCE:
        sync(force=True)
        return
    state = fingerprint()
    print(f"[token-sync] 监听 {SRC}", flush=True)
    (RELAY_DIR / "token-sync.pid").write_text(str(os.getpid()), encoding="utf-8")
    synced_at = 0.0
    while True:
        time.sleep(5)
        now = fingerprint()
        if now != state:
            state = now
            if time.time() - synced_at >= 30:
                try:
                    if sync():
                        synced_at = time.time()
                except Exception as e:
                    print(f"[token-sync] 错误 {e}", flush=True)
                    time.sleep(30)
        try:
            exp = jwt_exp(current_token())
            if exp and exp - time.time() < 1800 and time.time() - synced_at >= 600:
                sync(force=True)
                synced_at = time.time()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            pf = RELAY_DIR / "token-sync.pid"
            if pf.exists() and pf.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pf.unlink()
        except Exception:
            pass
'''


if __name__ == "__main__":
    try:
        if os.name == "nt":
            os.system("")
    except Exception:
        pass
    args = sys.argv[1:]
    if "--auto" in args or "-a" in args:
        sys.exit(do_auto())
    if "--status" in args:
        for k, v in status().items():
            print(f"{k:12} : {v}")
        sys.exit(0)
    sys.exit(menu())
