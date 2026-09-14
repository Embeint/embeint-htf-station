from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UicrWriteSettings(BaseModel):
    name: str
    address: str | int | None = None
    value: str | int | None = None
    source: str = "auto"
    width_bits: int = 32
    byte_order: str = "little"


class StageDependencySettings(BaseModel):
    lane: str
    stage: str
    outcome: str = "passed"


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
    locks: tuple[str, ...] = ()
    after: tuple[StageDependencySettings, ...] = ()


class ProgrammerSettings(BaseModel):
    name: str
    kind: str
    serial_number: str | int | None = None
    target_device: str | None = None
    board: str | None = None
    rtt_channel: int = 0
    rtt_telnet_port: int = 19021


class LaneSettings(BaseModel):
    name: str
    programmer: str
    dut_id_source: str = "manual"


class LanePlanSettings(BaseModel):
    lane: str
    stages: tuple[StageSettings, ...]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTF_", env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True,
    )

    broker_host: str = Field("localhost", validation_alias=AliasChoices("HTF_MQTT_HOST", "HTF_BROKER_HOST"))
    broker_port: int = Field(8883, ge=1, le=65535,
                             validation_alias=AliasChoices("HTF_MQTT_PORT", "HTF_BROKER_PORT"))
    broker_username: str | None = Field(None,
        validation_alias=AliasChoices("HTF_MQTT_USERNAME", "HTF_BROKER_USERNAME"))
    broker_password: str | None = Field(None, repr=False,
        validation_alias=AliasChoices("HTF_MQTT_PASSWORD", "HTF_BROKER_PASSWORD"))
    mqtt_transport: Literal["tls", "mtls", "plaintext"] = "tls"
    mqtt_ca_cert: Path | None = None
    mqtt_client_cert: Path | None = None
    mqtt_client_key: Path | None = Field(None, repr=False)

    @model_validator(mode="after")
    def validate_mqtt_transport(self) -> Self:
        if bool(self.mqtt_client_cert) != bool(self.mqtt_client_key):
            raise ValueError("MQTT client_cert and client_key must be configured together")
        if self.mqtt_transport == "mtls" and not self.mqtt_client_cert:
            raise ValueError("MQTT mtls requires client_cert and client_key")
        if self.mqtt_transport == "plaintext" and any((
            self.mqtt_ca_cert, self.mqtt_client_cert, self.mqtt_client_key,
        )):
            raise ValueError("MQTT plaintext cannot be used with TLS certificate paths")
        return self

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
    lanes: tuple[LaneSettings, ...] = ()
    plans: tuple[LanePlanSettings, ...] = ()

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

    programmers = parse_programmer_settings(data)
    stages = parse_stage_settings(data)
    lanes = parse_lane_settings(data, programmers)
    plans = parse_lane_plan_settings(data, lanes, stages)

    transport = _env_or_config("HTF_MQTT_TRANSPORT", mqtt, "transport", "tls")
    return Settings(
        mqtt_transport=transport,
        mqtt_ca_cert=_mqtt_path(path, mqtt, "ca_cert"),
        mqtt_client_cert=_mqtt_path(path, mqtt, "client_cert"),
        mqtt_client_key=_mqtt_path(path, mqtt, "client_key"),
        broker_host=_env_or_config("HTF_MQTT_HOST", mqtt, "host", "localhost"),
        broker_port=int(_env_or_config("HTF_MQTT_PORT", mqtt, "port", 1883 if transport == "plaintext" else 8883)),
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
        programmers=programmers,
        stages=stages,
        lanes=lanes,
        plans=plans,
    )


def _mqtt_path(config_path: Path, mqtt: dict[str, Any], name: str) -> Path | None:
    value = _optional_str(_env_or_config(f"HTF_MQTT_{name.upper()}", mqtt, name))
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    return _parse_yaml_text(path.read_text(encoding="utf-8"), path)


def parse_stage_settings_from_yaml_text(text: str) -> tuple[StageSettings, ...]:
    return parse_stage_settings(_parse_yaml_text(text, Path("<runtime-yaml>")))


def parse_runtime_plans_from_yaml_text(
    text: str,
    programmers: tuple[ProgrammerSettings, ...],
) -> tuple[tuple[LaneSettings, ...], tuple[LanePlanSettings, ...]]:
    """Parse server-owned plans while checking their programmer names locally.

    Runtime YAML deliberately contains no programmer hardware details; those remain
    in the station's local config.  A legacy flat ``stages`` document is one
    ``default`` lane.
    """
    data = _parse_yaml_text(text, Path("<runtime-yaml>"))
    lanes = parse_lane_settings(data, programmers)
    return lanes, parse_lane_plan_settings(data, lanes, parse_stage_settings(data))


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

        stages.append(_parse_stage_settings_item(raw_stage, f"Config stage {index}", default_programmer=None))

    if not stages:
        raise ConfigError("Config section 'stages' must contain at least one stage")

    return tuple(stages)


