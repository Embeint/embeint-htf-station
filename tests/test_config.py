from pathlib import Path

import pytest

from embeint_htf_station.config import (
    ConfigError,
    ProgrammerSettings,
    load_settings_from_yaml,
    parse_lane_plan_settings,
    parse_lane_settings,
    parse_stage_settings,
)


def test_two_programmer_infuse_jig_sample_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = Path(__file__).parents[1] / "samples/two-programmer-infuse-jig/config.yaml"
    settings = load_settings_from_yaml(config)

    assert [lane.name for lane in settings.lanes] == ["left", "right"]
    assert [plan.lane for plan in settings.plans] == ["left", "right"]
    assert settings.plans[0].stages[0].programmer == "jlink_left"
    assert settings.plans[1].stages[0].programmer == "jlink_right"
    right_provisioning = settings.plans[1].stages[2]
    assert right_provisioning.locks == ("infuse_api", "board_pool:kudu")
    assert right_provisioning.after[0].lane == "left"
    assert right_provisioning.after[0].stage == "Left provisioning"


def test_load_settings_from_yaml_expands_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTF_MQTT_HOST", "broker.local")
    monkeypatch.setenv("HTF_MQTT_PORT", "1884")
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
mqtt:
  host: ${HTF_MQTT_HOST:-localhost}
  port: ${HTF_MQTT_PORT:-1883}
  username: ${HTF_MQTT_USERNAME:-}
  password: ${HTF_MQTT_PASSWORD:-}
