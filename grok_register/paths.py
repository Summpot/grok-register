"""Project path anchors for the grok_register package.

PROJECT_ROOT = repository root (config.json, turnstilePatch/, output/)
PACKAGE_DIR  = this package directory (grok_register/)
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

CONFIG_FILE = PROJECT_ROOT / "config.json"
CONFIG_EXAMPLE = PROJECT_ROOT / "config.example.json"
OUTPUT_DIR = PROJECT_ROOT / "output"
TURNSTILE_DIR = PROJECT_ROOT / "turnstilePatch"
CRASH_LOG_FILE = PROJECT_ROOT / "gui_crash.log"
TOKEN_JSON = PROJECT_ROOT / "token.json"


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
