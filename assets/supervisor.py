#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoClaw 反代守护（Python 版）。

为什么不用 .cmd / .ps1：
  1) 老的 supervisor.cmd 健康检测是
         netstat -ano | findstr /R /C:":18766 .*LISTENING"
     findstr 的 /C: 在 cmd 下被当成"从文件读"，它去找一个叫 `.*LISTENING"` 的文件，
     报 "Cannot open"，errorlevel 恒为 1。于是每 5 分钟无条件调一次 start-relay.cmd，
     而那个脚本当时也不查端口占用 → 新实例 EADDRINUSE 自杀 + 多实例互踩，
     表现就是反代莫名反复掉线。
  2) PowerShell 版在这个环境里进程会被反复替换掉（父会话回收），活不长。
     Python 的 token-sync.py 已经验证能长期驻留，所以守护也用 Python。

判据：直接 TcpClient 式连一次 127.0.0.1:18766。能建立连接就算活 ——
比解析 netstat 文本稳，不受 IPv4/IPv6 混排、端口出现在远端列等干扰。
"""

import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RELAY_DIR = Path(r"C:\Users\Administrator\.autoclaw-relay")
NODE = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
# 用 pythonw.exe（Windows GUI 子系统，不分配控制台）。
# 换成 python.exe 的话，每被拉起一次都会闪一个黑色控制台窗口 —— 用户就是为这个来的。
# pythonw 没有控制台，stdout/stderr 一律丢弃（重定向到 DEVNULL）。
PYW = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
if not os.path.isfile(PYW):
    PYW = r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
PY = PYW
PORT = 18766
INTERVAL = 30          # 秒。反代掉线时人是在用着的，5 分钟等于干等
LOG = RELAY_DIR / "keepalive.log"
PIDFILE = RELAY_DIR / "supervisor.pid"

# 探测/启动都不要走代理，否则 127.0.0.1 可能被系统代理截走
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    # 同时打到 stdout，前台调试时能直接看
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def port_alive(port: int = PORT, timeout: float = 1.5) -> bool:
    """连得上就算活。用 create_connection 而不是 netstat 文本匹配。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def pid_alive(pid: int) -> bool:
    """Windows 上判断 pid 存活：tasklist 按 pid 过滤，有输出即活。"""
    if not pid or pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
        return f'"{pid}"' in (r.stdout or "")
    except Exception:
        return False


def read_pid_file(p: Path):
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def detached_flags():
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    return 0x00000008 | 0x00000200 | 0x08000000


def start_relay() -> bool:
    """起反代。先刷新一次凭据，再拉 node。"""
    log("relay down -> 拉起")

    # 续期器先跑一次 --once，保证 request-headers.json 是最新的
    try:
        subprocess.run([PY, "token-sync.py", "--once"], cwd=str(RELAY_DIR),
                       capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
    except Exception as e:
        log(f"token-sync --once 失败：{e}")

    # 反代需要一堆环境变量，WMI 传不了 env，所以先写一个一次性 .cmd，
    # 再用 WMI 拉它 —— 这样既带上环境变量，又脱离当前进程树。
    key = ""
    key_file = RELAY_DIR / "api-key.txt"
    if key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
    launcher = RELAY_DIR / "_launch-relay.cmd"
    launcher.write_text(
        "@echo off\r\n"
        f"set RELAY_DIR={RELAY_DIR}\r\n"
        f"set NODE={NODE}\r\n"
        f"set AUTOCLAW_BIND_HOST=127.0.0.1\r\n"
        f"set AUTOCLAW_PORT={PORT}\r\n"
        "set AUTOCLAW_CONTRACT=2x\r\n"
        "set AUTOCLAW_CLOUD_LANE=cn\r\n"
        "set AUTOCLAW_CLIENT_VERSION=2.2.0.308\r\n"
        "set AUTOCLAW_X_CHANNEL=official\r\n"
        "set AUTOCLAW_X_TRACE_ID=autoclaw-desktop\r\n"
        "set AUTOCLAW_HARNESS_MARKER=0\r\n"
        "set AUTOCLAW_MAX_OUTPUT_TOKENS=0\r\n"
        f"set AUTOCLAW_INBOUND_KEYS={key}\r\n"
        f"set AUTOCLAW_LOG_FILE={RELAY_DIR}\\server.log\r\n"
        'set no_proxy=127.0.0.1,localhost\r\n'
        'set NO_PROXY=127.0.0.1,localhost\r\n'
        "cd /d \"%RELAY_DIR%\"\r\n"
        'start "" /min "%NODE%" server.mjs\r\n',
        encoding="utf-8")

    pid = _spawn_via_wmi(launcher, workdir=str(RELAY_DIR))
    if not pid:
        # WMI 失败时退回直接启动（至少有窗口但不至于起不来）
        log("WMI 拉起反代失败，退回 Popen")
        logf = open(RELAY_DIR / "server.log", "a", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "AUTOCLAW_BIND_HOST": "127.0.0.1",
            "AUTOCLAW_PORT": str(PORT),
            "AUTOCLAW_CONTRACT": "2x",
            "AUTOCLAW_CLOUD_LANE": "cn",
            "AUTOCLAW_CLIENT_VERSION": "2.2.0.308",
            "AUTOCLAW_X_CHANNEL": "official",
            "AUTOCLAW_X_TRACE_ID": "autoclaw-desktop",
            "AUTOCLAW_HARNESS_MARKER": "0",
            "AUTOCLAW_MAX_OUTPUT_TOKENS": "0",
            "AUTOCLAW_LOG_FILE": str(RELAY_DIR / "server.log"),
        })
        if key:
            env["AUTOCLAW_INBOUND_KEYS"] = key
        try:
            p = subprocess.Popen(
                [NODE, "server.mjs"], cwd=str(RELAY_DIR), env=env,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=detached_flags(), close_fds=True,
            )
            (RELAY_DIR / "relay.pid").write_text(str(p.pid), encoding="utf-8")
        finally:
            logf.close()

    # 等端口真的起来（最多 20 秒）
    for _ in range(40):
        if port_alive():
            log("relay 拉起成功")
            return True
        time.sleep(0.5)
    log("relay 拉起后仍未监听 —— 查 server.log")
    return False


