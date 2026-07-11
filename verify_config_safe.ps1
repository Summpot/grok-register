param()
Set-Location -LiteralPath $PSScriptRoot
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe verify_config_safe.py
} else {
  python verify_config_safe.py
}
