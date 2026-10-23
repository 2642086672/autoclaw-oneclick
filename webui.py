# -*- coding: utf-8 -*-
"""
AutoClaw 反代 · 图形界面

    python webui.py          起服务并自动打开浏览器
    python webui.py --port 9000

纯 Python 标准库，不装任何第三方包。浏览器里填路径、点按钮即可。
所有配置存在 ~/.autoclaw-relay/config.json，下次打开自动带出来。
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
RELAY_DIR_DEFAULT = HOME / ".autoclaw-relay"
STATE_DIR = HOME / ".openclaw-autoclaw"
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
AUTOSTART_NAME = "AutoClaw-Relay-Supervisor.cmd"

UI_PORT = int(os.environ.get("AUTOCLAW_UI_PORT", "9090"))

# ---------------------------------------------------------------- 运行态
LOGS = []
LOG_LOCK = threading.Lock()
JOBS = {}          # name -> {"running": bool}
JOB_LOCK = threading.Lock()


def log(msg, level="info"):
    with LOG_LOCK:
        LOGS.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": str(msg)})
        del LOGS[:-800]          # 只留最近 800 条


def job_start(name):
    with JOB_LOCK:
        if JOBS.get(name, {}).get("running"):
            return False
        JOBS[name] = {"running": True}
        return True


def job_end(name):
    with JOB_LOCK:
        JOBS[name] = {"running": False}


def job_running(name):
    with JOB_LOCK:
        return JOBS.get(name, {}).get("running", False)


# ---------------------------------------------------------------- 配置
def cfg_path():
    return RELAY_DIR_DEFAULT / "config.json"


def load_cfg():
    p = cfg_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cfg(cfg):
    RELAY_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
    p = cfg_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- 探测
def find_node():
    for c in (os.environ.get("NODE_EXE"), shutil.which("node"),
              str(HOME / r".workbuddy\binaries\node\versions\22.22.2-6\node.exe"),
              r"C:\Program Files\nodejs\node.exe"):
        if c and Path(c).is_file():
            return c
        if c and shutil.which(c):
            return shutil.which(c)
    return ""


def find_python():
    for c in (sys.executable,
              str(HOME / r".workbuddy\binaries\python\envs\default\Scripts\python.exe"),
              shutil.which("python")):
        if c and Path(c).is_file():
            return c
    return sys.executable


def find_autoclaw():
    """扫常见安装位置，返回 main.cjs 路径（找不到给空串）。"""
    env = os.environ.get("AUTOCLAW_BUNDLE")
    if env and Path(env).is_file():
        return env
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             str(HOME / "AppData/Local/Programs"), "C:\\", "D:\\", "E:\\"]
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if "autoclaw" not in name.lower():
                continue
            base = Path(root) / name
            cands = [base] + [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
            for rel in (Path("resources/app/dist/main.cjs"), Path("resources/app.asar")):
                for c in cands:
                    p = c / rel
                    if p.is_file():
                        return str(p)
    return ""


def win_file_version(path):
    """
    读 exe 的 FileVersion（形如 2.2.0.308）。失败返回空串。

    注意：必须显式声明 argtypes/restype —— 64 位下不声明的话 ctypes 按 32 位
    传指针，VerQueryValueW 拿到的 lplp 会被截断，永远返回空。
    """
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version

        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            wintypes.DWORD, wintypes.LPVOID]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR,
                                       ctypes.POINTER(wintypes.LPVOID),
                                       ctypes.POINTER(wintypes.UINT)]
        ver.VerQueryValueW.restype = wintypes.BOOL

        p = str(path)
        size = ver.GetFileVersionInfoSizeW(p, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(p, 0, size, buf):
            return ""
        lplp = wintypes.LPVOID()
        ln = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(lplp), ctypes.byref(ln)):
            return ""

        class VFFI(ctypes.Structure):
            _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                        ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                        ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                        ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                        ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                        ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                        ("dwFileDateLS", wintypes.DWORD)]
        v = ctypes.cast(lplp, ctypes.POINTER(VFFI)).contents
        ms, ls = v.dwFileVersionMS, v.dwFileVersionLS
        s = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        return s if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", s) else ""
    except Exception:
        return ""


def probe_autoclaw_version(main_cjs):
    """
    反推客户端版本号。X-Version 必须**四段式**（如 2.2.0.308），
    写成三段式网关会拒，而且报的是误导性的 "Invalid token"，所以这里务必准确。
    """
    p = Path(main_cjs)
    # 主 exe 在 <app根>\<产品>.exe，main.cjs 在 <app根>\resources\app\dist\main.cjs。
    # 注意 dist\ 下自己也有个 resources\（放 bin/installer），按「有没有 resources 子目录」
    # 判断会误认 dist 为根 —— 必须按完整相对路径反推。
    approot = None
    rel = Path("resources") / "app" / "dist" / "main.cjs"
    for parent in p.parents:
        try:
            if (parent / rel).resolve() == p.resolve():
                approot = parent
                break
        except Exception:
            continue
    cands = []
    if approot:
        cands = [e for e in approot.glob("*.exe")]
    # 优先名字带 autoclaw 的；实在没有就退而求其次（只取 app 根下的）
    ordered = ([e for e in cands if "autoclaw" in e.name.lower()]
               or [e for e in cands if "unins" not in e.name.lower()]
               or cands)
    for exe in ordered:
        v = win_file_version(exe)
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v or ""):
            return v
    # 2) 退回 package.json 的 version（三段），补 .0 占位
    try:
        j = p.parent.parent / "package.json"
        if j.is_file():
            v = str(json.loads(j.read_text(encoding="utf-8", errors="replace")).get("version", "")).strip()
            if re.fullmatch(r"\d+\.\d+\.\d+", v):
                return v + ".0"
    except Exception:
        pass
    return "2.2.0.308"


# ---------------------------------------------------------------- 进程
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def clean_env():
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def spawn_detached(cmd, cwd=None):
    devnull = open(os.devnull, "w")
    try:
        return subprocess.Popen(cmd, cwd=cwd, env=clean_env(),
                                stdin=devnull, stdout=devnull, stderr=devnull,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                                close_fds=True)
    finally:
        devnull.close()


def spawn_wmi(cmdline):
    """
    WMI Win32_Process.Create 拉进程，返回 pid（失败 None）。

    DETACHED_PROCESS 只脱离控制台、不脱离作业对象(Job Object)：本服务如果是被
    IDE / 终端 / 调度器以作业方式拉起的，它 Popen 出来的子进程仍在同一个作业里，
    外层一收摊整棵树就被终止（症状：start 成功、几秒后端口没了）。
    WMI 创建的进程挂在 wmiprvse.exe 名下，不属于我们的作业，能真正活下来。
    """
    q = "'" + cmdline.replace("'", "''") + "'"
    ps = ("$r = ([wmiclass]'root\\cimv2:Win32_Process').Create(%s); "
          "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { -1 }") % q
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=90, env=clean_env())
        pid = int((r.stdout or "-1").strip().splitlines()[-1])
    except Exception:
        return None
    return pid if pid > 0 else None


def start_relay(relay_dir, port):
    """拉起反代；常规 Popen 起不来时自动换 WMI 重投。返回是否成功。"""
    launcher = str(relay_dir / "start-relay.cmd")
    spawn_detached(["cmd.exe", "/c", launcher], cwd=str(relay_dir))
    for _ in range(20):
        if port_listening(port):
            return True
        time.sleep(0.5)
    pid = spawn_wmi('cmd.exe /c "%s"' % launcher)
    if pid:
        for _ in range(40):
            if port_listening(port):
                return True
            time.sleep(0.5)
    return port_listening(port)


def run(cmd, timeout=180):
    try:
        return subprocess.run(cmd, env=clean_env(), capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except Exception as e:
        class R: returncode, stdout, stderr = 1, "", str(e)
        return R()


def port_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.2)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------- 流程
def do_deploy(cfg):
    """完整部署流水线。cfg 来自前端表单。"""
    relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
    node = cfg.get("node_path") or find_node()
    py = cfg.get("python_path") or find_python()
    bundle = cfg.get("autoclaw_path") or find_autoclaw()
    port = int(cfg.get("port") or 18766)
    lane = cfg.get("lane") or "cn"
    version = cfg.get("client_version") or "2.2.0.308"
    key = (cfg.get("api_key") or "").strip()
    if not key:
        key = "sk-ac-" + secrets.token_hex(24)

    relay_dir.mkdir(parents=True, exist_ok=True)
    log(f"运行目录：{relay_dir}", "dim")

    # 1 环境
    log("── 1/6 检查环境 ──", "head")
    if not node or not Path(node).is_file():
        log("没找到 Node.js。去 https://nodejs.org 装一个 20 以上版本。", "err")
        return False
    log(f"Node.js  {run([node, '-v']).stdout.strip()}  {node}", "ok")
    if not py or not Path(py).is_file():
        log("没找到 Python。去 https://python.org 装 3.10 以上。", "err")
        return False
    log(f"Python   {run([py, '-c', 'import sys;print(sys.version.split()[0])']).stdout.strip()}  {py}", "ok")
    r = run([py, "-c", "import cryptography;print(cryptography.__version__)"])
    if r.returncode != 0:
        log("缺 cryptography，正在安装...", "warn")
        r2 = run([py, "-m", "pip", "install", "cryptography"], timeout=600)
        if r2.returncode == 0:
            log("cryptography 已安装", "ok")
        else:
            log("cryptography 装不上，手动跑：pip install cryptography", "err")
            return False
    else:
        log(f"cryptography {r.stdout.strip()}", "ok")
    if not bundle or not Path(bundle).is_file():
        log("找不到 AutoClaw 客户端（main.cjs）。在上面「AutoClaw 安装位置」里填对路径。", "err")
        return False
    log(f"AutoClaw  {bundle}", "ok")

    # 2 凭证
    log("── 2/6 取账号凭证 ──", "head")
    if not (ASSETS / "make_compat_auth.py").is_file():
        log("assets/make_compat_auth.py 缺失，工具文件不全。", "err")
        return False
    env = clean_env()
    env["AUTOCLAW_BUNDLE"] = bundle
    r = subprocess.run([py, str(ASSETS / "make_compat_auth.py")], capture_output=True, text=True,
                       cwd=str(ASSETS), env=env, timeout=180, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log(f"解密失败：{(r.stderr or r.stdout or '')[-300:]}", "err")
        log("提示：先打开 AutoClaw 登录一次，再重新部署。", "warn")
        return False
    auth_file = relay_dir / "auth-compat" / "auth.json"
    if not auth_file.is_file():
        log("auth-compat/auth.json 没生成。", "err")
        return False
    tok = (json.loads(auth_file.read_text(encoding="utf-8")).get("token") or "")
    if not tok:
        log("凭证里没有 token。", "err")
        return False
    if not tok.startswith("Bearer "):
        tok = "Bearer " + tok
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    hf = STATE_DIR / "request-headers.json"
    tmp = hf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"headers": {"X-Authorization": tok, "X-Client-Type": "pc"},
                               "sessionHeaders": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, hf)
    log(f"凭证已解密（token {len(tok)} 字符）", "ok")

    # 3 persona
    log("── 3/6 抽取 persona ──", "head")
    env["NODE_EXE"] = node
    out = relay_dir / "persona.txt"
    r = subprocess.run([py, str(ASSETS / "persona_extract.py"), "--out", str(out)],
                       capture_output=True, text=True, env=env, timeout=240,
                       encoding="utf-8", errors="replace")
    try:
        j = json.loads(r.stdout or "{}")
    except Exception:
        j = {}
    if j.get("ok"):
        log(f"persona 已抽取：{j.get('source', '')}  长度 {j.get('len')}", "ok")
    elif out.is_file():
        log("本次抽取失败，沿用已有的 persona.txt", "warn")
    else:
        log(f"persona 抽取失败：{j.get('error') or (r.stderr or '')[-200:]}", "err")
        return False

    # 4 部署
    log("── 4/6 部署反代 ──", "head")
    shutil.copy2(ASSETS / "server_2x.mjs", relay_dir / "server.mjs")
    log(f"已写入 {relay_dir / 'server.mjs'}", "ok")

    # 5 脚本与自启
    log("── 5/6 写配置与守护脚本 ──", "head")
    (relay_dir / "a_switch.py").write_text(
        (ASSETS / "a_switch.py").read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    (relay_dir / "token-sync.py").write_text(TOKEN_SYNC_SRC, encoding="utf-8")
    (relay_dir / "api-key.txt").write_text(key, encoding="utf-8")

    common = (f'set NODE={node}\r\nset PY={py}\r\n'
              'set http_proxy=\r\nset https_proxy=\r\nset HTTP_PROXY=\r\nset HTTPS_PROXY=\r\n'
              'set all_proxy=\r\nset ALL_PROXY=\r\n'
              'set no_proxy=127.0.0.1,localhost\r\nset NO_PROXY=127.0.0.1,localhost\r\n')
    (relay_dir / "start-relay.cmd").write_text(
        "@echo off\r\nsetlocal\r\n"
        f"set RELAY_DIR={relay_dir}\r\n" + common +
        f"set INBOUND_KEY={key}\r\n"
        'cd /d "%RELAY_DIR%"\r\n'
        '"%PY%" token-sync.py --once >nul 2>&1\r\n'
        'start "autoclaw-token-sync" /min "%PY%" token-sync.py\r\n'
        f"set AUTOCLAW_BIND_HOST=127.0.0.1\r\nset AUTOCLAW_PORT={port}\r\n"
        "set AUTOCLAW_CONTRACT=2x\r\n"
        f"set AUTOCLAW_CLOUD_LANE={lane}\r\n"
        f"set AUTOCLAW_CLIENT_VERSION={version}\r\n"
        "set AUTOCLAW_X_CHANNEL=official\r\nset AUTOCLAW_X_TRACE_ID=autoclaw-desktop\r\n"
        "set AUTOCLAW_HARNESS_MARKER=0\r\nset AUTOCLAW_MAX_OUTPUT_TOKENS=0\r\n"
        "set AUTOCLAW_INBOUND_KEYS=%INBOUND_KEY%\r\n"
        'set AUTOCLAW_LOG_FILE=%RELAY_DIR%\\server.log\r\n'
        'start "autoclaw-relay" /min "%NODE%" server.mjs\r\n'
        "endlocal\r\n", encoding="utf-8")
    (relay_dir / "supervisor.cmd").write_text(
        "@echo off\r\nsetlocal\r\n"
        f"set RELAY_DIR={relay_dir}\r\n" + common +
        "title AutoClaw 反代守护\r\n"
        "echo AutoClaw 反代守护运行中（每 5 分钟自查，关窗口即停）\r\n"
        ":loop\r\n"
        f'  netstat -ano | findstr /R /C:":{port} .*LISTENING" >nul 2>&1\r\n'
        "  if errorlevel 1 (\r\n"
        '    echo [%time%] 反代未运行，拉起中...\r\n'
        '    echo [%date% %time%] relay down >> "%RELAY_DIR%\\keepalive.log"\r\n'
        '    call "%RELAY_DIR%\\start-relay.cmd" >nul 2>&1\r\n'
        "  )\r\n"
        "  set SPID=\r\n"
        '  if exist "%RELAY_DIR%\\token-sync.pid" set /p SPID=<"%RELAY_DIR%\\token-sync.pid"\r\n'
        "  if not defined SPID goto :start_sync\r\n"
        '  tasklist /FI "PID eq %SPID%" 2>nul | findstr /I "python" >nul 2>&1\r\n'
        "  if errorlevel 1 goto :start_sync\r\n"
        "  goto :sleep\r\n"
        "  :start_sync\r\n"
        '  echo [%time%] 续期器未运行，拉起中...\r\n'
        '  echo [%date% %time%] token-sync down >> "%RELAY_DIR%\\keepalive.log"\r\n'
        '  cd /d "%RELAY_DIR%"\r\n'
        '  start "autoclaw-token-sync" /min "%PY%" token-sync.py\r\n'
        "  :sleep\r\n"
        "  timeout /t 300 /nobreak >nul\r\n"
        "  goto :loop\r\n"
        "endlocal\r\n", encoding="utf-8")
    log("已写 start-relay.cmd / supervisor.cmd / token-sync.py", "ok")

    if cfg.get("autostart", True):
        try:
            STARTUP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(relay_dir / "supervisor.cmd", STARTUP_DIR / AUTOSTART_NAME)
            log(f"开机自启已装：{STARTUP_DIR / AUTOSTART_NAME}", "ok")
        except Exception as e:
            log(f"开机自启装不上：{e}", "warn")
    else:
        log("未安装开机自启（按你的选择）", "dim")

    # 6 启动
    log("── 6/6 启动服务 ──", "head")
    if port_listening(port):
        log(f"端口 {port} 已在监听，跳过启动", "warn")
    else:
        if not start_relay(relay_dir, port):
            log(f"启动超时，看日志 {relay_dir / 'server.log'}", "err")
            return False
        log(f"反代已启动（端口 {port}）", "ok")

    log("", "dim")
    log("════════ 部署完成 ════════", "head")
    log(f"OpenAI   baseUrl : http://127.0.0.1:{port}/v1", "info")
    log(f"Anthropic baseUrl: http://127.0.0.1:{port}", "info")
    log(f"API Key          : {key}", "info")
    return True


def do_stop(cfg):
    relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
    port = int(cfg.get("port") or 18766)
    killed = []
    r = run(["netstat", "-ano"], timeout=40)
    for line in (r.stdout or "").splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            run(["taskkill", "/F", "/PID", pid], timeout=30)
            killed.append(f"relay pid {pid}")
    pf = relay_dir / "token-sync.pid"
    if pf.is_file():
        pid = pf.read_text(encoding="utf-8").strip()
        if pid:
            run(["taskkill", "/F", "/PID", pid], timeout=30)
            killed.append(f"token-sync pid {pid}")
        try:
            pf.unlink()
        except Exception:
            pass
    log("已停止：" + (", ".join(killed) if killed else "本来就没在跑"), "ok")
    return True


def do_refresh_creds(cfg):
    """只刷凭证，不动别的。"""
    py = cfg.get("python_path") or find_python()
    bundle = cfg.get("autoclaw_path") or find_autoclaw()
    relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
    env = clean_env()
    env["AUTOCLAW_BUNDLE"] = bundle
    r = subprocess.run([py, str(ASSETS / "make_compat_auth.py")], capture_output=True, text=True,
                       cwd=str(ASSETS), env=env, timeout=180, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log(f"刷新失败：{(r.stderr or r.stdout or '')[-300:]}", "err")
        return False
    auth_file = relay_dir / "auth-compat" / "auth.json"
    if not auth_file.is_file():
        log("auth.json 没生成", "err")
        return False
    tok = (json.loads(auth_file.read_text(encoding="utf-8")).get("token") or "")
    if tok and not tok.startswith("Bearer "):
        tok = "Bearer " + tok
    if not tok:
        log("凭证里没有 token", "err")
        return False
    hf = STATE_DIR / "request-headers.json"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = hf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"headers": {"X-Authorization": tok, "X-Client-Type": "pc"},
                               "sessionHeaders": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, hf)
    log("凭证已刷新", "ok")
    return True


def current_status(cfg):
    relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
    port = int(cfg.get("port") or 18766)
    st = {"relay": "运行中" if port_listening(port) else "未运行",
          "port": port,
          "autostart": "已装" if (STARTUP_DIR / AUTOSTART_NAME).is_file() else "未装",
          "key": (relay_dir / "api-key.txt").read_text(encoding="utf-8").strip()
                 if (relay_dir / "api-key.txt").is_file() else "",
          "upstream": "", "uptime": "", "token_left": ""}
    if st["relay"] == "运行中":
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f"http://127.0.0.1:{port}/health", timeout=6) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            st["upstream"] = j.get("cloud", "")
            st["uptime"] = f"{int(j.get('uptime_s', 0)) // 60} 分钟"
        except Exception as e:
            st["upstream"] = f"（读不到：{e}）"
    hf = STATE_DIR / "request-headers.json"
    if hf.is_file():
        try:
            tok = json.loads(hf.read_text(encoding="utf-8"))["headers"]["X-Authorization"]
            raw = tok.split(" ", 1)[-1]
            part = raw.split(".")[1] + "=" * (-(len(raw.split(".")[1])) % 4)
            exp = int(json.loads(__import__("base64").urlsafe_b64decode(part)).get("exp", 0))
            left = exp - time.time()
            st["token_left"] = (f"{left / 3600:.1f} 小时" if left > 0 else "已过期")
        except Exception:
            st["token_left"] = "（读不到）"
    return st


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/config":
            cfg = load_cfg()
            cfg.setdefault("node_path", find_node())
            cfg.setdefault("python_path", find_python())
            cfg.setdefault("autoclaw_path", find_autoclaw())
            cfg.setdefault("relay_dir", str(RELAY_DIR_DEFAULT))
            cfg.setdefault("port", 18766)
            cfg.setdefault("lane", "cn")
            cfg.setdefault("client_version", probe_autoclaw_version(cfg["autoclaw_path"]))
            cfg.setdefault("autostart", True)
            cfg.setdefault("api_key", "")
            self._json(cfg)
        elif self.path == "/api/status":
            self._json(current_status(load_cfg()))
        elif self.path.startswith("/api/log"):
            since = int(self.path.split("since=")[-1]) if "since=" in self.path else 0
            with LOG_LOCK:
                items = LOGS[since:]
                total = len(LOGS)
            self._json({"items": items, "next": total})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {}

        if self.path == "/api/config":
            cfg = load_cfg()
            for k in ("autoclaw_path", "node_path", "python_path", "relay_dir", "port",
                      "lane", "client_version", "api_key", "autostart"):
                if k in body:
                    cfg[k] = body[k]
            save_cfg(cfg)
            log("配置已保存", "ok")
            self._json({"ok": True})

        elif self.path == "/api/deploy":
            cfg = load_cfg()
            cfg.update({k: v for k, v in body.items() if k in
                        ("autoclaw_path", "node_path", "python_path", "relay_dir", "port",
                         "lane", "client_version", "api_key", "autostart")})
            save_cfg(cfg)
            if not job_start("deploy"):
                self._json({"ok": False, "err": "已有部署在跑"}, 409)
                return
            self._json({"ok": True})

            def work():
                log("", "dim")
                log("开始部署…", "head")
                try:
                    ok = do_deploy(cfg)
                    if ok:
                        log("全部完成。", "ok")
                    else:
                        log("部署中断，看上面的红字。", "err")
                except Exception as e:
                    log(f"出错：{type(e).__name__}: {e}", "err")
                finally:
                    job_end("deploy")

            threading.Thread(target=work, daemon=True).start()

        elif self.path == "/api/start":
            cfg = load_cfg()
            if not job_start("start"):
                self._json({"ok": False, "err": "正在启动"}, 409)
                return
            self._json({"ok": True})

            def work():
                port = int(cfg.get("port") or 18766)
                relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
                if port_listening(port):
                    log(f"端口 {port} 已在监听", "warn")
                else:
                    start_relay(relay_dir, port)
                    log(f"反代 {'已启动' if port_listening(port) else '启动失败'}（端口 {port}）",
                        "ok" if port_listening(port) else "err")
                job_end("start")

            threading.Thread(target=work, daemon=True).start()

        elif self.path == "/api/stop":
            cfg = load_cfg()
            self._json({"ok": True})
            threading.Thread(target=lambda: (do_stop(cfg), job_end("stop")), daemon=True).start()

        elif self.path == "/api/refresh":
            cfg = load_cfg()
            if not job_start("refresh"):
                self._json({"ok": False, "err": "正在刷新"}, 409)
                return
            self._json({"ok": True})

            def work():
                do_refresh_creds(cfg)
                job_end("refresh")

            threading.Thread(target=work, daemon=True).start()

        elif self.path == "/api/newkey":
            key = "sk-ac-" + secrets.token_hex(24)
            cfg = load_cfg()
            cfg["api_key"] = key
            save_cfg(cfg)
            relay_dir = Path(cfg.get("relay_dir") or RELAY_DIR_DEFAULT)
            relay_dir.mkdir(parents=True, exist_ok=True)
            (relay_dir / "api-key.txt").write_text(key, encoding="utf-8")
            log(f"已生成新 Key：{key}", "ok")
            log("要让它生效：保存配置后重新部署一次。", "warn")
            self._json({"ok": True, "key": key})

        elif self.path == "/api/autostart":
            on = bool(body.get("on"))
            try:
                if on:
                    shutil.copy2(Path(cfg_of(body).get("relay_dir") or RELAY_DIR_DEFAULT) / "supervisor.cmd",
                                 STARTUP_DIR / AUTOSTART_NAME)
                    log("开机自启已装", "ok")
                else:
                    p = STARTUP_DIR / AUTOSTART_NAME
                    if p.is_file():
                        p.unlink()
                    log("开机自启已移除", "ok")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "err": str(e)}, 500)
        else:
            self._send(404, "not found", "text/plain")


def cfg_of(_=None):
    return load_cfg()


# ---------------------------------------------------------------- 页面
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoClaw 反代 · 设置</title>
<style>
:root{
  --bg:#f6f7f9; --card:#fff; --line:#e4e6ea; --text:#1c1e23; --muted:#6b7280;
  --brand:#2563eb; --brand-h:#1d4ed8; --ok:#16a34a; --warn:#d97706; --err:#dc2626;
  --code-bg:#f1f3f5; --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:28px 18px 60px}
header{margin-bottom:22px}
h1{font-size:23px;margin:0 0 6px;font-weight:650;letter-spacing:-.2px}
.sub{color:var(--muted);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px 22px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 16px;font-weight:620;display:flex;align-items:center;gap:8px}
.card h2 .n{width:22px;height:22px;border-radius:6px;background:var(--brand);color:#fff;
  font-size:12px;display:grid;place-items:center;font-weight:700;flex:none}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px}
.field{display:flex;flex-direction:column;gap:6px}
.field.full{grid-column:1/-1}
label{font-size:13px;color:var(--muted);font-weight:500}
input[type=text],input[type=number],select{
  width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
  font-size:14px;font-family:inherit;background:#fff;color:var(--text);transition:border .15s,box-shadow .15s}
input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.13)}
input::placeholder{color:#aab0b8}
.row{display:flex;gap:8px;align-items:center}
.row input{flex:1}
.btn{padding:9px 15px;border:1px solid var(--line);background:#fff;color:var(--text);
  border-radius:8px;font-size:13.5px;cursor:pointer;white-space:nowrap;
  transition:background .15s,border-color .15s;font-family:inherit}
.btn:hover{background:#f3f4f6}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:560}
.btn.primary:hover{background:var(--brand-h)}
.btn.danger{color:var(--err);border-color:#f3c5c5}
.btn.danger:hover{background:#fdf2f2}
.btn:disabled{opacity:.5;cursor:not-allowed}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px}
.hint{font-size:12.5px;color:var(--muted);margin-top:2px}
.check{display:flex;align-items:center;gap:9px;font-size:14px;cursor:pointer}
.check input{width:16px;height:16px;cursor:pointer}
.conn{display:grid;grid-template-columns:auto 1fr;gap:9px 16px;font-size:14px;align-items:center}
.conn .k{color:var(--muted);font-size:13px;white-space:nowrap}
.conn .v{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;
  background:var(--code-bg);padding:7px 11px;border-radius:7px;word-break:break-all;
  display:flex;align-items:center;gap:8px}
.copy{margin-left:auto;padding:3px 9px;font-size:11.5px;border:1px solid var(--line);
  border-radius:6px;background:#fff;cursor:pointer;color:var(--muted);flex:none}
.copy:hover{background:#eef1f4;color:var(--text)}
.badge{padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:560}
.badge.on{background:#e7f7ec;color:var(--ok)}
.badge.off{background:#fdeaea;color:var(--err)}
.logbox{background:#14161a;color:#d7dae0;border-radius:9px;padding:13px 15px;
  font:12.5px/1.75 ui-monospace,Menlo,Consolas,monospace;height:330px;overflow:auto;
  white-space:pre-wrap;word-break:break-all}
.logbox .t{color:#6b7280}
.logbox .ok{color:#6ee7a0}
.logbox .warn{color:#fbbf5b}
.logbox .err{color:#ff8181}
.logbox .head{color:#7cb7ff;font-weight:600}
.logbox .dim{color:#6b7280}
.toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,20px);
  background:#1c1e23;color:#fff;padding:11px 20px;border-radius:9px;font-size:13.5px;
  opacity:0;transition:.25s;pointer-events:none;z-index:99;max-width:88vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
@media(max-width:640px){
  .wrap{padding:18px 12px 40px}
  .grid{grid-template-columns:1fr}
  .conn{grid-template-columns:1fr}
  .conn .v{font-size:12px}
  .actions .btn{flex:1 1 calc(50% - 5px)}
  .logbox{height:250px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AutoClaw 反代</h1>
    <div class="sub">把 AutoClaw 的模型额度变成本地接口，任何 AI 工具都能用。填好路径点「开始部署」。</div>
  </header>

  <div class="card">
    <h2><span class="n">1</span>路径设置</h2>
    <div class="grid">
      <div class="field full">
        <label>AutoClaw 安装位置（main.cjs 或 app.asar）</label>
        <input type="text" id="autoclaw_path" placeholder="D:\autoclaw\AutoClaw2\resources\app\dist\main.cjs">
        <div class="hint">自动探测到了，不对就改成你自己的路径。找不到会报「找不到 AutoClaw 客户端」。</div>
      </div>
      <div class="field">
        <label>Node.js</label>
        <input type="text" id="node_path" placeholder="C:\Program Files\nodejs\node.exe">
      </div>
      <div class="field">
        <label>Python</label>
        <input type="text" id="python_path" placeholder="C:\Python313\python.exe">
      </div>
    </div>
  </div>

  <div class="card">
    <h2><span class="n">2</span>服务设置</h2>
    <div class="grid">
      <div class="field">
        <label>运行目录</label>
        <input type="text" id="relay_dir" placeholder="%USERPROFILE%\.autoclaw-relay">
      </div>
      <div class="field">
        <label>监听端口</label>
        <input type="number" id="port" value="18766">
      </div>
      <div class="field">
        <label>线路</label>
        <select id="lane">
          <option value="cn">国内版（zhipuai.cn）</option>
          <option value="oversea">国际版（autoglm.ai）</option>
        </select>
      </div>
      <div class="field">
        <label>客户端版本号（X-Version，四段式）</label>
        <input type="text" id="client_version" placeholder="2.2.0.308">
      </div>
      <div class="field full">
        <label>API Key（留空则自动生成）</label>
        <div class="row">
          <input type="text" id="api_key" placeholder="sk-ac-... 留空自动生成">
          <button class="btn" onclick="newKey()">生成新的</button>
        </div>
        <div class="hint">这是你自己的钥匙，填进 AI 工具里用。别发给别人。</div>
      </div>
      <div class="field full">
        <label class="check"><input type="checkbox" id="autostart" checked> 装开机自启（服务挂了每 5 分钟自动拉起）</label>
      </div>
    </div>
    <div class="actions">
      <button class="btn primary" id="btnDeploy" onclick="deploy()">开始部署</button>
      <button class="btn" onclick="saveCfg()">保存配置</button>
    </div>
  </div>

  <div class="card">
    <h2><span class="n">3</span>连接信息</h2>
    <div class="conn">
      <div class="k">OpenAI 接口</div>
      <div class="v"><span id="cOpenai">http://127.0.0.1:18766/v1</span>
        <button class="copy" onclick="copy('cOpenai')">复制</button></div>
      <div class="k">Claude 接口</div>
      <div class="v"><span id="cAnth">http://127.0.0.1:18766</span>
        <button class="copy" onclick="copy('cAnth')">复制</button></div>
      <div class="k">API Key</div>
      <div class="v"><span id="cKey">—</span><button class="copy" onclick="copy('cKey')">复制</button></div>
      <div class="k">服务状态</div>
      <div class="v" style="font-family:inherit"><span id="cRelay" class="badge off">未运行</span>
        <span id="cUpstream" style="color:var(--muted);font-size:12.5px"></span></div>
      <div class="k">凭证剩余</div>
      <div class="v" style="font-family:inherit"><span id="cToken">—</span></div>
    </div>
    <div class="actions">
      <button class="btn" onclick="startSvc()">启动</button>
      <button class="btn danger" onclick="stopSvc()">停止</button>
      <button class="btn" onclick="refreshCreds()">刷新凭证</button>
      <button class="btn" onclick="loadStatus()">刷新状态</button>
    </div>
  </div>

  <div class="card">
    <h2><span class="n">4</span>运行日志</h2>
    <div class="logbox" id="logbox"><span class="dim">等待操作…</span></div>
    <div class="actions"><button class="btn" onclick="clearLog()">清屏</button></div>
  </div>

  <footer>AutoClaw 反代 · 本地运行，凭证不出这台电脑</footer>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let logNext = 0;

function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(()=>t.classList.remove('show'), 2600);
}

async function api(path, method='GET', body){
  const opt = {method, headers:{'Content-Type':'application/json'}};
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  return r.json();
}

function collect(){
  return {
    autoclaw_path: $('autoclaw_path').value.trim(),
    node_path: $('node_path').value.trim(),
    python_path: $('python_path').value.trim(),
    relay_dir: $('relay_dir').value.trim(),
    port: parseInt($('port').value) || 18766,
    lane: $('lane').value,
    client_version: $('client_version').value.trim(),
    api_key: $('api_key').value.trim(),
    autostart: $('autostart').checked,
  };
}

async function loadCfg(){
  const c = await api('/api/config');
  $('autoclaw_path').value = c.autoclaw_path || '';
  $('node_path').value = c.node_path || '';
  $('python_path').value = c.python_path || '';
  $('relay_dir').value = c.relay_dir || '';
  $('port').value = c.port || 18766;
  $('lane').value = c.lane || 'cn';
  $('client_version').value = c.client_version || '';
  $('api_key').value = c.api_key || '';
  $('autostart').checked = c.autostart !== false;
  $('cOpenai').textContent = 'http://127.0.0.1:' + ($('port').value || 18766) + '/v1';
  $('cAnth').textContent  = 'http://127.0.0.1:' + ($('port').value || 18766);
}

async function saveCfg(){
  await api('/api/config','POST',collect());
  toast('配置已保存');
}

async function deploy(){
  $('btnDeploy').disabled = true;
  toast('部署开始，看下面日志');
  try{ await api('/api/deploy','POST',collect()); }
  catch(e){ toast('请求失败：'+e); }
  setTimeout(()=>{ $('btnDeploy').disabled = false; }, 1500);
}

async function startSvc(){ await api('/api/start','POST',collect()); toast('正在启动…'); }
async function stopSvc(){ await api('/api/stop','POST',collect()); toast('正在停止…'); }
async function refreshCreds(){ await api('/api/refresh','POST',collect()); toast('正在刷新凭证…'); }

async function newKey(){
  const r = await api('/api/newkey','POST',{});
  if (r.key){ $('api_key').value = r.key; toast('已生成新 Key'); }
}

function clearLog(){ $('logbox').innerHTML = '<span class="dim">已清屏</span>'; logNext = 0; }

function copy(id){
  const txt = $(id).textContent;
  navigator.clipboard?.writeText(txt).then(()=>toast('已复制')).catch(()=>{
    const ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove(); toast('已复制');
  });
}

async function pollLog(){
  try{
    const r = await api('/api/log?since=' + logNext);
    if (r.items && r.items.length){
      const box = $('logbox');
      if (logNext === 0) box.innerHTML = '';
      for (const it of r.items){
        const div = document.createElement('div');
        div.className = it.level || '';
        div.textContent = (it.t ? it.t + '  ' : '') + it.msg;
        box.appendChild(div);
      }
      logNext = r.next;
      box.scrollTop = box.scrollHeight;
    }
  }catch(e){}
}

async function loadStatus(){
  try{
    const s = await api('/api/status');
    const el = $('cRelay');
    el.textContent = s.relay;
    el.className = 'badge ' + (s.relay === '运行中' ? 'on' : 'off');
    $('cUpstream').textContent = s.upstream ? ('　' + s.upstream) : '';
    $('cToken').textContent = s.token_left || '—';
    if (s.key) $('cKey').textContent = s.key;
  }catch(e){}
}

$('port').addEventListener('input', ()=>{
  $('cOpenai').textContent = 'http://127.0.0.1:' + ($('port').value || 18766) + '/v1';
  $('cAnth').textContent  = 'http://127.0.0.1:' + ($('port').value || 18766);
});

loadCfg().then(()=>{ loadStatus(); });
setInterval(pollLog, 900);
setInterval(loadStatus, 6000);
</script>
</body>
</html>"""


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


def main():
    port = UI_PORT
    for a in sys.argv[1:]:
        if a.startswith("--port"):
            if "=" in a:
                port = int(a.split("=", 1)[1])
            else:
                port = int(sys.argv[sys.argv.index(a) + 1])

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"AutoClaw 反代设置页： {url}")
    print("浏览器没自动打开的话，手动复制上面的地址。关掉本窗口即退出设置页。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
