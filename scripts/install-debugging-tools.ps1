# install-debugging-tools.ps1
# Installs the Debugging Tools for Windows component of the Windows SDK.
#
# Requires: admin privileges. Downloads ~100 MB (the SDK installer), then
# fetches the Debugging Tools component specifically (~500 MB).
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install-debugging-tools.ps1

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$DEBUGGERS_X64 = "C:\Program Files (x86)\Windows Kits\10\Debuggers\x64"
$SDK_INSTALLER_URL = "https://go.microsoft.com/fwlink/?linkid=2286561"  # Windows 11 SDK (10.0.26100)
$INSTALLER_PATH = "$env:TEMP\winsdksetup.exe"

function Test-DebuggingToolsPresent {
    return (Test-Path "$DEBUGGERS_X64\dbgsrv.exe") -and (Test-Path "$DEBUGGERS_X64\cdb.exe")
}

function Add-ToUserPath {
    param([string]$Directory)
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath -notlike "*$Directory*") {
        [Environment]::SetEnvironmentVariable("Path", "$currentPath;$Directory", "User")
        Write-Host "  Added $Directory to user PATH. Open a new terminal for it to take effect."
    } else {
        Write-Host "  $Directory is already on PATH."
    }
}

Write-Host "=== Stackly: Windows Debugging Tools installer ===" -ForegroundColor Cyan
Write-Host ""

if (Test-DebuggingToolsPresent) {
    Write-Host "Debugging Tools already installed at $DEBUGGERS_X64" -ForegroundColor Green
    Add-ToUserPath -Directory $DEBUGGERS_X64
    Write-Host ""
    Write-Host "Run 'stackly doctor' in a new terminal to verify." -ForegroundColor Green
    exit 0
}

Write-Host "Downloading Windows SDK installer..."
Invoke-WebRequest -Uri $SDK_INSTALLER_URL -OutFile $INSTALLER_PATH -UseBasicParsing

Write-Host "Running installer (Debugging Tools component only, quiet mode)..."
Write-Host "  This will take several minutes and download ~500 MB."

# OptionId.WindowsDesktopDebuggers is the feature ID for Debugging Tools for Windows
$installArgs = @(
    "/features",
    "OptionId.WindowsDesktopDebuggers",
    "/quiet",
    "/norestart"
)
$process = Start-Process -FilePath $INSTALLER_PATH -ArgumentList $installArgs -Wait -PassThru
if ($process.ExitCode -ne 0) {
    Write-Error "Installer exited with code $($process.ExitCode). See %TEMP%\WindowsSDK_*.log for details."
    exit $process.ExitCode
}

Remove-Item $INSTALLER_PATH -Force -ErrorAction SilentlyContinue

if (-not (Test-DebuggingToolsPresent)) {
    Write-Error "Installation completed but Debugging Tools are not at $DEBUGGERS_X64. Check the SDK install log."
    exit 1
}

Write-Host "Debugging Tools installed successfully." -ForegroundColor Green
Add-ToUserPath -Directory $DEBUGGERS_X64
Write-Host ""
Write-Host "Open a new terminal and run 'stackly doctor' to verify." -ForegroundColor Green
