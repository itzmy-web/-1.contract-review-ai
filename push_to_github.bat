@echo off
cd /d "%~dp0"
title Push Contract Review System to GitHub
echo ==========================================================
echo   合同智能审查系统 - 推送到 GitHub
echo ==========================================================
echo.
echo 仓库目录: %CD%
echo 远端地址: origin = https://github.com/itzmy-web/-.git
echo.
echo -------- 最近提交 --------
git log --oneline -5
echo.
echo -------- 工作区状态 --------
git status --short
echo.
echo 说明: 本机 https 访问 github.com 可能不通，脚本会自动改走 ssh 通道
echo       前提：本机已配置 GitHub SSH key，已在 ssh -T git@github.com 验证过。
echo.
echo 开始推送...
git -c url."git@github.com:".insteadOf="https://github.com/" push -u origin main
echo.
if errorlevel 1 goto FAIL
echo [成功] 已推送到 GitHub。
goto END
:FAIL
echo [失败] 推送未成功。常见原因与处理：
echo   1. SSH key 未配置：执行 ssh -T git@github.com 看是否提示 Hi 用户名。
echo   2. 远端仓库名不对：到 GitHub 确认后执行
echo      git remote set-url origin https://github.com/itzmy-web/仓库名.git
echo   3. 网络不通：先打开 VPN 再重试。
:END
echo.
pause
