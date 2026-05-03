$ErrorActionPreference = "Stop"

# Export the development YOLO .pt model to a lightweight ONNX model for release.
# Run from the project root:
#   .\export_onnx_model.ps1

Set-Location $PSScriptRoot

$PtModel = "models\yolov8_custom.pt"
$OnnxModel = "models\yolov8_custom.onnx"

if (-not (Test-Path $PtModel)) {
    throw "Missing $PtModel"
}

uv sync --extra gpu --extra dev
uv run --with onnx yolo export model=$PtModel format=onnx imgsz=640 opset=12 simplify=False

if (-not (Test-Path $OnnxModel)) {
    throw "ONNX export finished but $OnnxModel was not found."
}

Write-Host "ONNX model exported: $OnnxModel"
