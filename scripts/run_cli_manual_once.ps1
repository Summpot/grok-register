param()
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:PYTHONIOENCODING = "utf-8:replace"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe -u register_cli.py --count 1 --threads 1
} else {
  python -u register_cli.py --count 1 --threads 1
}
