@echo off
chcp 65001
title Cookie 采集同步工具
cd /d "%~dp0"

rem ---- 找 Python：先用现成的 python 真跑一次，不行再找 py 启动器
set "PY=python"
python -c "import sys" || (
    set "PY="
    where py && set "PY=py -3"
)
if not defined PY (
    echo [x] 未检测到可用的 Python。请安装 Python 3.10+: https://www.python.org/downloads/
    echo     安装时勾选 "Add Python to PATH"，然后重新运行本脚本。
    pause
    exit /b 1
)

rem ---- 首次运行：建独立虚拟环境（不污染系统 Python）
set "VENV=..\data\.venv_collector"
if not exist "%VENV%\Scripts\python.exe" (
    echo [首次运行] 创建独立环境并安装依赖（约 2-5 分钟，取决于网速）...
    %PY% -m venv "%VENV%"
    "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
    "%VENV%\Scripts\python.exe" -m pip install -q playwright requests
    "%VENV%\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 (
        echo [x] 依赖安装失败，请检查网络后重新运行本脚本。
        pause
        exit /b 1
    )
)
echo [√] 环境就绪，启动采集器...

rem ---- 运行采集器（参数原样透传，支持 -p douyin --push 等）
"%VENV%\Scripts\python.exe" cookie_collector.py %*
echo.
pause
