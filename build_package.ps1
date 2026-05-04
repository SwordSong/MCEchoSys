$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $true
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock] $Command
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Command"
    }
}

# Run this script from the project root:
#   .\build_package.ps1
#
# The release package uses YOLO ONNX + ONNX Runtime/DirectML. It deliberately
# excludes CUDA Torch/ultralytics so the published _internal directory stays
# much smaller than the development environment.

Set-Location $PSScriptRoot

$BuildDir = Join-Path $PSScriptRoot "build\mc-enhance-helper"
$DistDir = Join-Path $PSScriptRoot "dist\mc-enhance-helper"
$SpecFile = Join-Path $PSScriptRoot "mc-enhance-helper.spec"
Remove-Item -LiteralPath $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $DistDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $SpecFile -Force -ErrorAction SilentlyContinue

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

Invoke-Checked { uv sync --extra dev }

# SQLCipher support for the packaged client.
Invoke-Checked { uv pip install --force-reinstall -r requirements-sqlcipher.txt }

# rapidocr-onnxruntime declares the CPU onnxruntime wheel as a dependency.
# Install RapidOCR without dependencies, then make DirectML the only explicit
# onnxruntime package before PyInstaller collects files.
Invoke-Checked { uv pip install --force-reinstall --no-deps "rapidocr-onnxruntime>=1.4.4" }
$InstalledPackages = & uv pip list --format freeze
if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code $LASTEXITCODE`: uv pip list --format freeze"
}
if ($InstalledPackages -match "(?m)^onnxruntime==") {
    Invoke-Checked { uv pip uninstall onnxruntime }
} else {
    Write-Host "CPU onnxruntime package is not installed."
}
Invoke-Checked { uv pip install --force-reinstall --no-deps onnxruntime-directml }
Invoke-Checked { uv run --no-sync python -c "import onnxruntime as ort; providers=ort.get_available_providers(); print('onnxruntime providers:', providers); assert 'DmlExecutionProvider' in providers, providers" }

$pyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",
    "--noupx",
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

$VcRuntimeDlls = @(
    "msvcp140.dll",
    "msvcp140_1.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "concrt140.dll"
)
foreach ($DllName in $VcRuntimeDlls) {
    $DllPath = Join-Path $env:WINDIR "System32\$DllName"
    if (Test-Path $DllPath) {
        $pyInstallerArgs += @("--add-binary", "$DllPath;.")
    }
}

Invoke-Checked { uv run --no-sync pyinstaller @pyInstallerArgs }

$ExePath = Join-Path $DistDir "mc-enhance-helper.exe"
Write-Host ""
Write-Host "Running ONNX Runtime package smoke test..."
Invoke-Checked { & $ExePath --ort-smoke-test }

Write-Host ""
Write-Host "Build finished: dist\mc-enhance-helper\mc-enhance-helper.exe"
Write-Host "If you need console logs, change --windowed to --console in build_package.ps1."
Write-Host "This package uses models\yolov8_custom.onnx and does not ship CUDA Torch."
