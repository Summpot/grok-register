# Clean package notes

This package intentionally excludes local/private runtime data:
- config.json and config backups
- output/accounts_*.txt
- output/mail_credentials.txt
- output/cpa_auths/
- .venv/
- __pycache__/
- gui_crash.log

Before use, copy config.example.json to config.json and fill in your own keys/endpoints.