def start_token_sync() -> None:
    log("token-sync down -> 拉起")
    try:
        p = subprocess.Popen(
            [PY, "token-sync.py"], cwd=str(RELAY_DIR),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=detached_flags(), close_fds=True,
        )
        (RELAY_DIR / "token-sync.pid").write_text(str(p.pid), encoding="utf-8")
    except Exception as e:
        log(f"token-sync 拉起失败：{e}")


# ---------------- AutoClaw 客户端守护 ----------------
# 凭据 account-credentials.enc 只有客户端自己会续。客户端一关，凭据到期就是 401，
# 反代这边再怎么重启也没用（只能读到那份旧的 enc）。所以客户端也得守着。
AUTOCLAW_EXE_CANDIDATES = [
    r"D:\autoclaw\AutoClaw2\AutoClaw2.exe",
    r"C:\Users\Administrator\AppData\Local\Programs\AutoClaw2\AutoClaw2.exe",
]
ENC_GLOB = r"C:\Users\Administrator\AppData\Roaming\AutoClaw-official\accounts"


def autoclaw_exe():
    for p in AUTOCLAW_EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def autoclaw_running() -> bool:
    """2.x 进程名是 AutoClaw2.exe，1.x 是 AutoClaw.exe，两个都认。"""
    for name in ("AutoClaw2.exe", "AutoClaw.exe"):
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
                creationflags=0x08000000,
            )
            if name.lower() in (r.stdout or "").lower():
                return True
        except Exception:
            pass
    return False


