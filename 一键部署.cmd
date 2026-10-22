@echo off
REM ============================================================
REM  AutoClaw 反代一键部署
REM  双击这一件事就跑完：检查环境 -> 取凭证 -> 抽 persona
REM  -> 部署 -> 装开机自启 -> 起服务 -> 打印连接信息
REM ============================================================
cd /d "%~dp0"

REM 优先用系统里的 python，找不到再试常见路径
set PY=
where python >nul 2>&1 && set PY=python
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe

if not defined PY (
  echo.
  echo   没找到 Python，请先安装：https://www.python.org/downloads/
  echo   安装时记得勾选 "Add python.exe to PATH"
  echo.
  pause
  exit /b 1
)

"%PY%" oneclick.py --auto
echo.
pause
