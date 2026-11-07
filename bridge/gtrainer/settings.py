"""Rider settings shared by all profiles (currently: FTP)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_FTP_W = 200


class RiderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ftp_w: int = Field(default=DEFAULT_FTP_W, ge=50, le=600, description="Functional Threshold Power in watts")
    # GT7 gears: full throttle needs first_gear_ftp x FTP in 1st gear, rising evenly
    # to 100% FTP in the top gear; the trainer grade puts that power at target_cadence_rpm.
    first_gear_ftp: float = Field(default=0.5, ge=0.1, le=1.0)
    target_cadence_rpm: int = Field(default=90, ge=50, le=130)


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
        "# Rider settings shared by all profiles. power_ftp = power_w / ftp_w;\n"
        "# with GT7 telemetry, power_gear = power_w / (ftp_w x gear share).\n"
        + yaml.safe_dump(rider.model_dump(), sort_keys=False)
    )
    tmp.replace(path)
