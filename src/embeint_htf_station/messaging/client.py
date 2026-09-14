from __future__ import annotations

import asyncio
import ssl
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import aiomqtt
import structlog

from embeint_htf_station.config import ConfigError, Settings
from embeint_htf_station.contracts.mqtt import Heartbeat

log = structlog.get_logger(__name__)


def _client_identifier(settings: Settings) -> str:
    """Return the MQTT client ID expected by the broker identity.

    The HTF server provisions dynamic-security clients with their broker
    username as the required client ID. Fall back to the server's canonical
    station username shape for configurations without a username.
    """

    return settings.broker_username or f"station-{settings.station_id.replace('-', '')}"


def _unexpected_disconnect_context(settings: Settings, error: Exception) -> dict[str, object]:
    """Describe an established session ending unexpectedly for operator logs.

    MQTT does not identify whether a disconnect was caused by a network fault or
    another client connecting with the same client ID. Because station client IDs
    are intentionally tied to provisioned identities, credential reuse is an
    operational possibility that should be visible to the station operator.
    """

    return {
        "station_id": settings.station_id,
        "client_id": _client_identifier(settings),
        "reason": str(error),
        "possible_session_takeover": True,
    }


def create_tls_context(settings: Settings) -> ssl.SSLContext | None:
    """Validate local material before starting MQTT or accepting run commands."""
    if settings.mqtt_transport == "plaintext":
        return None
    try:
        context = ssl.create_default_context(cafile=settings.mqtt_ca_cert)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if settings.mqtt_client_cert:
            # Supplying a callback prevents OpenSSL prompting on unattended stations.
            context.load_cert_chain(settings.mqtt_client_cert, settings.mqtt_client_key, password=lambda: "")
        return context
    except (OSError, ValueError) as error:
        raise ConfigError(
            "Cannot load MQTT TLS material: check CA/client PEM files, matching unencrypted "
            "private key, and file read permissions"
        ) from error


@asynccontextmanager
async def connect(settings: Settings) -> AsyncIterator[aiomqtt.Client]:
    tls_context = create_tls_context(settings)
    connected = False
    try:
        async with aiomqtt.Client(
            hostname=settings.broker_host,
            port=settings.broker_port,
            username=settings.broker_username,
            password=settings.broker_password,
            identifier=_client_identifier(settings),
            tls_context=tls_context,
        ) as client:
            connected = True
            log.info("broker.connected", host=settings.broker_host, port=settings.broker_port)
            yield client
    except aiomqtt.MqttError as error:
        if connected:
            log.warning("broker.disconnected", **_unexpected_disconnect_context(settings, error))
        else:
            log.error("broker.connection_failed", host=settings.broker_host, port=settings.broker_port,
                      transport=settings.mqtt_transport,
                      diagnostic="Check broker hostname/CA, certificate expiry, client key and MQTT credentials")
        raise


async def heartbeat_loop(client: aiomqtt.Client, settings: Settings, interval_s: float = 5.0) -> None:
    topic = f"{settings.topic_prefix}/heartbeat"
    while True:
        payload = Heartbeat(ts=_utc_now(), status="idle", activeLanes=[]).model_dump_json(by_alias=True)
        await client.publish(topic, payload=payload, qos=1)
        await asyncio.sleep(interval_s)


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
