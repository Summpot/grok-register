# Clean package notes

This package intentionally excludes local/private runtime data:
- config.json and config backups
- accounts_*.txt
- mail_credentials.txt
- cpa_auths/
- sub2api_exports/
- .venv/
- __pycache__/
- gui_crash.log

Before use, copy config.example.json to config.json and fill in your own keys/endpoints.
