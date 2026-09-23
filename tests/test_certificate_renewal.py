import asyncio
import os
import ipaddress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging import renewal


@pytest.fixture
def enrolled(tmp_path):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=90))
          .not_valid_after(now + timedelta(days=90)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .sign(ca_key, hashes.SHA256()))
    settings = Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()), mqtt_transport="mtls",
                        mqtt_client_cert=tmp_path / "client.pem", mqtt_client_key=tmp_path / "client.key",
                        api_base_url="https://staging.example", station_key="station-api-secret")
    def sign(public_key, days=5, uri=None, *, ca_flag=False):
        builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"station-{settings.station_id.replace('-', '')}")]))
                   .issuer_name(ca.subject).public_key(public_key).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=25) if days <= 5 else now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=days))
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
                   .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri or renewal.identity(settings))]), False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False))
        if ca_flag is not None:
            builder = builder.add_extension(x509.BasicConstraints(ca=ca_flag, path_length=None), True)
        return builder.sign(ca_key, hashes.SHA256())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = sign(key.public_key())
    settings.mqtt_client_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    settings.mqtt_client_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    def response(request):
        csr = x509.load_pem_x509_csr(request["csrPem"].encode())
        assert csr.is_signature_valid
        import base64
        key.public_key().verify(base64.b64decode(request["proof"]),
            f"HTF-CERT-RENEW-V1\n{settings.station_id}\n{request['csrPem']}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return {"certificatePem": sign(csr.public_key(), 30).public_bytes(serialization.Encoding.PEM).decode(),
                "caChainPem": ca.public_bytes(serialization.Encoding.PEM).decode()}
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
              .issuer_name(ca.subject).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
              .sign(ca_key, hashes.SHA256()))
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "server.key").write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return settings, response, sign


def test_station_certificate_accepts_missing_leaf_basic_constraints_but_rejects_ca(enrolled):
    settings, _, sign = enrolled
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = sign(key.public_key(), ca_flag=None)
    renewal.validate_certificate(leaf, key, settings)
    ca = sign(key.public_key(), ca_flag=True)
    with pytest.raises(ValueError, match="CA certificate"):
        renewal.validate_certificate(ca, key, settings)


def test_renewal_accepts_enrollment_and_replacement_without_basic_constraints(enrolled, monkeypatch):
    settings, response, sign = enrolled
    key = serialization.load_pem_private_key(settings.mqtt_client_key.read_bytes(), password=None)
    chain = x509.load_pem_x509_certificates(settings.mqtt_client_cert.read_bytes())[1]
    enrollment = sign(key.public_key(), ca_flag=None)
    settings.mqtt_client_cert.write_bytes(
        enrollment.public_bytes(serialization.Encoding.PEM) + chain.public_bytes(serialization.Encoding.PEM))

    def bare_leaf_response(request):
        result = response(request)
        csr = x509.load_pem_x509_csr(request["csrPem"].encode())
        result["certificatePem"] = sign(csr.public_key(), 30, ca_flag=None).public_bytes(serialization.Encoding.PEM).decode()
        return result

    renewer = renewal.CertificateRenewer(settings)
    monkeypatch.setattr(renewer, "_request", bare_leaf_response)
    assert renewer.check()
    installed = x509.load_pem_x509_certificate(settings.mqtt_client_cert.read_bytes())
    with pytest.raises(x509.ExtensionNotFound):
        installed.extensions.get_extension_for_class(x509.BasicConstraints)


@pytest.mark.skipif(os.getenv("HTF_TLS_INTEGRATION") != "1", reason="requires Docker")
async def test_renewed_pair_authenticates_against_real_mqtt_broker(enrolled, monkeypatch):
    from tests.test_tls_integration import broker, round_trip
    settings, response, _ = enrolled
    directory = settings.mqtt_client_cert.parent
    directory.chmod(0o755)  # Broker fixture copies only disposable test material.
    with broker(directory) as port:
        settings.broker_host, settings.broker_port = "127.0.0.1", port
        settings.mqtt_ca_cert = directory / "ca.pem"
        await round_trip(settings)
        renewer = renewal.CertificateRenewer(settings)
        renewer.connected()
        monkeypatch.setattr(renewer, "_request", response)
        assert renewer.check()
        await round_trip(settings)
        renewer.connected()
        renewer.next_check = 0
        assert not renewer.check()


def test_renewal_installs_pair_atomically_and_restart_uses_it(enrolled, monkeypatch):
    settings, response, _ = enrolled
    original = settings.model_copy()
    renewer = renewal.CertificateRenewer(settings)
    monkeypatch.setattr(renewer, "_request", response)
    assert renewer.check()
    assert settings.mqtt_client_cert == settings.mqtt_client_key
    assert settings.mqtt_client_cert != original.mqtt_client_cert
    assert not (renewer.directory / "pending.json").exists()
    if os.name != "nt":
        assert settings.mqtt_client_key.stat().st_mode & 0o777 == 0o600
        assert renewer.directory.stat().st_mode & 0o777 == 0o700
    restarted = renewal.CertificateRenewer(original)
    monkeypatch.setattr(restarted, "_request", lambda _: pytest.fail("Not due"))
    assert not restarted.check()
    assert original.mqtt_client_cert == settings.mqtt_client_cert


