# Creates dist\WhatsAppTransfer.exe. Run it from the project folder.
$ErrorActionPreference = "Stop"

if (-not (Test-Path .venv)) {
    py -3.14 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller pytest
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw "Tests failed: exe not created" }

.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name WhatsAppTransfer `
    --collect-submodules iphone_backup_decrypt `
    --exclude-module pytest `
    app.py
