@echo off
chcp 65001 >nul
rem ============================================================
rem  VERA 一键启动(回测 Web + 实盘交易两个进程)
rem  编码:本文件 UTF-8 + CRLF,首行 chcp 65001 切控制台,
rem  PYTHONIOENCODING 保证 Python 中文日志不乱码
rem ============================================================
set PYTHONIOENCODING=utf-8
cd /d %~dp0

rem ---- Python 定位(2026-09-16 修)------------------------------------------
rem 本机 python.exe 没进系统 PATH, 直接敲 python 会命中微软商店的 0 字节占位符
rem (报 "Python was not found"), 真实解释器在 D:\Program Files\Python313。
rem 这里把它排到 PATH 最前面, 下面 3 个 start 窗口继承同一份 PATH, 不再撞占位符。
set "PYDIR=D:\Program Files\Python313"
if not exist "%PYDIR%\python.exe" (
  echo [错误] 找不到 Python: "%PYDIR%\python.exe"
  echo        请改本文件顶部的 PYDIR, 或把 Python 目录加进系统 PATH。
  pause
  exit /b 1
)
set "PATH=%PYDIR%;%PYDIR%\Scripts;%PATH%"
echo [0/3] Python 解释器: %PYDIR%\python.exe

echo [1/3] 启动回测 Web (8080) ...
rem 默认稳定模式(2026-09-07 起): 代码改动不会自动重启, 防打断长回测/深度思考
rem 开发要热更时: 把下行改为 python server.py --reload
start "VERA-Web-8080" cmd /k python server.py

echo [2/3] 启动实盘交易 (8081) ...
rem 注意:交易进程需要 miniQMT 已登录运行
rem 测试模式(不下单,用 FakeGateway): 把下行改为 python trade_main.py --fake
start "VERA-Trade-8081" cmd /k python trade_main.py --config config/trade.yaml

echo [3/3] 启动定时调度 (舆情扫描/舆情日报/月度笔记/周度进化) ...
rem 舆情扫描+日报挂在 scheduler 进程, 不拉它就没有飞书推送 (2026-08-14 修复)
start "VERA-Scheduler" cmd /k python -m scheduler

echo.
echo 三个进程已在新窗口启动(窗口保留,报错可见):
echo   回测/选股/交易页:  http://localhost:8080
echo   交易进程 API:      http://localhost:8081
echo   定时调度(飞书舆情): python -m scheduler
echo.
echo 关闭对应窗口即停止对应进程。
if exist "%~dp0dsh-runtime\dsh.cmd" (echo [体检] DSH 深度思考通道: 已部署) else (echo [体检] DSH 深度思考通道: 未部署, 研究 TAB 勾选框不可用)
pause