def test_lost_response_retries_identical_csr_and_key(enrolled, monkeypatch):
    settings, response, _ = enrolled
    original = settings.mqtt_client_cert
    renewer = renewal.CertificateRenewer(settings)
    requests = []
    def lost(request):
        requests.append(request)
        raise TimeoutError("response lost")
    monkeypatch.setattr(renewer, "_request", lost)
    assert not renewer.check()
    pending = (renewer.directory / "pending.json").read_bytes()
    assert settings.mqtt_client_cert == original
    restarted = renewal.CertificateRenewer(settings)
    def recovered(request):
        assert request == requests[0]
        assert (renewer.directory / "pending.json").read_bytes() == pending
        return response(request)
    monkeypatch.setattr(restarted, "_request", recovered)
    assert restarted.check()


@pytest.mark.parametrize("failure", ["wrong_identity", "wrong_key", "wrong_anchor", "expired"])
def test_invalid_replacement_keeps_old_pair(enrolled, monkeypatch, failure):
    settings, response, sign = enrolled
    original = settings.mqtt_client_cert
    renewer = renewal.CertificateRenewer(settings)
    def bad(request):
        result = response(request)
        csr = x509.load_pem_x509_csr(request["csrPem"].encode())
        if failure == "wrong_identity":
            result["certificatePem"] = sign(csr.public_key(), 30, "urn:other-station").public_bytes(serialization.Encoding.PEM).decode()
        elif failure == "wrong_key":
            result["certificatePem"] = sign(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(), 30).public_bytes(serialization.Encoding.PEM).decode()
        elif failure == "wrong_anchor":
            result["caChainPem"] = result["certificatePem"]
        else:
            result["certificatePem"] = sign(csr.public_key(), -1).public_bytes(serialization.Encoding.PEM).decode()
        return result
    monkeypatch.setattr(renewer, "_request", bad)
    assert not renewer.check()
    assert settings.mqtt_client_cert == original
    assert not (renewer.directory / "active.json").exists()


def test_crash_before_pointer_commit_can_retry(enrolled, monkeypatch):
    settings, response, _ = enrolled
    original = settings.mqtt_client_cert
    renewer = renewal.CertificateRenewer(settings)
    cached = {}
    def stable_response(request):
        if not cached:
            cached.update(response(request))
        return cached
    monkeypatch.setattr(renewer, "_request", stable_response)
    atomic = renewal.atomic_write
    def fail_pointer(path, data):
        if path.name == "active.json":
            raise OSError("disk unavailable")
        atomic(path, data)
    monkeypatch.setattr(renewal, "atomic_write", fail_pointer)
    assert not renewer.check()
    assert settings.mqtt_client_cert == original
    monkeypatch.setattr(renewal, "atomic_write", atomic)
    renewer.next_check = 0
    assert renewer.check()


def test_crash_after_pointer_commit_recovers_pending(enrolled, monkeypatch):
    settings, response, _ = enrolled
    original = settings.model_copy()
    renewer = renewal.CertificateRenewer(settings)
    monkeypatch.setattr(renewer, "_request", response)
    saved = {}
    atomic = renewal.atomic_write
    def remember(path, data):
        if path.name == "pending.json":
            saved["pending"] = data
        atomic(path, data)
    monkeypatch.setattr(renewal, "atomic_write", remember)
    assert renewer.check()
    atomic(renewer.directory / "pending.json", saved["pending"])
    restarted = renewal.CertificateRenewer(original)
    assert not restarted.check()
    assert not (renewer.directory / "pending.json").exists()
    assert original.mqtt_client_cert == settings.mqtt_client_cert


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com?x=y"])
def test_renewal_forbids_insecure_api_origin(enrolled, url):
    settings, _, _ = enrolled
    settings.api_base_url = url
    with pytest.raises(ValueError):
        renewal.CertificateRenewer(settings)._request({})


def test_missing_api_key_and_redirect_are_rejected(enrolled):
    settings, _, _ = enrolled
    settings.station_key = None
    with pytest.raises(ValueError):
        renewal.CertificateRenewer(settings)._request({})
    with pytest.raises(ValueError):
        renewal.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


def test_disabling_renewal_preserves_manual_setup(enrolled):
    settings, _, _ = enrolled
    settings.mqtt_auto_renew = False
    renewer = renewal.CertificateRenewer(settings)
    assert not renewer.check()
    assert not list(settings.mqtt_client_cert.parent.glob(".htf-mtls-*"))


def test_disabling_future_renewal_keeps_last_installed_pair(enrolled, monkeypatch):
    settings, response, _ = enrolled
    original = settings.model_copy()
    renewer = renewal.CertificateRenewer(settings)
    monkeypatch.setattr(renewer, "_request", response)
    assert renewer.check()
    original.mqtt_auto_renew = False
    restarted = renewal.CertificateRenewer(original)
    monkeypatch.setattr(restarted, "_request", lambda _: pytest.fail("Renewal disabled"))
    assert not restarted.check()
    assert original.mqtt_client_cert == settings.mqtt_client_cert


async def test_handover_waits_for_idle_and_preserves_received_commands():
    queue = asyncio.Queue()
    async def messages():
        while True:
            yield await queue.get()
    called = []
    renewer = SimpleNamespace(check=lambda: called.append(True) or True)
    busy = True
    stream = renewal.renewing_messages(SimpleNamespace(messages=messages()), renewer, lambda: not busy,
                                      poll_seconds=.005, inbox=renewal.MessageInbox())
    await queue.put("first command")
    assert await anext(stream) == "first command"
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(.02)
    assert called == []
    busy = False
    with pytest.raises(renewal.CertificateRenewed):
        await task
    assert called == [True]
