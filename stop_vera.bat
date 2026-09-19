@echo off
rem ============================================================
rem  VERA 一键停止(回测 Web + 实盘交易)
rem  优先按窗口标题关, 兜底按端口杀 PID(校验是 python 才杀)
rem  说明:交易系统按"崩溃安全"设计(重启先对账, SQLite WAL,
rem  急停三重态), 强制结束不会造成账本不一致
rem  编码:本文件 GBK + CRLF(2026-09-16 改, 原因同 start_vera.bat)
rem ============================================================

echo 停止 VERA-Web-8080 ...
taskkill /FI "WINDOWTITLE eq VERA-Web-8080*" /T /F >nul 2>&1

echo 停止 VERA-Trade-8081 ...
taskkill /FI "WINDOWTITLE eq VERA-Trade-8081*" /T /F >nul 2>&1

echo 停止 VERA-Scheduler ...
taskkill /FI "WINDOWTITLE eq VERA-Scheduler*" /T /F >nul 2>&1

rem ---- 兜底: 按端口找 PID(窗口标题被改过时仍有救, 只杀 python) ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8080 .*LISTENING"') do (
    echo 端口 8080 被 PID=%%a 占用, 校验进程名 ...
    call :KillIfPython %%a
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8081 .*LISTENING"') do (
    echo 端口 8081 被 PID=%%a 占用, 校验进程名 ...
    call :KillIfPython %%a
)

echo.
echo 已停止。注意:本脚本不碰 miniQMT 客户端本身。
pause
exit /b 0

:KillIfPython
rem 参数 %1 = PID; 映像名是 python.exe 才杀, 否则跳过防误杀
tasklist /FI "PID eq %1" /FO CSV /NH | findstr /I "python.exe" >nul
if %errorlevel%==0 (
    taskkill /F /PID %1 >nul 2>&1
    echo   已停止 PID=%1
) else (
    echo   跳过 PID=%1 ^(不是 python.exe, 防误杀^)
)
goto :eof
