@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0.."

REM 端口与 server.py 对齐：优先取环境变量 PORTAL_SERVER_PORT，默认 7788
if defined PORTAL_SERVER_PORT (
    set "PORT=%PORTAL_SERVER_PORT%"
) else (
    set "PORT=7788"
)

echo ============================================
echo  Portal 本地服务
echo  访问地址: http://localhost:!PORT!
echo ============================================
echo.
echo [1/2] 检测端口 !PORT! 是否已被占用（保证服务唯一）...

set "FOUND=0"
set "KILLED= "
REM 提取所有监听目标端口的 PID（tokens=5 取 netstat 最后一列 PID）
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":!PORT! .*LISTENING"') do (
    call :kill_pid %%P
)

if "!FOUND!"=="0" (
    echo     端口空闲，无需清理。
) else (
    echo     等待端口释放...
    ping -n 2 127.0.0.1 >nul
)

echo.
echo [2/2] 启动新服务... 按 Ctrl+C 停止
echo ============================================
python portal/server.py

echo.
echo 服务已退出。
pause
endlocal
goto :eof

REM ===== 子程序：kill 单个 PID（含去重、过滤 PID 0）=====
:kill_pid
set "PID=%~1"
if "!PID!"=="" goto :eof
if "!PID!"=="0" goto :eof
echo !KILLED! | findstr /C:" !PID! " >nul && goto :eof
set "FOUND=1"
echo     发现已运行的服务，PID=!PID!，正在关闭...
taskkill /F /PID !PID! >nul 2>&1
if !errorlevel!==0 (
    echo     已关闭 PID=!PID!
) else (
    echo     关闭 PID=!PID! 失败（可能已退出或权限不足）
)
set "KILLED=!KILLED!!PID! "
goto :eof
