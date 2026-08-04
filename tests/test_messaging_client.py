from __future__ import annotations

from datetime import UTC

from embeint_htf_station.messaging.client import _utc_now


def test_utc_now_is_timezone_aware() -> None:
    assert _utc_now().tzinfo is UTC


def test_module_entrypoint_exports_cli() -> None:
    from embeint_htf_station import __main__

    assert callable(__main__.main)
