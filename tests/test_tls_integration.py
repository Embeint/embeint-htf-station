"""Real TLS handshakes and MQTT round trips; opt in locally, mandatory in CI."""
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time

import aiomqtt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from embeint_htf_station.config import ConfigError, Settings
from embeint_htf_station.messaging.client import connect

pytestmark = pytest.mark.skipif(os.getenv('HTF_TLS_INTEGRATION') != '1', reason='requires Docker')


def command(*args):
    return subprocess.check_output(args, text=True).strip()


@pytest.fixture(scope='module')
def pki(tmp_path_factory):
    directory = tmp_path_factory.mktemp('mqtt-pki')
    directory.chmod(0o755)  # Disposable fixture must be readable after Mosquitto drops privileges.
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'HTF test CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    (directory / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name in ['server', 'mismatch', 'expired-server', 'client', 'expired-client', 'untrusted-client']:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server = name in ['server', 'mismatch', 'expired-server']
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (x509.CertificateBuilder().subject_name(subject)
                   .issuer_name(subject if name == 'untrusted-client' else ca_name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=2))
                   .not_valid_after(now + timedelta(days=-1 if name.startswith('expired') else 1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                       key.public_key() if name == 'untrusted-client' else ca_key.public_key()), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([
                       ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH,
                   ]), critical=False))
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.DNSName('wrong.example' if name == 'mismatch' else 'localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.2' if name == 'mismatch' else '127.0.0.1')),
            ]), critical=False)
        cert = builder.sign(key if name == 'untrusted-client' else ca_key, hashes.SHA256())
        (directory / f'{name}.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory / f'{name}.key').write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return directory


@contextmanager
def broker(pki: Path, server='server', plaintext=False):
    config = 'listener 8883\nallow_anonymous true\npersistence false\n'
    if not plaintext:
        config += (f'cafile /fixture/ca.pem\ncertfile /fixture/{server}.pem\n'
                   f'keyfile /fixture/{server}.key\nrequire_certificate true\ntls_version tlsv1.2\n')
    (pki / 'mosquitto.conf').write_text(config)
    # Copy rather than bind mount: works with a remote Docker daemon too.
    container = command('docker', 'create', '-p', '127.0.0.1::8883', 'eclipse-mosquitto:2.0.22',
                        'mosquitto', '-c', '/fixture/mosquitto.conf')
    try:
        command('docker', 'cp', str(pki), f'{container}:/fixture')
        command('docker', 'start', container)
        port = int(json.loads(command('docker', 'inspect', container))[0]
                   ['NetworkSettings']['Ports']['8883/tcp'][0]['HostPort'])
        deadline = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail(command('docker', 'logs', container))
                time.sleep(.1)
        yield port
    except BaseException:
        print(command('docker', 'logs', container))
        raise
    finally:
        command('docker', 'rm', '-f', container)


def settings(pki, port, client='client', **kwargs):
    return Settings(org_id='org', station_id='station', broker_host='127.0.0.1', broker_port=port,
                    mqtt_ca_cert=pki / 'ca.pem',
                    mqtt_client_cert=pki / f'{client}.pem' if client else None,
                    mqtt_client_key=pki / f'{client}.key' if client else None, _env_file=None, **kwargs)


async def round_trip(value):
    async with asyncio.timeout(5):
        async with connect(value) as client:
            await client.subscribe(f'{value.topic_prefix}/test', qos=1)
            await client.publish(f'{value.topic_prefix}/test', b'verified', qos=1)
            message = await anext(aiter(client.messages))
            assert message.payload == b'verified'


async def test_valid_mtls_round_trip(pki):
    with broker(pki) as port:
        await round_trip(settings(pki, port, mqtt_transport='mtls'))


@pytest.mark.parametrize('server', ['mismatch', 'expired-server'])
async def test_invalid_server_fails_before_run(pki, server):
    with broker(pki, server) as port:
        with pytest.raises((aiomqtt.MqttError, ssl.SSLError)):
            async with connect(settings(pki, port)):
                pytest.fail('Invalid broker certificate accepted')


async def test_untrusted_server_fails_before_run(pki):
    with broker(pki) as port:
        value = settings(pki, port).model_copy(update={'mqtt_ca_cert': None})
        with pytest.raises((aiomqtt.MqttError, ssl.SSLError)):
            async with connect(value):
                pytest.fail('Private test CA trusted by system store')


@pytest.mark.parametrize('client', [None, 'expired-client', 'untrusted-client'])
async def test_invalid_client_fails_before_run(pki, client):
    with broker(pki) as port:
        with pytest.raises((aiomqtt.MqttError, ssl.SSLError)):
            async with asyncio.timeout(20):
                async with connect(settings(pki, port, client=client)):
                    pytest.fail('Invalid station certificate accepted')


async def test_mismatched_private_key(pki):
    value = settings(pki, 8883).model_copy(update={'mqtt_client_key': pki / 'server.key'})
    with pytest.raises(ConfigError, match='matching unencrypted'):
        async with connect(value):
            pytest.fail('Mismatched key accepted')


async def test_explicit_local_plaintext(pki):
    with broker(pki, plaintext=True) as port:
        await round_trip(Settings(org_id='org', station_id='station', broker_host='127.0.0.1',
                                  broker_port=port, mqtt_transport='plaintext', _env_file=None))
