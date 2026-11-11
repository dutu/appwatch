"""Recoverable watch selection, independent of loading or preparing YAML."""

import hashlib
import json
from pathlib import Path

from pydantic import field_validator

from app_watch.config.runtime import parse_parameters
from app_watch.errors import ConfigurationError
from app_watch.trigger.models import InstanceID, Versioned


def canonical_source(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"Cannot resolve configuration path {path}: {error}") from error


class WatchIdentity(Versioned):
    source_path: str
    job_id: InstanceID
    params: dict[str, str]

    @field_validator("source_path")
    @classmethod
    def absolute_source(cls, value: str) -> str:
        path = Path(value)
        if (
            not path.is_absolute()
            or str(path) != value
            or ".." in path.parts
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Configuration path must be absolute, normalized, without controls")
        return value

    @field_validator("params")
    @classmethod
    def valid_params(cls, value: dict[str, str]) -> dict[str, str]:
        return parse_parameters([f"{key}={item}" for key, item in value.items()])

    @property
    def scope(self) -> str:
        value = [self.source_path, self.job_id, sorted(self.params.items())]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:16]
