"""Настройки: секреты и пути из окружения (.env), пороги — из config/defaults.toml."""
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from planerka.db.models import Setting


class Env(BaseModel):
    """Переменные окружения. Имя переменной — имя поля в верхнем регистре."""

    database_url: str
    database_url_test: str = ""
    anthropic_api_key: str = Field("", repr=False)
    hf_token: str = Field("", repr=False)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = Field("", repr=False)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = Field("", repr=False)
    service_mailbox: str = ""
    data_dir: Path = Path("data")
    inbox_dir: Path = Path("data/inbox")
    backup_dir: Path = Path("backups")
    models_dir: Path = Path("models")
    backup_passphrase: str = Field("", repr=False)


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


class UnknownSetting(KeyError):
    pass


def get_setting(session: Session, key: str) -> Any:
    """Значение из БД, иначе из config/defaults.toml, иначе UnknownSetting."""
    row = session.get(Setting, key)
    if row is not None:
        return row.value
    defaults = load_defaults()
    if key in defaults:
        return defaults[key]
    raise UnknownSetting(key)


def set_setting(session: Session, key: str, value: Any, changed_by: str) -> None:
    if key not in load_defaults():
        raise UnknownSetting(key)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, changed_by=changed_by))
    else:
        row.value = value
        row.changed_by = changed_by
        row.changed_at = func.now()
    session.flush()
