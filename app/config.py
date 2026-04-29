"""Configuration loading.

For local development, copy config/config.example.yaml to config/config.yaml.
You can also point STOCK_CONFIG to any YAML config file.
"""
import os
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = Path(os.environ.get("STOCK_CONFIG", PROJECT_ROOT / "config" / "config.yaml"))


def load() -> dict:
    if not _CONFIG_PATH.exists():
        example = PROJECT_ROOT / "config" / "config.example.yaml"
        raise FileNotFoundError(
            f"Config file not found: {_CONFIG_PATH}. Copy {example} to config/config.yaml first."
        )
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = load()
