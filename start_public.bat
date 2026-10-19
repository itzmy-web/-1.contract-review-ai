@echo off
chcp 65001 >nul
echo ============================================
echo   法衡律鉴 - 公网访问启动器
echo ============================================
echo.

REM 1) 启动后端服务（绑定 0.0.0.0，监听 8010）
echo [1/2] 正在启动后端服务 (http://0.0.0.0:8010)...
start "合同审查服务" /min cmd /c "cd /d D:\审批合同 && .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8010"

REM 2) 启动 cpolar 内网穿透（把 8010 映射到公网）
echo [2/2] 正在启动 cpolar 隧道...
echo.
echo   隧道启动后，终端里会显示一个 https://xxx.cpolar.cn 的公网地址，
echo   把这个地址发给别人，手机/电脑都能打开。
echo.
cd /d C:\Users\zmy\cpolar
cpolar.exe http 8010

pause
