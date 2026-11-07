"""Rider settings shared by all profiles (currently: FTP)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_FTP_W = 200


class RiderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ftp_w: int = Field(default=DEFAULT_FTP_W, ge=50, le=600, description="Functional Threshold Power in watts")


class SettingsError(ValueError):
    pass


def load_rider(path: Path) -> RiderSettings:
    if not path.exists():
        return RiderSettings()
    try:
        return RiderSettings.model_validate(yaml.safe_load(path.read_text()) or {})
    except (yaml.YAMLError, ValidationError) as e:
        raise SettingsError(f"{path}: {e}") from e


def save_rider(path: Path, rider: RiderSettings) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "# Rider settings shared by all profiles. power_ftp = power_w / ftp_w.\n"
        + yaml.safe_dump(rider.model_dump(), sort_keys=False)
    )
    tmp.replace(path)
