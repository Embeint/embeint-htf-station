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
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
stages:
  - name: print testing
    kind: print
    message: testing
    wait_seconds: 5
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
    assert settings.org_id == "org-1"
    assert settings.station_id == "station-1"
    assert len(settings.stages) == 1
    assert settings.stages[0].name == "print testing"
    assert settings.stages[0].kind == "print"
    assert settings.stages[0].message == "testing"
    assert settings.stages[0].wait_seconds == 5


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
