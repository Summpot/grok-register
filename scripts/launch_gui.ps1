param()
# Project root is parent of scripts/
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:PYTHONIOENCODING = "utf-8:replace"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe grok_register_ttk.py
} else {
  python grok_register_ttk.py
}