def parse_lane_settings(
    data: dict[str, Any],
    programmers: tuple[ProgrammerSettings, ...] | None = None,
) -> tuple[LaneSettings, ...]:
    raw_lanes = data.get("lanes")
    if raw_lanes is None:
        return ()
    if not isinstance(raw_lanes, list):
        raise ConfigError("Config section 'lanes' must be a list")

    programmer_names = {programmer.name for programmer in programmers or ()}
    lanes: list[LaneSettings] = []
    lane_names: set[str] = set()
    assigned_programmers: set[str] = set()
    for index, raw_lane in enumerate(raw_lanes, start=1):
        if not isinstance(raw_lane, dict):
            raise ConfigError(f"Config lane {index} must be a mapping")
        name = raw_lane.get("name")
        programmer = raw_lane.get("programmer")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Config lane {index} requires a non-empty name")
        if not isinstance(programmer, str) or not programmer.strip():
            raise ConfigError(f"Config lane {index} requires a non-empty programmer")

        lane_name = name.strip()
        programmer_name = programmer.strip()
        if lane_name in lane_names:
            raise ConfigError(f"Config lane {index} duplicates lane name: {lane_name}")
        if programmer_names and programmer_name not in programmer_names:
            raise ConfigError(f"Config lane {index} references unknown programmer: {programmer_name}")
        if programmer_name in assigned_programmers:
            raise ConfigError(f"Config lane {index} duplicates programmer assignment: {programmer_name}")

        lane_names.add(lane_name)
        assigned_programmers.add(programmer_name)
        lanes.append(LaneSettings(
            name=lane_name,
            programmer=programmer_name,
            dut_id_source=str(raw_lane.get("dut_id_source", raw_lane.get("dutIdSource", "manual"))),
        ))

    return tuple(lanes)


def parse_lane_plan_settings(
    data: dict[str, Any],
    lanes: tuple[LaneSettings, ...] = (),
    fallback_stages: tuple[StageSettings, ...] | None = None,
) -> tuple[LanePlanSettings, ...]:
    raw_plans = data.get("plans")
    if raw_plans is None:
        if lanes:
            raise ConfigError("Config section 'plans' is required when 'lanes' is configured")
        stages = fallback_stages if fallback_stages is not None else parse_stage_settings(data)
        return (LanePlanSettings(lane="default", stages=stages),)
    if not isinstance(raw_plans, list):
        raise ConfigError("Config section 'plans' must be a list")
    if not raw_plans:
        raise ConfigError("Config section 'plans' must contain at least one plan")

    lanes_by_name = {lane.name: lane for lane in lanes}
    if not lanes_by_name:
        raise ConfigError("Config section 'lanes' is required when 'plans' is configured")

    plans: list[LanePlanSettings] = []
    planned_lanes: set[str] = set()
    stage_names_by_lane: dict[str, set[str]] = {}
    for index, raw_plan in enumerate(raw_plans, start=1):
        if not isinstance(raw_plan, dict):
            raise ConfigError(f"Config plan {index} must be a mapping")
        lane = raw_plan.get("lane")
        if not isinstance(lane, str) or not lane.strip():
            raise ConfigError(f"Config plan {index} requires a non-empty lane")

        lane_name = lane.strip()
        lane_settings = lanes_by_name.get(lane_name)
        if lane_settings is None:
            raise ConfigError(f"Config plan {index} references unknown lane: {lane_name}")
        if lane_name in planned_lanes:
            raise ConfigError(f"Config plan {index} duplicates lane plan: {lane_name}")

        raw_stages = raw_plan.get("stages")
        if not isinstance(raw_stages, list):
            raise ConfigError(f"Config plan {index} section 'stages' must be a list")
        if not raw_stages:
            raise ConfigError(f"Config plan {index} section 'stages' must contain at least one stage")

        stages: list[StageSettings] = []
        stage_names: set[str] = set()
        for stage_index, raw_stage in enumerate(raw_stages, start=1):
            if not isinstance(raw_stage, dict):
                raise ConfigError(f"Config plan {index} stage {stage_index} must be a mapping")
            stage = _parse_stage_settings_item(
                raw_stage,
                f"Config plan {index} stage {stage_index}",
                default_programmer=lane_settings.programmer,
            )
            if stage.name in stage_names:
                raise ConfigError(f"Config plan {index} stage {stage_index} duplicates stage name: {stage.name}")
            stage_names.add(stage.name)
            stages.append(stage)

        planned_lanes.add(lane_name)
        stage_names_by_lane[lane_name] = stage_names
        plans.append(LanePlanSettings(lane=lane_name, stages=tuple(stages)))

    _validate_stage_dependencies(plans, stage_names_by_lane)
    if planned_lanes != set(lanes_by_name):
        missing = ", ".join(sorted(set(lanes_by_name) - planned_lanes))
        raise ConfigError(f"Config section 'plans' is missing lane plans: {missing}")
    return tuple(plans)


