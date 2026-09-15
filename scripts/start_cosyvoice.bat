@echo off
REM CosyVoice3 本地推理服务启动脚本
REM 用法：在仓库根目录执行 scripts\start_cosyvoice.bat
REM 依赖：conda 环境 cosyvoice 已安装依赖并下载模型

set REPO_ROOT=%~dp0..
cd /d %REPO_ROOT%

echo [1/2] 启动 CosyVoice3 推理服务 (http://127.0.0.1:9880) ...
start "CosyVoice3-Service" cmd /k conda run -n cosyvoice --no-capture-output python cosyvoice_service\server.py --host 127.0.0.1 --port 9880

echo [2/2] 等待模型加载（首次约 30-60 秒），可通过 http://127.0.0.1:9880/health 查看
echo 完成。
