#!/usr/bin/env python3
"""兼容入口：多线程注册 CLI。

推荐：
  uv run python register_cli.py --count 1 --threads 1
  uv run python -m grok_register.cli --count 1 --threads 1
"""
from __future__ import annotations

import sys

from grok_register.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