def parse_programmer_settings(data: dict[str, Any]) -> tuple[ProgrammerSettings, ...]:
    raw_programmers = data.get("programmers")
    if raw_programmers is None:
        return ()
    if not isinstance(raw_programmers, list):
        raise ConfigError("Config section 'programmers' must be a list")

    programmers: list[ProgrammerSettings] = []
    names: set[str] = set()
    for index, raw_programmer in enumerate(raw_programmers, start=1):
        if not isinstance(raw_programmer, dict):
            raise ConfigError(f"Config programmer {index} must be a mapping")
        name = raw_programmer.get("name")
        kind = raw_programmer.get("kind")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Config programmer {index} requires a non-empty name")
        if not isinstance(kind, str) or not kind.strip():
            raise ConfigError(f"Config programmer {index} requires a non-empty kind")
        if name.strip() in names:
            raise ConfigError(f"Config programmer {index} duplicates programmer name: {name.strip()}")
        names.add(name.strip())
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


def _parse_stage_settings_item(
    raw_stage: dict[str, Any],
    label: str,
    default_programmer: str | None,
) -> StageSettings:
    name = raw_stage.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{label} requires a non-empty name")

    return StageSettings(
        name=name.strip(),
        kind=str(raw_stage.get("kind", "print")),
        message=str(raw_stage.get("message", "testing")),
        wait_seconds=float(raw_stage.get("wait_seconds", raw_stage.get("waitSeconds", 5))),
        programmer=_optional_str(raw_stage.get("programmer")) or default_programmer,
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
        locks=_lock_tuple(raw_stage.get("locks")),
        after=_dependency_tuple(raw_stage.get("after")),
    )


def _dependency_tuple(value: Any) -> tuple[StageDependencySettings, ...]:
    if value is None or value == "":
        return ()
    if not isinstance(value, list):
        raise ConfigError("Config stage field 'after' must be a list")

    dependencies: list[StageDependencySettings] = []
    for index, raw_dependency in enumerate(value, start=1):
        if not isinstance(raw_dependency, dict):
            raise ConfigError(f"Config stage dependency {index} must be a mapping")
        lane = raw_dependency.get("lane")
        stage = raw_dependency.get("stage")
        if not isinstance(lane, str) or not lane.strip():
            raise ConfigError(f"Config stage dependency {index} requires a non-empty lane")
        if not isinstance(stage, str) or not stage.strip():
            raise ConfigError(f"Config stage dependency {index} requires a non-empty stage")
        dependencies.append(StageDependencySettings(
            lane=lane.strip(),
            stage=stage.strip(),
            outcome=str(raw_dependency.get("outcome", "passed")),
        ))
        if dependencies[-1].outcome not in {"passed", "failed", "aborted", "error"}:
            raise ConfigError(f"Config stage dependency {index} has an invalid outcome")
    return tuple(dependencies)


def _lock_tuple(value: Any) -> tuple[str, ...]:
    locks = _str_tuple(value)
    if any(not lock.strip() for lock in locks):
        raise ConfigError("Config stage locks must be non-empty strings")
    if len(set(locks)) != len(locks):
        raise ConfigError("Config stage locks must be unique")
    return locks


def _validate_stage_dependencies(
    plans: tuple[LanePlanSettings, ...],
    stage_names_by_lane: dict[str, set[str]],
) -> None:
    lane_names = stage_names_by_lane.keys()
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {
        (plan.lane, stage.name): set() for plan in plans for stage in plan.stages
    }
    for plan in plans:
        for stage in plan.stages:
            for dependency in stage.after:
                if dependency.lane not in lane_names:
                    raise ConfigError(
                        f"Config stage '{stage.name}' references unknown dependency lane: {dependency.lane}",
                    )
                if dependency.stage not in stage_names_by_lane[dependency.lane]:
                    raise ConfigError(
                        f"Config stage '{stage.name}' references unknown dependency stage: "
                        f"{dependency.lane}.{dependency.stage}",
                    )
                graph[(plan.lane, stage.name)].add((dependency.lane, dependency.stage))

    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()
    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            raise ConfigError("Config stage dependencies must not contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)


def _parse_yaml_text(text: str, path: Path) -> dict[str, Any]:
    expanded_text = _expand_env(text, path)
    try:
        parsed = yaml.safe_load(expanded_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: could not parse YAML: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    return parsed


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
