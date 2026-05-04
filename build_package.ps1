$ErrorActionPreference = "Stop"

# Run this script from the project root:
#   .\build_package.ps1
#
# The release package uses YOLO ONNX + ONNX Runtime/DirectML. It deliberately
# excludes CUDA Torch/ultralytics so the published _internal directory stays
# much smaller than the development environment.test

Set-Location $PSScriptRoot

$OnnxModel = Join-Path $PSScriptRoot "models\yolov8_custom.onnx"
$PtModel = Join-Path $PSScriptRoot "models\yolov8_custom.pt"

if (-not (Test-Path $OnnxModel)) {
    Write-Host "Missing models\yolov8_custom.onnx"
    Write-Host ""
    Write-Host "Export it once from the training model before packaging:"
    Write-Host "  .\export_onnx_model.ps1"
    Write-Host ""
    if (Test-Path $PtModel) {
        Write-Host "Found models\yolov8_custom.pt, but the lightweight package must use ONNX."
    }
    throw "ONNX model is required for lightweight packaging."
}

uv sync --extra dev

# SQLCipher support for the packaged client.
uv pip install -r requirements-sqlcipher.txt

$pyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "mc-enhance-helper",

    "--add-data", "models\yolov8_custom.onnx;models",
    "--add-data", "data\echo_dictionary.json;data",
    "--add-data", "data\panel_layout.json;data",
    "--add-data", "data\strategy_priority.json;data",
    "--add-data", "data\substat_values.json;data",
    "--add-data", "data\character_name_entry_id.json;data",

    "--collect-all", "rapidocr_onnxruntime",
    "--collect-all", "onnxruntime",

    "--exclude-module", "ultralytics",
    "--exclude-module", "torch",
    "--exclude-module", "torchvision",
    "--exclude-module", "torchaudio",

    "--hidden-import", "cv2",
    "--hidden-import", "dxcam",
    "--hidden-import", "windows_capture",
    "--hidden-import", "PyQt6.sip",
    "--hidden-import", "sqlcipher3",
    "--hidden-import", "sqlcipher3.dbapi2",
    "--hidden-import", "pysqlcipher3.dbapi2",

    "src\ui\overlay.py"
)

uv run pyinstaller @pyInstallerArgs

Write-Host ""
Write-Host "Build finished: dist\mc-enhance-helper\鸣潮声骸助手.exe"
Write-Host "If you need console logs, change --windowed to --console in build_package.ps1."
Write-Host "This package uses models\yolov8_custom.onnx and does not ship CUDA Torch."