server:
  api_base_url: ${HTF_API_BASE_URL:-http://localhost:5080}
  station_key: ${HTF_API_KEY:-}
  firmware_cache_dir: .cache/firmware
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
plugins: [infuse]
programmers:
  - name: jlink_1
    kind: jlink
    serial_number: 823000667
    target_device: nrf54l15_xxca
    board: kudu
    rtt_channel: 0
    rtt_telnet_port: 19021
stages:
  - name: Device Erase
    kind: nrfutil_device_recover
    programmer: jlink_1
  - name: Flash Firmware
    kind: firmware_flash
    programmer: jlink_1
    firmware_id: app-1
    firmware_version: latest
    path: zephyr/zephyr.hex
  - name: Validation
    kind: infuse_validation
    programmer: jlink_1
    reset_before_capture: true
    number_of_tests: 10
    test_timeout_seconds: 60
    tests: [BT, MODEM, DISK]
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert settings.broker_host == "broker.local"
    assert settings.broker_port == 1884
    assert settings.broker_username is None
    assert settings.broker_password is None
    assert settings.api_base_url == "http://localhost:5080"
    assert settings.station_key is None
    assert settings.firmware_cache_dir == ".cache/firmware"
    assert settings.org_id == "org-1"
    assert settings.station_id == "station-1"
    assert settings.plugins == ("infuse",)
    assert len(settings.programmers) == 1
    assert settings.programmers[0].name == "jlink_1"
    assert settings.programmers[0].kind == "jlink"
    assert settings.programmers[0].serial_number == 823000667
    assert settings.programmers[0].target_device == "nrf54l15_xxca"
    assert settings.programmers[0].board == "kudu"
    assert settings.programmers[0].rtt_channel == 0
    assert settings.programmers[0].rtt_telnet_port == 19021
    assert len(settings.stages) == 3
    assert settings.stages[0].name == "Device Erase"
    assert settings.stages[0].kind == "nrfutil_device_recover"
    assert settings.stages[0].programmer == "jlink_1"
    assert settings.stages[1].name == "Flash Firmware"
    assert settings.stages[1].kind == "firmware_flash"
    assert settings.stages[1].programmer == "jlink_1"
    assert settings.stages[1].firmware_id == "app-1"
    assert settings.stages[1].firmware_version == "latest"
    assert settings.stages[1].path == "zephyr/zephyr.hex"
    assert settings.stages[2].name == "Validation"
    assert settings.stages[2].kind == "infuse_validation"
    assert settings.stages[2].programmer == "jlink_1"
    assert settings.stages[2].reset_before_capture is True
    assert settings.stages[2].number_of_tests == 10
    assert settings.stages[2].test_timeout_seconds == 60
    assert settings.stages[2].tests == ("BT", "MODEM", "DISK")


def test_load_settings_from_yaml_does_not_require_mqtt_or_server_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTF_MQTT_HOST", "broker.staging.local")
    monkeypatch.setenv("HTF_MQTT_PORT", "1885")
    monkeypatch.setenv("HTF_API_BASE_URL", "https://staging.example.com")
    monkeypatch.setenv("HTF_API_KEY", "station-key")
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
stages:
  - name: Smoke
    kind: print
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert settings.broker_host == "broker.staging.local"
    assert settings.broker_port == 1885
    assert settings.api_base_url == "https://staging.example.com"
    assert settings.station_key == "station-key"
    assert settings.org_id == "org-1"
    assert settings.station_id == "station-1"


def test_load_settings_from_yaml_expands_environment_before_yaml_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")
    monkeypatch.setenv("HTF_MQTT_PORT", "1887")
    monkeypatch.setenv("HTF_PLUGIN_LIST", "[infuse, validation]")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
mqtt:
  port: ${HTF_MQTT_PORT}
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
plugins: ${HTF_PLUGIN_LIST}
stages:
  - name: Smoke
    kind: print
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert settings.broker_port == 1887
    assert settings.plugins == ("infuse", "validation")


def test_load_settings_from_yaml_environment_overrides_mqtt_and_server_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTF_MQTT_HOST", "env-broker.local")
    monkeypatch.setenv("HTF_MQTT_PORT", "1886")
    monkeypatch.setenv("HTF_MQTT_USERNAME", "env-user")
    monkeypatch.setenv("HTF_MQTT_PASSWORD", "env-pass")
    monkeypatch.setenv("HTF_API_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("HTF_API_KEY", "env-station-key")
    monkeypatch.setenv("HTF_FIRMWARE_CACHE_DIR", ".env-cache/firmware")
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
mqtt:
  host: yaml-broker.local
  port: 1999
  username: yaml-user
  password: yaml-pass
server:
  api_base_url: https://yaml.example.com
  station_key: yaml-station-key
  firmware_cache_dir: .yaml-cache/firmware
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert settings.broker_host == "env-broker.local"
    assert settings.broker_port == 1886
    assert settings.broker_username == "env-user"
    assert settings.broker_password == "env-pass"
    assert settings.api_base_url == "https://env.example.com"
    assert settings.station_key == "env-station-key"
    assert settings.firmware_cache_dir == ".env-cache/firmware"


def test_parse_stage_settings_supports_infuse_provisioning_uicr_entries() -> None:
    stages = parse_stage_settings({
        "stages": [{
            "name": "Device Provisioning",
            "kind": "infuse_provisioning",
            "programmer": "jlink_1",
            "board_pool": "kudu",
            "uicr": [{
                "name": "infuse_id",
                "bytes": 8,
                "value": "infuse_id",
                "endin": "LSB",
            }],
        }],
    })

    assert stages[0].board_pool == "kudu"
    assert stages[0].constants == ()
    assert len(stages[0].uicr) == 1
    assert stages[0].uicr[0].name == "infuse_id"
    assert stages[0].uicr[0].address is None
    assert stages[0].uicr[0].source == "auto"
    assert stages[0].uicr[0].value == "infuse_id"
    assert stages[0].uicr[0].width_bits == 64
    assert stages[0].uicr[0].byte_order == "little"


def test_load_settings_from_yaml_parses_multi_lane_plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTF_ORG_ID", "org-1")
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
programmers:
  - name: jlink_1
    kind: jlink
  - name: jlink_2
    kind: jlink
lanes:
  - name: left
    programmer: jlink_1
  - name: right
    programmer: jlink_2
plans:
  - lane: left
    stages:
      - name: Left Erase
        kind: nrfutil_device_recover
      - name: Left Provisioning
        kind: infuse_provisioning
        locks: [infuse_api, board_pool:kudu]
  - lane: right
    stages:
      - name: Right Erase
        kind: nrfutil_device_recover
      - name: Right Provisioning
        kind: infuse_provisioning
        locks: [infuse_api, board_pool:kudu]
        after:
          - lane: left
            stage: Left Provisioning
            outcome: passed
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert [lane.name for lane in settings.lanes] == ["left", "right"]
    assert [plan.lane for plan in settings.plans] == ["left", "right"]
    assert settings.plans[0].stages[0].programmer == "jlink_1"
    assert settings.plans[1].stages[0].programmer == "jlink_2"
    assert settings.plans[0].stages[1].locks == ("infuse_api", "board_pool:kudu")
    assert settings.plans[1].stages[1].after[0].lane == "left"
    assert settings.plans[1].stages[1].after[0].stage == "Left Provisioning"
    assert settings.plans[1].stages[1].after[0].outcome == "passed"


def test_parse_lane_plan_settings_wraps_flat_stages_as_default_plan() -> None:
    stages = parse_stage_settings({
        "stages": [{
            "name": "Smoke",
            "kind": "print",
        }],
    })

    plans = parse_lane_plan_settings({}, fallback_stages=stages)

    assert len(plans) == 1
    assert plans[0].lane == "default"
    assert plans[0].stages == stages


def test_parse_lane_settings_rejects_unknown_programmer() -> None:
    programmers = (ProgrammerSettings(name="jlink_1", kind="jlink"),)

    with pytest.raises(ConfigError, match="unknown programmer: jlink_2"):
        parse_lane_settings({
            "lanes": [{
                "name": "right",
                "programmer": "jlink_2",
            }],
        }, programmers)


def test_parse_lane_plan_settings_requires_plans_for_lanes() -> None:
    with pytest.raises(ConfigError, match="'plans' is required"):
        parse_lane_plan_settings({}, lanes=parse_lane_settings({
            "lanes": [{
                "name": "left",
                "programmer": "jlink_1",
            }],
        }))


def test_parse_lane_plan_settings_rejects_unknown_dependency_stage() -> None:
    lanes = parse_lane_settings({
        "lanes": [
            {"name": "left", "programmer": "jlink_1"},
            {"name": "right", "programmer": "jlink_2"},
        ],
    })

    with pytest.raises(ConfigError, match="left.Missing Stage"):
        parse_lane_plan_settings({
            "plans": [
                {
                    "lane": "left",
                    "stages": [{"name": "Left Erase", "kind": "print"}],
                },
                {
                    "lane": "right",
                    "stages": [{
                        "name": "Right Erase",
                        "kind": "print",
                        "after": [{"lane": "left", "stage": "Missing Stage"}],
                    }],
                },
            ],
        }, lanes)


def test_runtime_plan_parser_uses_local_programmers_and_rejects_dependency_cycles() -> None:
    from embeint_htf_station.config import parse_runtime_plans_from_yaml_text

    with pytest.raises(ConfigError, match="cycle"):
        parse_runtime_plans_from_yaml_text("""
lanes:
  - name: left
    programmer: jlink_1
  - name: right
    programmer: jlink_2
plans:
  - lane: left
    stages:
      - name: flash
        after: [{lane: right, stage: flash}]
  - lane: right
    stages:
      - name: flash
        after: [{lane: left, stage: flash}]
""", (ProgrammerSettings(name="jlink_1", kind="jlink"), ProgrammerSettings(name="jlink_2", kind="jlink")))


def test_lane_config_rejects_duplicate_programmer_and_stage_locks() -> None:
    with pytest.raises(ConfigError, match="duplicates programmer assignment"):
        parse_lane_settings({"lanes": [
            {"name": "left", "programmer": "jlink_1"},
            {"name": "right", "programmer": "jlink_1"},
        ]})

    with pytest.raises(ConfigError, match="locks must be unique"):
        parse_stage_settings({"stages": [{"name": "flash", "locks": ["api", "api"]}]})


def test_load_settings_from_yaml_reads_sibling_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        """
HTF_ORG_ID=org-from-dotenv
HTF_STATION_ID=station-from-dotenv
""",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        """
mqtt:
  host: localhost
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
""",
        encoding="utf-8",
    )

    settings = load_settings_from_yaml(config)

    assert settings.org_id == "org-from-dotenv"
    assert settings.station_id == "station-from-dotenv"


def test_load_settings_from_yaml_requires_station_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HTF_ORG_ID", raising=False)
    monkeypatch.setenv("HTF_STATION_ID", "station-1")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
mqtt:
  host: localhost
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="HTF_ORG_ID"):
        load_settings_from_yaml(config)


def test_parse_stage_settings_requires_at_least_one_stage() -> None:
    with pytest.raises(ConfigError, match="at least one stage"):
        parse_stage_settings({"stages": []})
