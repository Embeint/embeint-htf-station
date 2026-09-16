from pathlib import Path
import ssl
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from embeint_htf_station.config import ConfigError, Settings, load_settings_from_yaml
from embeint_htf_station.messaging.client import connect, create_tls_context


def settings(**kwargs):
    return Settings(org_id='org', station_id='station', _env_file=None, **kwargs)


def test_secure_defaults():
    value = settings()
    assert value.mqtt_transport == 'tls'
    assert value.broker_port == 8883
    context = create_tls_context(value)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize('kwargs', [
    {'mqtt_client_cert': 'cert.pem'}, {'mqtt_client_key': 'key.pem'},
    {'mqtt_transport': 'mtls'}, {'mqtt_transport': 'insecure'},
    {'mqtt_transport': 'plaintext', 'mqtt_ca_cert': 'ca.pem'},
    {'broker_port': 0}, {'broker_port': 65536},
])
def test_invalid_transport(kwargs):
    with pytest.raises(ValidationError):
        settings(**kwargs)


def test_explicit_plaintext():
    assert create_tls_context(settings(mqtt_transport='plaintext', broker_port=1883)) is None


def test_tls_client_context():
    with patch('ssl.create_default_context') as create:
        value = settings(mqtt_ca_cert='ca.pem', mqtt_client_cert='cert.pem', mqtt_client_key='key.pem')
        assert create_tls_context(value) is create.return_value
        create.assert_called_once_with(cafile=Path('ca.pem'))
        args = create.return_value.load_cert_chain.call_args
        assert args.args == (Path('cert.pem'), Path('key.pem'))
        assert args.kwargs['password']() == ''


@pytest.mark.parametrize('error', [PermissionError(), FileNotFoundError(), ssl.SSLError()])
def test_unreadable_key_is_actionable(error):
    with patch('ssl.create_default_context') as create:
        create.return_value.load_cert_chain.side_effect = error
        with pytest.raises(ConfigError, match='file read permissions'):
            create_tls_context(settings(mqtt_client_cert='cert.pem', mqtt_client_key='key.pem'))


def test_missing_ca_is_actionable(tmp_path):
    with pytest.raises(ConfigError, match='CA/client PEM'):
        create_tls_context(settings(mqtt_ca_cert=tmp_path / 'missing.pem'))


def test_yaml_and_env_precedence(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('station: {org_id: org, station_id: station}\nmqtt:\n'
                      '  transport: mtls\n  client_cert: yaml.pem\n  client_key: yaml.key\n')
    monkeypatch.setenv('HTF_MQTT_CLIENT_CERT', 'env.pem')
    monkeypatch.setenv('HTF_MQTT_CLIENT_KEY', 'env.key')
    monkeypatch.setenv('HTF_MQTT_CA_CERT', 'ca.pem')
    monkeypatch.setenv('HTF_MQTT_HOST', 'broker.example')
    monkeypatch.setenv('HTF_MQTT_PASSWORD', 'secret')
    value = load_settings_from_yaml(config)
    assert value.mqtt_transport == 'mtls'
    assert value.mqtt_client_cert == tmp_path / 'env.pem'
    assert value.mqtt_client_key == tmp_path / 'env.key'
    assert value.mqtt_ca_cert == tmp_path / 'ca.pem'
    assert value.broker_host == 'broker.example'
    assert value.broker_password == 'secret'
    assert 'secret' not in repr(value)


def test_environment_only_cli(monkeypatch):
    for key, value in {'TRANSPORT': 'mtls', 'HOST': 'mqtt.example', 'PORT': '9999',
                       'CLIENT_CERT': 'cert.pem', 'CLIENT_KEY': 'key.pem', 'CA_CERT': 'ca.pem',
                       'USERNAME': 'station', 'PASSWORD': 'secret'}.items():
        monkeypatch.setenv(f'HTF_MQTT_{key}', value)
    value = settings()
    assert value.mqtt_transport == 'mtls'
    assert value.broker_host == 'mqtt.example'
    assert value.broker_port == 9999
    assert value.mqtt_client_cert == Path('cert.pem')
    assert value.broker_password == 'secret'


async def test_invalid_material_does_not_construct_mqtt_client(tmp_path):
    with patch('aiomqtt.Client') as client:
        with pytest.raises(ConfigError):
            async with connect(settings(mqtt_ca_cert=tmp_path / 'missing')):
                pytest.fail('Must not start a run')
        client.assert_not_called()


def test_yaml_transport_default_and_override(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('station: {org_id: org, station_id: station}\n')
    assert load_settings_from_yaml(config).broker_port == 8883
    monkeypatch.setenv('HTF_MQTT_TRANSPORT', 'plaintext')
    assert load_settings_from_yaml(config).broker_port == 1883
    assert load_settings_from_yaml(config).mqtt_transport == 'plaintext'


def test_dotenv_transport_paths_and_process_precedence(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('station: {org_id: org, station_id: station}\n')
    (tmp_path / '.env').write_text('HTF_MQTT_TRANSPORT=mtls\nHTF_MQTT_CLIENT_CERT=client.pem\n'
                                  'HTF_MQTT_CLIENT_KEY=client.key\nHTF_MQTT_HOST=file.example\n')
    # Track additions by the existing dotenv loader for test cleanup.
    for name in ['TRANSPORT', 'CLIENT_CERT', 'CLIENT_KEY']:
        monkeypatch.setenv(f'HTF_MQTT_{name}', '')
        monkeypatch.delenv(f'HTF_MQTT_{name}')
    monkeypatch.setenv('HTF_MQTT_HOST', 'process.example')
    value = load_settings_from_yaml(config)
    assert value.mqtt_transport == 'mtls'
    assert value.mqtt_client_cert == tmp_path / 'client.pem'
    assert value.broker_host == 'process.example'
