@echo off
REM === 鸣潮强化识别工具 — GPU 版启动脚本 ===
REM 使用 uv 管理环境，无需手动激活

echo Syncing GPU dependencies...
uv sync --extra gpu

echo Starting overlay + pipeline...
uv run python -m src.ui.overlay
pause
