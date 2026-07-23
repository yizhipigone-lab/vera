@echo off
chcp 65001 >nul
cd /d "E:\1target\VERA"
set PYTHONIOENCODING=utf-8
"D:\Program Files\Python313\python.exe" tools\gs_top10_prep_parallel.py
echo EXIT CODE: %ERRORLEVEL%
pause
