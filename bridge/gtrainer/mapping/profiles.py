"""Profile files on disk: YAML with optional `extends: <other profile>` inheritance."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import Profile

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileError(ValueError):
    pass


MERGED_SECTIONS = ("buttons", "axes", "layers")


def merge_profiles(base: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Inheritance: `buttons`/`axes`/`layers` merge per entry (a child entry replaces
    the parent's binding whole); other keys are replaced. null removes a key/entry."""
    out = dict(base)
    for key, value in child.items():
        if value is None:
            out.pop(key, None)
        elif key in MERGED_SECTIONS and isinstance(value, dict) and isinstance(out.get(key), dict):
            section = dict(out[key])
            for entry, binding in value.items():
                if binding is None:
                    section.pop(entry, None)
                else:
                    section[entry] = binding
            out[key] = section
        else:
            out[key] = value
    return out


def format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"] if p not in ("function-after[_validate(), Profile]",))
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"{loc}: {msg}" if loc else msg)
    return "\n".join(lines)


class ProfileStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ProfileError(f"invalid profile name {name!r} (use a-z, 0-9, '-', '_')")
        return self.dir / f"{name}.yaml"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.yaml") if NAME_RE.match(p.stem))

    def read_text(self, name: str) -> str:
        path = self.path(name)
        if not path.exists():
            raise ProfileError(f"profile {name!r} not found")
        return path.read_text()

    def load(self, name: str) -> Profile:
        return self.validate_doc(self.resolve(name))

    def resolve(self, name: str, text: str | None = None, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
        """Return the merged document for `name` (optionally using unsaved `text`)."""
        if name in _seen:
            raise ProfileError(f"'extends' cycle: {' -> '.join(_seen + (name,))}")
        doc = self.parse(text if text is not None else self.read_text(name), name)
        parent = doc.pop("extends", None)
        if parent is None:
            return doc
        if not isinstance(parent, str):
            raise ProfileError(f"{name}: 'extends' must be a profile name")
        base = self.resolve(parent, _seen=_seen + (name,))
        base.pop("name", None)
        base.pop("description", None)
        return merge_profiles(base, doc)

    @staticmethod
    def parse(text: str, name: str = "profile") -> dict[str, Any]:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ProfileError(f"{name}: invalid YAML: {e}") from e
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            raise ProfileError(f"{name}: top level must be a mapping")
        return doc

    @staticmethod
    def validate_doc(doc: dict[str, Any]) -> Profile:
        try:
            return Profile.model_validate(doc)
        except ValidationError as e:
            raise ProfileError(format_validation_error(e)) from e

    def save_text(self, name: str, text: str) -> Profile:
        """Validate (including inheritance) and only then write."""
        profile = self.validate_doc(self.resolve(name, text=text))
        path = self.path(name)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return profile

    def read_doc(self, name: str) -> dict[str, Any]:
        """The profile's own (unmerged) document, for the form editor."""
        return self.parse(self.read_text(name), name)

    def save_doc(self, name: str, doc: dict[str, Any]) -> Profile:
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        return self.save_text(name, text)
