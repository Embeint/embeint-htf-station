from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StageSettings(BaseModel):
    name: str
    kind: str = "print"
    message: str = "testing"
    wait_seconds: float = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTF_", env_file=".env", extra="ignore")

    broker_host: str = "localhost"
    broker_port: int = 1883
    broker_username: str | None = None
    broker_password: str | None = None

    api_base_url: str = "http://localhost:5080"
    station_key: str | None = None

    org_id: str = Field(..., description="UUID of the org this station belongs to")
    station_id: str = Field(..., description="UUID assigned to this station by the server")
    stages: tuple[StageSettings, ...] = Field(default_factory=lambda: (
        StageSettings(name="print testing"),
    ))

    @property
    def topic_prefix(self) -> str:
        return f"htf/v1/{self.org_id}/{self.station_id}"


class ConfigError(ValueError):
    """Raised when a station YAML config cannot be loaded."""


_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-?(?P<default>[^}]*))?\}")


def load_settings_from_yaml(path: Path) -> Settings:
    """Load station settings from a small YAML file with ${ENV_VAR} expansion.

    The sample station config is intentionally simple: nested mappings only, with
    values supplied from environment variables. This parser keeps the runtime
    dependency-free until station configs need full YAML features.
    """

    _load_env_file(path.with_name(".env"))
    data = _parse_simple_yaml(path)
    mqtt = _mapping(data.get("mqtt"), "mqtt")
    station = _mapping(data.get("station"), "station")
    server = _optional_mapping(data.get("server"))

    return Settings(
        broker_host=str(mqtt.get("host", "localhost")),
        broker_port=int(mqtt.get("port", 1883)),
        broker_username=_optional_str(mqtt.get("username")),
        broker_password=_optional_str(mqtt.get("password")),
        api_base_url=str(server.get("api_base_url", "http://localhost:5080")),
        station_key=_optional_str(server.get("station_key")),
        org_id=str(_required(station, "org_id")),
        station_id=str(_required(station, "station_id")),
        stages=parse_stage_settings(data),
    )


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    return _parse_simple_yaml_text(path.read_text(encoding="utf-8"), path)


def parse_stage_settings_from_yaml_text(text: str) -> tuple[StageSettings, ...]:
    return parse_stage_settings(_parse_simple_yaml_text(text, Path("<runtime-yaml>")))


def parse_stage_settings(data: dict[str, Any]) -> tuple[StageSettings, ...]:
    raw_stages = data.get("stages")
    if raw_stages is None:
        return (StageSettings(name="print testing"),)
    if not isinstance(raw_stages, list):
        raise ConfigError("Config section 'stages' must be a list")

    stages: list[StageSettings] = []
    for index, raw_stage in enumerate(raw_stages, start=1):
        if not isinstance(raw_stage, dict):
            raise ConfigError(f"Config stage {index} must be a mapping")
        name = raw_stage.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Config stage {index} requires a non-empty name")

        stages.append(StageSettings(
            name=name.strip(),
            kind=str(raw_stage.get("kind", "print")),
            message=str(raw_stage.get("message", "testing")),
            wait_seconds=float(raw_stage.get("wait_seconds", raw_stage.get("waitSeconds", 5))),
        ))

    if not stages:
        raise ConfigError("Config section 'stages' must contain at least one stage")

    return tuple(stages)


def _parse_simple_yaml_text(text: str, path: Path) -> dict[str, Any]:
    lines: list[tuple[int, int, str]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2 != 0:
            raise ConfigError(f"{path}:{line_no}: indentation must use two-space levels")
        lines.append((line_no, indent, raw_line.strip()))

    if not lines:
        return {}

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index

        line_no, actual_indent, stripped = lines[index]
        if actual_indent != indent:
            raise ConfigError(f"{path}:{line_no}: unexpected indentation")
        if stripped.startswith("- "):
            return parse_list(index, indent)
        return parse_mapping(index, indent)

    def parse_mapping(index: int, indent: int) -> tuple[dict[str, Any], int]:
        values: dict[str, Any] = {}

        while index < len(lines):
            line_no, actual_indent, stripped = lines[index]
            if actual_indent < indent:
                break
            if actual_indent > indent:
                raise ConfigError(f"{path}:{line_no}: unexpected indentation")
            if stripped.startswith("- "):
                break

            key, raw_value = _parse_key_value(stripped, path, line_no)
            index += 1
            if raw_value == "":
                if index < len(lines) and lines[index][1] > indent:
                    values[key], index = parse_block(index, lines[index][1])
                else:
                    values[key] = {}
            else:
                values[key] = _coerce_scalar(_expand_env(raw_value, path, line_no))

        return values, index

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        values: list[Any] = []

        while index < len(lines):
            line_no, actual_indent, stripped = lines[index]
            if actual_indent < indent:
                break
            if actual_indent != indent or not stripped.startswith("- "):
                break

            item_text = stripped[2:].strip()
            index += 1
            if item_text == "":
                if index < len(lines) and lines[index][1] > indent:
                    item, index = parse_block(index, lines[index][1])
                else:
                    item = {}
            elif ":" in item_text:
                key, raw_value = _parse_key_value(item_text, path, line_no)
                item = {
                    key: _coerce_scalar(_expand_env(raw_value, path, line_no)) if raw_value else {},
                }
                if index < len(lines) and lines[index][1] > indent:
                    extra, index = parse_mapping(index, lines[index][1])
                    item.update(extra)
            else:
                item = _coerce_scalar(_expand_env(item_text, path, line_no))
                if index < len(lines) and lines[index][1] > indent:
                    nested_line_no = lines[index][0]
                    raise ConfigError(f"{path}:{nested_line_no}: scalar list item cannot have nested values")

            values.append(item)

        return values, index

    parsed, index = parse_block(0, lines[0][1])
    if index != len(lines):
        line_no = lines[index][0]
        raise ConfigError(f"{path}:{line_no}: could not parse YAML")
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    return parsed


def _parse_key_value(text: str, path: Path, line_no: int) -> tuple[str, str]:
    if ":" not in text:
        raise ConfigError(f"{path}:{line_no}: expected 'key: value'")
    key, raw_value = text.split(":", 1)
    key = key.strip()
    raw_value = raw_value.strip()
    if not key:
        raise ConfigError(f"{path}:{line_no}: empty key")
    return key, raw_value


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_no}: expected KEY=value")

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"{path}:{line_no}: empty environment variable name")

        os.environ.setdefault(key, _strip_quotes(value.strip()))


def _expand_env(value: str, path: Path, line_no: int) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        env_value = os.getenv(name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        raise ConfigError(f"{path}:{line_no}: missing required environment variable {name}")

    return _ENV_PATTERN.sub(replace, value)


def _coerce_scalar(value: str) -> str | int | float | bool | None:
    value = _strip_quotes(value)
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _strip_quotes(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{name}' must be a mapping")
    return value


def _optional_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("Optional config section must be a mapping")
    return value


def _required(values: dict[str, Any], key: str) -> Any:
    value = values.get(key)
    if value is None or value == "":
        raise ConfigError(f"Config value 'station.{key}' is required")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
