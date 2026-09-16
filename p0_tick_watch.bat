@echo off
chcp 65001 >nul
rem ============================================================
rem  P0 订阅腿验证(盘中运行, 只读不下单)
rem  前提: miniQMT 已登录, 当前在交易时段(9:30-11:30 / 13:00-15:00)
rem  观察 60 秒行情推送, 验证订阅通道是否通
rem ============================================================
set PYTHONIOENCODING=utf-8
cd /d %~dp0

rem ---- Python 定位(2026-09-16 修): 详见 start_vera.bat 顶部说明 ----------
set "PYDIR=D:\Program Files\Python313"
if not exist "%PYDIR%\python.exe" (
  echo [错误] 找不到 Python: "%PYDIR%\python.exe"
  pause
  exit /b 1
)
set "PATH=%PYDIR%;%PYDIR%\Scripts;%PATH%"

set VERA_QMT_ACCOUNT=180056133
set VERA_QMT_PATH=D:\Program Files\XCXT\userdata_mini

echo 开始观察行情推送(60 秒, 只读)...
python tools/p0_tick_watch.py 60

echo.
echo 判读方法:
echo   事件数 ^> 0 且字段齐全 = 订阅腿正常
echo   事件数 = 0 = 订阅不通(系统会靠轮询兜底, 止损延迟约 60s)
pause
