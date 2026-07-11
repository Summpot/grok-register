param()
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe .\scripts\verify_config_safe.py
} else {
  python .\scripts\verify_config_safe.py
}
