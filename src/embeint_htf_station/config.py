from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UicrWriteSettings(BaseModel):
    name: str
    address: str | int | None = None
    value: str | int | None = None
    source: str = "auto"
    width_bits: int = 32
    byte_order: str = "little"


class StageSettings(BaseModel):
    name: str
    kind: str = "print"
    message: str = "testing"
    wait_seconds: float = 5.0
    programmer: str | None = None
    firmware_id: str | None = None
    firmware_version: str = "latest"
    path: str | None = None
    number_of_tests: int | None = None
    test_timeout_seconds: float = 60.0
    tests: tuple[str, ...] = ()
    rtt_command: tuple[str, ...] = ()
    rtt_channel: int = 0
    rtt_telnet_port: int = 19021
    reset_before_capture: bool = True
    board_pool: str | None = None
    constants: tuple[str, ...] = ()
    uicr: tuple[UicrWriteSettings, ...] = ()
    hardware_id_address: str | int | None = None
    hardware_id_words: int | None = None


class ProgrammerSettings(BaseModel):
    name: str
    kind: str
    serial_number: str | int | None = None
    target_device: str | None = None
    board: str | None = None
    rtt_channel: int = 0
    rtt_telnet_port: int = 19021


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTF_", env_file=".env", extra="ignore")

    broker_host: str = "localhost"
    broker_port: int = 1883
    broker_username: str | None = None
    broker_password: str | None = None

    api_base_url: str = "http://localhost:5080"
    station_key: str | None = None
    firmware_cache_dir: str = ".htf-cache/firmware"

    org_id: str = Field(..., description="UUID of the org this station belongs to")
    station_id: str = Field(..., description="UUID assigned to this station by the server")
    plugins: tuple[str, ...] = ()
    programmers: tuple[ProgrammerSettings, ...] = ()
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

    Environment variables are expanded before parsing so YAML files can use
    `${NAME}` and `${NAME:-default}` placeholders.
    """

    _load_env_file(path.with_name(".env"))
    data = _parse_simple_yaml(path)
    mqtt = _optional_mapping(data.get("mqtt"), "mqtt")
    station = _mapping(data.get("station"), "station")
    server = _optional_mapping(data.get("server"), "server")

    return Settings(
        broker_host=_env_or_config("HTF_MQTT_HOST", mqtt, "host", "localhost"),
        broker_port=int(_env_or_config("HTF_MQTT_PORT", mqtt, "port", 1883)),
        broker_username=_optional_str(_env_or_config("HTF_MQTT_USERNAME", mqtt, "username")),
        broker_password=_optional_str(_env_or_config("HTF_MQTT_PASSWORD", mqtt, "password")),
        api_base_url=_env_or_config("HTF_API_BASE_URL", server, "api_base_url", "http://localhost:5080"),
        station_key=_optional_str(_env_or_config("HTF_API_KEY", server, "station_key")),
        firmware_cache_dir=_env_or_config(
            "HTF_FIRMWARE_CACHE_DIR",
            server,
            "firmware_cache_dir",
            ".htf-cache/firmware",
        ),
        org_id=str(_required(station, "org_id")),
        station_id=str(_required(station, "station_id")),
        plugins=_str_tuple(data.get("plugins")),
        programmers=parse_programmer_settings(data),
        stages=parse_stage_settings(data),
    )


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    return _parse_yaml_text(path.read_text(encoding="utf-8"), path)


def parse_stage_settings_from_yaml_text(text: str) -> tuple[StageSettings, ...]:
    return parse_stage_settings(_parse_yaml_text(text, Path("<runtime-yaml>")))


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
            programmer=_optional_str(raw_stage.get("programmer")),
            firmware_id=_optional_str(raw_stage.get("firmware_id", raw_stage.get("firmwareId"))),
            firmware_version=str(raw_stage.get("firmware_version", raw_stage.get("firmwareVersion", "latest"))),
            path=_optional_str(raw_stage.get("path")),
            number_of_tests=_optional_int(raw_stage.get("number_of_tests", raw_stage.get("numberOfTests"))),
            test_timeout_seconds=float(
                raw_stage.get("test_timeout_seconds", raw_stage.get("testTimeoutSeconds", 60)),
            ),
            tests=_str_tuple(raw_stage.get("tests")),
            rtt_command=_str_tuple(raw_stage.get("rtt_command", raw_stage.get("rttCommand"))),
            rtt_channel=int(raw_stage.get("rtt_channel", raw_stage.get("rttChannel", 0))),
            rtt_telnet_port=int(raw_stage.get("rtt_telnet_port", raw_stage.get("rttTelnetPort", 19021))),
            reset_before_capture=bool(
                raw_stage.get("reset_before_capture", raw_stage.get("resetBeforeCapture", True)),
            ),
            board_pool=_optional_str(raw_stage.get("board_pool", raw_stage.get("boardPool"))),
            constants=_str_tuple(raw_stage.get("constants")),
            uicr=_uicr_tuple(raw_stage.get("uicr", raw_stage.get("uicr_writes", raw_stage.get("uicrWrites")))),
            hardware_id_address=raw_stage.get("hardware_id_address", raw_stage.get("hardwareIdAddress")),
            hardware_id_words=_optional_int(raw_stage.get("hardware_id_words", raw_stage.get("hardwareIdWords"))),
        ))

    if not stages:
        raise ConfigError("Config section 'stages' must contain at least one stage")

    return tuple(stages)


def parse_programmer_settings(data: dict[str, Any]) -> tuple[ProgrammerSettings, ...]:
    raw_programmers = data.get("programmers")
    if raw_programmers is None:
        return ()
    if not isinstance(raw_programmers, list):
        raise ConfigError("Config section 'programmers' must be a list")

    programmers: list[ProgrammerSettings] = []
    for index, raw_programmer in enumerate(raw_programmers, start=1):
        if not isinstance(raw_programmer, dict):
            raise ConfigError(f"Config programmer {index} must be a mapping")
        name = raw_programmer.get("name")
        kind = raw_programmer.get("kind")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Config programmer {index} requires a non-empty name")
        if not isinstance(kind, str) or not kind.strip():
            raise ConfigError(f"Config programmer {index} requires a non-empty kind")
        programmers.append(ProgrammerSettings(
            name=name.strip(),
            kind=kind.strip(),
            serial_number=raw_programmer.get("serial_number", raw_programmer.get("serialNumber")),
            target_device=_optional_str(raw_programmer.get("target_device", raw_programmer.get("targetDevice"))),
            board=_optional_str(raw_programmer.get("board")),
            rtt_channel=int(raw_programmer.get("rtt_channel", raw_programmer.get("rttChannel", 0))),
            rtt_telnet_port=int(raw_programmer.get("rtt_telnet_port", raw_programmer.get("rttTelnetPort", 19021))),
        ))

    return tuple(programmers)


def _parse_yaml_text(text: str, path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: could not parse YAML: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    return _expand_env_values(parsed, path)


def _expand_env_values(value: Any, path: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _expand_env_values(item, path)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _expand_env_values(item, path)
            for item in value
        ]
    if isinstance(value, str):
        return _expand_env(value, path)
    return value


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


def _expand_env(value: str, path: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        env_value = os.getenv(name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        raise ConfigError(f"{path}: missing required environment variable {name}")

    return _ENV_PATTERN.sub(replace, value)


def _strip_quotes(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{name}' must be a mapping")
    return value


def _optional_mapping(value: Any, name: str = "Optional config section") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{name}' must be a mapping")
    return value


def _env_or_config(env_name: str, values: dict[str, Any], key: str, default: Any = None) -> str:
    value = os.getenv(env_name)
    if value is not None:
        return value
    configured = values.get(key, default)
    return "" if configured is None else str(configured)


def _required(values: dict[str, Any], key: str) -> Any:
    value = values.get(key)
    if value is None or value == "":
        raise ConfigError(f"Config value 'station.{key}' is required")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _uicr_tuple(value: Any) -> tuple[UicrWriteSettings, ...]:
    if value is None or value == "":
        return ()
    if not isinstance(value, list):
        raise ConfigError("Config stage field 'uicr' must be a list")

    writes: list[UicrWriteSettings] = []
    for index, raw_write in enumerate(value, start=1):
        if not isinstance(raw_write, dict):
            raise ConfigError(f"Config UICR write {index} must be a mapping")
        writes.append(UicrWriteSettings(
            name=str(raw_write.get("name", "")).strip(),
            address=raw_write.get("address"),
            value=raw_write.get("value"),
            source=str(raw_write.get("source", "auto")),
            width_bits=_uicr_width_bits(raw_write),
            byte_order=_uicr_byte_order(raw_write),
        ))
    return tuple(writes)


def _uicr_width_bits(raw_write: dict[str, Any]) -> int:
    byte_count = raw_write.get("bytes")
    if byte_count is not None:
        return int(byte_count) * 8
    return int(raw_write.get("width_bits", raw_write.get("widthBits", 32)))


def _uicr_byte_order(raw_write: dict[str, Any]) -> str:
    value = raw_write.get(
        "byte_order",
        raw_write.get("byteOrder", raw_write.get("endian", raw_write.get("endin", "little"))),
    )
    normalized = str(value).strip().lower()
    aliases = {
        "lsb": "little",
        "least": "little",
        "little": "little",
        "little_endian": "little",
        "little-endian": "little",
        "msb": "big",
        "most": "big",
        "big": "big",
        "big_endian": "big",
        "big-endian": "big",
    }
    return aliases.get(normalized, normalized)
