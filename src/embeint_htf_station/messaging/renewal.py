"""Station-owned renewal keys and crash-safe, single-file credential activation."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from uuid import UUID

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from filelock import FileLock, Timeout

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.inbox import MessageInbox

log = structlog.get_logger(__name__)


class CertificateRenewed(Exception):
    """The idle MQTT session should reconnect with the installed certificate."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Certificate renewal redirects are forbidden")


def atomic_write(path: Path, data: bytes) -> None:
    """Never expose half a key/certificate pair, including after power loss."""
    fd, name = tempfile.mkstemp(prefix=".renew-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identity(settings: Settings) -> str:
    return f"urn:embeint:htf:org:{UUID(settings.org_id)}:station:{UUID(settings.station_id)}"


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def validate_certificate(cert: x509.Certificate, key, settings: Settings) -> None:
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise ValueError("Expected RSA-2048 station key")
    if cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError("Station certificate/key mismatch")
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if list(san) != [x509.UniformResourceIdentifier(identity(settings))]:
        raise ValueError("Station certificate identity mismatch")
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.CLIENT_AUTH not in eku or ExtendedKeyUsageOID.SERVER_AUTH in eku:
        raise ValueError("Invalid station certificate usage")
    try:
        basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        # End-entity certificates may omit Basic Constraints. OpenBao's station
        # signing role can issue this shape; only an explicit CA=true is invalid.
        pass
    else:
        if basic_constraints.ca:
            raise ValueError("CA certificate cannot be used as a station")


class CertificateRenewer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.mqtt_transport == "mtls"
        self.next_check = 0.0
        self.failures = 0
        self.connected_fingerprint: str | None = None
        if self.enabled:
            self.original_cert = settings.mqtt_client_cert
            self.original_key = settings.mqtt_client_key
            assert self.original_cert and self.original_key
            self.directory = self.original_cert.parent / f".htf-mtls-{UUID(settings.station_id)}"

    def connected(self) -> None:
        """Record the identity only after MQTT authentication has succeeded."""
        if self.enabled:
            assert self.settings.mqtt_client_cert
            self.connected_fingerprint = fingerprint(x509.load_pem_x509_certificate(self.settings.mqtt_client_cert.read_bytes()))

    def _request(self, request: dict) -> dict:
        settings = self.settings
        url = urlsplit(settings.api_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Renewal requires a verified HTTPS API origin")
        if not settings.station_key:
            raise ValueError("Renewal requires the station API key")
        endpoint = settings.api_base_url.rstrip("/") + f"/api/v1/stations/{UUID(settings.station_id)}/certificates/renew"
        req = Request(endpoint, data=json.dumps(request).encode(), method="POST", headers={
            "Content-Type": "application/json", "X-Station-Key": settings.station_key,
            "Cache-Control": "no-store",
        })
        opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(req, timeout=20) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("Oversized renewal response")
            return json.loads(data)

    def check(self) -> bool:
        if not self.enabled or time.monotonic() < self.next_check:
            return False
        if not self.settings.mqtt_auto_renew and not self.directory.exists():
            return False
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.directory.chmod(0o700)
            with FileLock(str(self.directory / "renew.lock"), timeout=0):
                changed = self._check_locked()
            self.failures = 0
            self.next_check = time.monotonic() + 3600
            return changed
        except Timeout:
            self.next_check = time.monotonic() + 60
        except Exception as error:
            self.failures += 1
            self.next_check = time.monotonic() + min(3600, 60 * 2 ** min(self.failures - 1, 6))
            # HTTP errors/request objects can contain credentials; never log them.
            log.error("station.certificate.renewal_failed", error_type=type(error).__name__,
                      retry_seconds=round(self.next_check - time.monotonic()))
        return False

    def _check_locked(self) -> bool:
        settings = self.settings
        assert self.original_cert and self.original_key
        settings.mqtt_client_cert, settings.mqtt_client_key = self.original_cert, self.original_key
        anchor = fingerprint(x509.load_pem_x509_certificate(self.original_cert.read_bytes()))
        active_path, pending_path = self.directory / "active.json", self.directory / "pending.json"
        if active_path.exists():
            active = json.loads(active_path.read_bytes())
            if active["anchor"] != anchor:
                # An operator installed a new enrollment bundle. Never silently
                # revive credentials from the previous enrollment.
                raise ValueError("Enrollment changed; archive the old renewal directory before enabling renewal")
            pem = self.directory / active["file"]
            if pem.parent != self.directory or pem.name != active["file"]:
                raise ValueError("Invalid credential filename")
            settings.mqtt_client_cert = settings.mqtt_client_key = pem
        assert settings.mqtt_client_cert and settings.mqtt_client_key
        cert = x509.load_pem_x509_certificate(settings.mqtt_client_cert.read_bytes())
        key = serialization.load_pem_private_key(settings.mqtt_client_key.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("RSA enrollment required")
        validate_certificate(cert, key, settings)
        if not settings.mqtt_auto_renew:
            return self.connected_fingerprint is not None and self.connected_fingerprint != fingerprint(cert)
        now = datetime.now(UTC)
        window = min(timedelta(days=7), (cert.not_valid_after_utc - cert.not_valid_before_utc) / 3)
        if cert.not_valid_after_utc - now > window:
            if pending_path.exists():
                pending = json.loads(pending_path.read_bytes())
                pending_key = serialization.load_pem_private_key(pending["key"].encode(), password=None)
                # Crash after committing active.json but before removing pending.
                if pending["anchor"] == anchor and isinstance(pending_key, rsa.RSAPrivateKey) and pending_key.public_key().public_numbers() == key.public_key().public_numbers():
                    pending_path.unlink()
            return self.connected_fingerprint is not None and self.connected_fingerprint != fingerprint(cert)
        if cert.not_valid_after_utc <= now or cert.not_valid_before_utc > now:
            raise ValueError("Expired or not-yet-valid enrollment requires an administrator")
        if pending_path.exists():
            pending = json.loads(pending_path.read_bytes())
            if pending["parent"] != fingerprint(cert) or pending["anchor"] != anchor:
                raise ValueError("Pending renewal belongs to a different certificate")
            next_key = serialization.load_pem_private_key(pending["key"].encode(), password=None)
        else:
            next_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            csr = (x509.CertificateSigningRequestBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"station-{UUID(settings.station_id).hex}")]))
                   .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity(settings))]), False)
                   .sign(next_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
            proof = key.sign(f"HTF-CERT-RENEW-V1\n{UUID(settings.station_id)}\n{csr}".encode(), padding.PKCS1v15(), hashes.SHA256())
            pending = {"anchor": anchor, "parent": fingerprint(cert),
                       "key": next_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                       "request": {"certificatePem": cert.public_bytes(serialization.Encoding.PEM).decode(), "csrPem": csr,
                                   "proof": base64.b64encode(proof).decode()}}
            # Persist key and identical request BEFORE signing can occur remotely.
            atomic_write(pending_path, json.dumps(pending).encode())
        response = self._request(pending["request"])
        renewed = x509.load_pem_x509_certificate(response["certificatePem"].encode())
        validate_certificate(renewed, next_key, settings)
        if renewed.not_valid_before_utc > now or renewed.not_valid_after_utc <= max(now + timedelta(hours=1), cert.not_valid_after_utc):
            raise ValueError("Invalid replacement validity")
        # Verify against the already provisioned chain, never a newly supplied root.
        existing_chain = x509.load_pem_x509_certificates(settings.mqtt_client_cert.read_bytes())[1:]
        authorities = x509.load_pem_x509_certificates(response["caChainPem"].encode())
        if not existing_chain or not authorities:
            raise ValueError("Missing client CA chain")
        renewed.verify_directly_issued_by(authorities[0])
        for child, parent in zip(authorities, authorities[1:]):
            child.verify_directly_issued_by(parent)
        if fingerprint(authorities[-1]) != fingerprint(existing_chain[-1]):
            raise ValueError("Renewal cannot change the client trust anchor")
        pem = self.directory / f"{fingerprint(renewed)}.pem"
        material = (response["certificatePem"] + "\n" + response["caChainPem"] + "\n" + pending["key"]).encode()
        atomic_write(pem, material)
        # Ensure OpenSSL can load the combined pair before committing the pointer.
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(pem, pem, password=lambda: "")
        atomic_write(active_path, json.dumps({"anchor": anchor, "file": pem.name}).encode())
        settings.mqtt_client_cert = settings.mqtt_client_key = pem
        pending_path.unlink()
        log.info("station.certificate.renewed", expires_at=renewed.not_valid_after_utc.isoformat())
        return True


async def renewing_messages(client, renewer: CertificateRenewer, idle, poll_seconds=30, *, inbox: MessageInbox):
    """Hand over only while idle; the shared inbox retains unfinished reads."""
    iterator = aiter(client.messages)
    next_message = asyncio.create_task(anext(iterator))
    reconnect = False
    try:
        while True:
            done, _ = await asyncio.wait({next_message}, timeout=poll_seconds)
            if done:
                try:
                    message = next_message.result()
                except StopAsyncIteration:
                    return
                yield message
                inbox.handled(message)
                next_message = asyncio.create_task(anext(iterator))
            if idle():
                reconnect = reconnect or await asyncio.to_thread(renewer.check)
                # Prefer a completed read, but never infer queue ownership from
                # task.done(): aiomqtt has an inner queue task. The inbox retains
                # in-flight and buffered messages through connection teardown.
                if next_message.done() or not reconnect or not idle():
                    continue
                raise CertificateRenewed()
    finally:
        next_message.cancel()
        await asyncio.gather(next_message, return_exceptions=True)
