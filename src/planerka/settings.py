"""Настройки: секреты и пути из окружения (.env), пороги — из config/defaults.toml."""
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class Env(BaseModel):
    """Переменные окружения. Имя переменной — имя поля в верхнем регистре."""

    database_url: str
    database_url_test: str = ""
    anthropic_api_key: str = ""
    hf_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    service_mailbox: str = ""
    data_dir: Path = Path("data")
    inbox_dir: Path = Path("data/inbox")
    backup_dir: Path = Path("backups")
    models_dir: Path = Path("models")
    backup_passphrase: str = ""


def load_env(environ: Mapping[str, str] = os.environ) -> Env:
    values = {
        name: environ[name.upper()]
        for name in Env.model_fields
        if environ.get(name.upper(), "") != ""
    }
    return Env(**values)


def _flatten(table: Mapping[str, Any], prefix: str, out: dict[str, Any]) -> None:
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            _flatten(value, f"{path}.", out)
        else:
            out[path] = value


def load_defaults(path: Path = Path("config/defaults.toml")) -> dict[str, Any]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    out: dict[str, Any] = {}
    _flatten(data, "", out)
    return out
