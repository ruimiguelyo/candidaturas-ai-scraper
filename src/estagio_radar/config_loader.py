"""Carregamento centralizado dos ficheiros YAML de configuração."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=4)
def load_companies_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Lê config/companies.yaml."""
    path = Path(config_path) if config_path else CONFIG_DIR / "companies.yaml"
    return _load_yaml(path)


@lru_cache(maxsize=4)
def load_sources_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Lê config/sources.yaml."""
    path = Path(config_path) if config_path else CONFIG_DIR / "sources.yaml"
    return _load_yaml(path)
