@echo off
echo === Building mc_sys Windows package ===
uv sync --extra dev
uv run pyinstaller --noconfirm --onedir --windowed ^
    --name "mc_enhance" ^
    --add-data "models;models" ^
    --hidden-import "src" ^
    --hidden-import "src.ui" ^
    src\ui\overlay.py
echo === Done. Output in dist\mc_enhance\ ===
pause
