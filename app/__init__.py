"""Load .env before any module reads os.environ.

This runs first because every other module is imported through this package.
uvicorn does not read .env by itself, so without this the app starts fine and
then dies on the first message with KeyError: 'ANTHROPIC_API_KEY'."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # dotenv missing: fall back to real environment variables
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
