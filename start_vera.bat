@echo off
rem ============================================================
rem  VERA 一键启动(回测 Web + 实盘交易 + 定时调度三个进程)
rem  编码:本文件 GBK + CRLF(2026-09-16 改: cmd 对 UTF-8 批处理有解析
rem  bug, 会把中文注释拦腰截断当命令执行, 报"找不到文件");
rem  子窗口各自 chcp 65001 + PYTHONIOENCODING 保 Python 中文日志不乱码
rem ============================================================
set PYTHONIOENCODING=utf-8
cd /d %~dp0

rem ---- 2026-09-20: 清除"人工停止"标记 ----------------------------------
rem  与 stop_vera.bat 配对: 那边写标记, 这边清标记。
rem  8080 页面据此区分「你主动停的」与「疑似死了」——不区分就会误报。
rem  注意: 文件路径必须与 scheduler/health.py 的 STOP_MARKER_PATH 一致,
rem  由 tests/test_scheduler_health.py 锁死, 改一处必须改两处。
del "data\.vera_stopped" >nul 2>&1

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
rem claude CLI 定位(2026-09-16 修): 研究大脑标准档 spawn claude CLI,
rem 它装在 D:\Program Files\nodejs 但不在系统 PATH —— 不加会报
rem "claude CLI 未安装, 大脑不可用"并降级快速档。同步已写入用户 PATH。
set "NODEDIR=D:\Program Files\nodejs"
if exist "%NODEDIR%\claude.cmd" set "PATH=%NODEDIR%;%PATH%"
rem 通达信安装路径(2026-09-16 修): 代码默认 E:\NEW_TDX 与本机实际不符,
rem 不设会导致轮动取数的第二级兜底(TDX)与简称表 TDX 源静默失效。
set "TDX_HOME=D:\new_tdx"
echo [0/3] Python 解释器: %PYDIR%\python.exe

echo [1/3] 启动回测 Web (8080) ...
rem 默认稳定模式(2026-09-07 起): 代码改动不会自动重启, 防打断长回测/深度思考
rem 开发要热更时: 把下行改为 python server.py --reload
start "VERA-Web-8080" cmd /k "chcp 65001 >nul && python server.py"

echo [2/3] 启动实盘交易 (8081) ...
rem 2026-09-19 架构修订批次1.1: 先等 QMT 就绪再启 trade_main ——
rem 2026-09-02 冷启动事故: miniQMT 登录初始化需 30s~2min, 没就绪就启会
rem connect() 返回 -1 崩溃。用 tools\qmt_ready_check.py 探针(与 trade_main
rem 同一份 config)每 20 秒试一次, 最多 10 次; 仍不就绪则跳过交易进程
rem (fail-closed: 回测/调度照起, 交易不起), 绝不带病启动。
rem 测试模式(不下单,用 FakeGateway): 把下行 start 行改为 python trade_main.py --fake
set "QMT_OK=0"
rem 2026-09-20 审计 P3-5: 测试模式直接跳过等待 —— trade_main 的 fake 判定
rem 有三条来源, 探针已全对齐; 这里再挡一道, 免得测试时白等 200 秒。
rem 坑: 块内 echo 不许出现任何括号 (P0-1: cmd 解析期会提前闭合 for 块)。
if "%VERA_TRADE_FAKE%"=="1" (
  echo       VERA_TRADE_FAKE=1: 测试模式, 跳过 QMT 就绪等待。
  set "QMT_OK=1"
  goto :qmt_ready
)
rem 2026-09-20 审计 P0-1 修复: 原块内 echo 带未转义的圆括号, cmd 在解析期
rem 就于第一个右括号处提前闭合 for 块, 报 "... was unexpected at this time."
rem 并中止整个脚本 (交易与调度都不起)。修法: 提示语不用括号也不用中文
rem 全角括号, 并整行移出块外 (块内只留命令与 goto)。
rem 退出码语义: 0=就绪 / 1=未就绪(值得重试) / 2=配置错误(重试无意义, 立即失败)。
for /l %%i in (1,1,10) do (
  "%PYDIR%\python.exe" tools\qmt_ready_check.py --config config\trade.yaml 2>nul
  if not errorlevel 1 (
    set "QMT_OK=1"
    goto :qmt_ready
  )
  if errorlevel 2 goto :qmt_config_error
  echo       QMT not ready, retry %%i of 10 after 20s ...
  timeout /t 20 /nobreak >nul
)
:qmt_ready
goto :qmt_wait_done
:qmt_config_error
echo [错误] QMT 探针报 CONFIG-ERROR: account_id/qmt_path 缺失或配置读不到。
echo        请修好 config\trade.yaml 后重新启动; 本次不启动交易进程。
goto :qmt_after_trade
:qmt_wait_done
if "%QMT_OK%"=="1" goto :qmt_start_trade
echo [警告] QMT 未就绪或配置有误, 本次不启动交易进程 —— 回测/调度不受影响。
echo        请确认 miniQMT 已登录, 再手工运行: python trade_main.py --config config/trade.yaml
goto :qmt_after_trade
:qmt_start_trade
start "VERA-Trade-8081" cmd /k "chcp 65001 >nul && python trade_main.py --config config/trade.yaml"
:qmt_after_trade

echo [3/3] 启动定时调度 (舆情扫描/舆情日报/月度笔记/周度进化) ...
rem 舆情扫描+日报挂在 scheduler 进程, 不拉它就没有飞书推送 (2026-08-14 修复)
start "VERA-Scheduler" cmd /k "chcp 65001 >nul && python -m scheduler"

echo.
echo 三个进程已在新窗口启动(窗口保留,报错可见):
echo   回测/选股/交易页:  http://localhost:8080
echo   交易进程 API:      http://localhost:8081
echo   定时调度(飞书舆情): python -m scheduler
echo.
echo 关闭对应窗口即停止对应进程。
if exist "%~dp0dsh-runtime\dsh.cmd" (echo [体检] DSH 深度思考通道: 已部署) else (echo [体检] DSH 深度思考通道: 未部署, 研究 TAB 勾选框不可用)
pause
