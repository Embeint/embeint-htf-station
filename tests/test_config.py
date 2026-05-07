from pathlib import Path

import pytest

from embeint_htf_station.config import ConfigError, load_settings_from_yaml, parse_stage_settings


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
programmers:
  - name: jlink_1
    kind: jlink
    serial_number: 823000667
    target_device: nrf54l15_xxca
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
    assert len(settings.programmers) == 1
    assert settings.programmers[0].name == "jlink_1"
    assert settings.programmers[0].kind == "jlink"
    assert settings.programmers[0].serial_number == 823000667
    assert settings.programmers[0].target_device == "nrf54l15_xxca"
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
