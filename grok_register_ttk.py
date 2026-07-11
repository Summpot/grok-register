#!/usr/bin/env python3
"""兼容入口：GUI / 旧 CLI。

推荐：
  uv run python register_cli.py ...
  uv run python -m grok_register.cli ...
  uv run python -m grok_register.app
"""
from __future__ import annotations

from grok_register.app import main

if __name__ == "__main__":
    main()