def _acquire_singleton_lock():
    """
    单实例保护：只允许一个 supervisor。

    为什么需要：计划任务会反复拉起 supervisor。多实例并存时会互相抢着拉
    token-sync / 客户端，每 30 秒就多一个进程 —— 每个都闪控制台窗口。
    用独占文件锁（O_EXCL|O_CREAT），已存在则说明有别的实例在跑。
    """
    try:
        fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 可能是上次异常退出留下的残锁。检查锁里记的 pid 是否还活着，
        # 死了就当作残锁清掉重来。
        try:
            old = LOCKFILE.read_text(encoding="utf-8").strip()
            old_pid = int(old)
        except Exception:
            return False
        if old_pid and old_pid != os.getpid() and _pid_exists(old_pid):
            return False
        # 残锁，清掉重试一次
        try:
            LOCKFILE.unlink()
        except Exception:
            return False
        try:
            fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _spawn_via_wmi(exe_path, args="", workdir=None):
    """
    用 WMI 拉起进程，脱离当前进程树。

    supervisor 自己也是被计划任务/会话拉起的，若用 Popen，子进程和 supervisor
    在同一个作业对象里 —— supervisor 被收走时，子进程跟着一起死。
    WMI 创建的进程挂在 wmiprvse.exe 名下，不受这个作业对象约束。
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


def _pid_exists(pid: int) -> bool:
    """按 pid 判断进程是否存在（用于识别残锁）。"""
    if not pid or pid <= 0:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        return f'"{pid}"' in (r.stdout or "")
    except Exception:
        return False


def start_autoclaw() -> None:
    exe = autoclaw_exe()
    if not exe:
        log("找不到 AutoClaw2.exe，跳过客户端拉起")
        return
    log(f"AutoClaw 客户端未运行 -> 拉起（{exe}）")
    try:
        subprocess.Popen([exe], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=detached_flags(), close_fds=True)
    except Exception as e:
        log(f"AutoClaw 拉起失败：{e}")


def token_seconds_left():
    """读 request-headers.json 里的 JWT exp，返回剩余秒数；读不到返回 None。"""
    import base64
    import json as _json
    try:
        h = _json.loads((Path(os.path.expanduser("~")) / ".openclaw-autoclaw"
                         / "request-headers.json").read_text(encoding="utf-8"))
        tok = (h.get("headers") or h)["X-Authorization"].split(" ", 1)[1]
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(seg))["exp"]
        return exp - time.time()
    except Exception:
        return None


def _acquire_singleton_lock() -> bool:
    """
    只允许一个 supervisor 实例。

    用 msvcrt 锁一个独占文件：谁拿到锁谁跑，其它实例直接退出。
    为什么必要：计划任务每 5 分钟触发一次，触发器叠加时可能并存多个 supervisor。
    多实例会互相抢着拉 token-sync → 每 30 秒重复启动一次 → 反复闪黑窗口。
    """
    import msvcrt
    lock_file = RELAY_DIR / "supervisor.lock"
    try:
        fh = open(lock_file, "w")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        # 保持文件句柄打开，锁就一直是我们的
        _SINGLETON_HANDLE.append(fh)
        return True
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return False


_SINGLETON_HANDLE = []


def refresh_credentials(force=False):
    """
    把 enc 里的新凭据搬到 request-headers.json。
    剩余 < 30 分钟（或 force）才动手，避免每轮都读 DPAPI。
    """
    left = token_seconds_left()
    if not force and left is not None and left > 1800:
        return
    try:
        r = subprocess.run([PY, "token-sync.py", "--once"], cwd=str(RELAY_DIR),
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace",
                           creationflags=0x08000000)
        msg = (r.stdout or r.stderr or "").strip().splitlines()
        log(f"凭据刷新：{msg[-1] if msg else '无输出'}")
    except Exception as e:
        log(f"凭据刷新失败：{e}")


def main() -> None:
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"supervisor 启动（Python 版，pid {os.getpid()}，端口 {PORT}，每 {INTERVAL}s 自查）")

    # 启动时先把凭据刷一遍，避免拿着过期 token 空转
    refresh_credentials(force=True)

    # 单实例保护：只允许一个 supervisor。
    # 计划任务每 5 分钟会再拉一个起来（任务设置是 IgnoreNew，但触发器叠加时
    # 仍可能并存）。多实例会互相抢着拉 token-sync → 每 30 秒弹一个黑窗口。
    if not _acquire_singleton_lock():
        log("已有 supervisor 在运行，本实例退出（避免重复拉起导致黑窗口）")
        return

    last_sync_at = 0.0

    while True:
        try:
            now = time.time()

            # 1) AutoClaw 客户端：它是凭据的唯一续期来源，必须活着
            if not autoclaw_running():
                start_autoclaw()
                time.sleep(15)   # 给客户端一点时间落 enc

            # 2) 凭据：剩余不足 30 分钟就搬一次
            refresh_credentials()

            # 3) 反代端口
            if not port_alive():
                start_relay()

            # 4) 续期器：拉起后至少等 2 分钟再复查（进程启动慢，立刻查会误判成死了）
            if (not pid_alive(read_pid_file(RELAY_DIR / "token-sync.pid"))
                    and now - last_sync_at > 120):
                last_sync_at = now
                start_token_sync()

        except Exception as e:
            # 守护自己不能死：任何异常都吞掉记日志，下一轮继续
            log(f"supervisor 循环异常（已忽略）：{e!r}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    # 单次模式，方便手工验证：python supervisor.py --once
    if "--once" in sys.argv:
        print("port_alive       :", port_alive())
        print("autoclaw_running :", autoclaw_running())
        print("token-sync pid   :", read_pid_file(RELAY_DIR / "token-sync.pid"),
              "alive:", pid_alive(read_pid_file(RELAY_DIR / "token-sync.pid")))
        left = token_seconds_left()
        print("token 剩余       :", f"{left/3600:.2f} 小时" if left is not None else "读取失败")
        sys.exit(0)
    main()
