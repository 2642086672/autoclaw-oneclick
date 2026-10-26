@echo off
setlocal
set RELAY_DIR=C:\Users\Administrator\.autoclaw-relay
set NODE=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe
set PYW=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe
rem pythonw = GUI 子系统，不分配控制台 —— 换成 python.exe 每次拉起都会闪一个黑窗口
set PY=%PYW%
if not exist "%PY%" set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe
set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set all_proxy=
set ALL_PROXY=
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
set INBOUND_KEY=sk-ac-22cc095175e7ebe8c2f75e4d961eba025d7f7ddcd66cba64
cd /d "%RELAY_DIR%"

rem ── 参数（下面的检测要用，必须先赋值）──
set AUTOCLAW_BIND_HOST=127.0.0.1
set AUTOCLAW_PORT=18766
set AUTOCLAW_CONTRACT=2x
set AUTOCLAW_CLOUD_LANE=cn
set AUTOCLAW_CLIENT_VERSION=2.2.0.308
set AUTOCLAW_X_CHANNEL=official
set AUTOCLAW_X_TRACE_ID=autoclaw-desktop
set AUTOCLAW_HARNESS_MARKER=0
set AUTOCLAW_MAX_OUTPUT_TOKENS=0
set AUTOCLAW_INBOUND_KEYS=%INBOUND_KEY%
set AUTOCLAW_LOG_FILE=%RELAY_DIR%\server.log

rem ── 端口占用检查 ──
rem 少了这个判断，守护每次无脑调用都会再起一个 node：新实例 EADDRINUSE 自杀，
rem 多实例互相踩，反代就会莫名反复掉线。
rem 用 /C: 做字面匹配（/R 正则下 : 和空格会被当分隔符，匹配不稳）。
netstat -ano -p TCP | findstr /C:":%AUTOCLAW_PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [start-relay] 端口 %AUTOCLAW_PORT% 已在监听，只检查续期器
  goto :sync_only
)

rem ── 端口空闲：正常启动路径 ──
"%PY%" token-sync.py --once >nul 2>&1
:sync_only

rem ── 续期器：pid 还活着就别重复起 ──
set SPID=
if exist "%RELAY_DIR%\token-sync.pid" set /p SPID=<"%RELAY_DIR%\token-sync.pid"
if not defined SPID goto :start_sync
tasklist /FI "PID eq %SPID%" 2>nul | findstr /I "python" >nul 2>&1
if not errorlevel 1 goto :skip_sync
:start_sync
start "autoclaw-token-sync" /min "%PY%" token-sync.py
:skip_sync

rem ── 端口在监听就到此为止，不再起 node（避免 EADDRINUSE 自杀循环）──
netstat -ano -p TCP | findstr /C:":%AUTOCLAW_PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 exit /b 0

start "autoclaw-relay" /min "%NODE%" server.mjs
endlocal
