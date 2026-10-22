@echo off
REM ============================================================
REM  AutoClaw 反代 · 管理面板
REM  双击打开数字菜单：启动 / 停止 / 状态 / 换 Key 等
REM ============================================================
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe

if not defined PY (
  echo.
  echo   没找到 Python，请先安装：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

"%PY%" oneclick.py
