from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from embeint_htf_station.config import Settings
from embeint_htf_station.firmware import FirmwareCache, FirmwareError, FirmwareVersion


class FakeFirmwareCache(FirmwareCache):
    def __init__(
        self,
        settings: Settings,
        archive_bytes: bytes,
        sha256: str,
        *,
        file_name: str = "firmware.zip",
    ) -> None:
        super().__init__(settings)
        self._archive_bytes = archive_bytes
        self._version = FirmwareVersion(
            id="version-1",
            version="v1.0.0",
            file_name=file_name,
            sha256=sha256,
            download_url="/download",
        )
        self.downloads = 0

    def _resolve(self, firmware_id: str, selector: str) -> FirmwareVersion:
        assert firmware_id == "firmware-1"
        assert selector == "latest"
        return self._version

    def _download_archive(self, version: FirmwareVersion, archive_path: Path) -> None:
        self.downloads += 1
        archive_path.write_bytes(self._archive_bytes)


def test_firmware_cache_downloads_when_local_checksum_differs(tmp_path: Path) -> None:
    archive_bytes = _zip_bytes({"build/zephyr/zephyr.hex": b":020000040000FA\n"})
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path),
    )
    cache = FakeFirmwareCache(settings, archive_bytes, sha256)
    stale_archive = tmp_path / "firmware-1" / "version-1" / "firmware.zip"
    stale_archive.parent.mkdir(parents=True)
    stale_archive.write_bytes(b"stale")
    stale_extracted = tmp_path / "firmware-1" / "version-1" / "extracted" / "zephyr" / "zephyr.hex"
    stale_extracted.parent.mkdir(parents=True)
    stale_extracted.write_bytes(b"old hex")

    extracted = cache.get_file("firmware-1", "latest", "zephyr/zephyr.hex")

    assert extracted == tmp_path / "firmware-1" / "version-1" / "extracted" / "zephyr" / "zephyr.hex"
    assert extracted.read_bytes() == b":020000040000FA\n"
    assert hashlib.sha256(stale_archive.read_bytes()).hexdigest() == sha256
    assert cache.downloads == 1


def test_firmware_cache_uses_valid_cached_archive(tmp_path: Path) -> None:
    archive_bytes = _zip_bytes({"zephyr/zephyr.hex": b"hex"})
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path),
    )
    cache = FakeFirmwareCache(settings, archive_bytes, sha256)
    cached_archive = tmp_path / "firmware-1" / "version-1" / "firmware.zip"
    cached_archive.parent.mkdir(parents=True)
    cached_archive.write_bytes(archive_bytes)

    extracted = cache.get_file("firmware-1", "latest", "zephyr/zephyr.hex")

    assert extracted.read_bytes() == b"hex"
    assert cache.downloads == 0


def test_firmware_cache_rejects_unsafe_archive_paths(tmp_path: Path) -> None:
    archive_bytes = _zip_bytes({"zephyr/zephyr.hex": b"hex"})
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path),
    )
    cache = FakeFirmwareCache(settings, archive_bytes, sha256)

    with pytest.raises(FirmwareError, match="invalid firmware archive path"):
        cache.get_file("firmware-1", "latest", "../zephyr.hex")


def test_firmware_cache_can_match_extracted_7z_paths_by_suffix(tmp_path: Path) -> None:
    extracted_root = tmp_path / "extracted"
    nested_file = extracted_root / "release" / "zephyr" / "zephyr.hex"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_bytes(b"hex")

    assert FirmwareCache._find_extracted_file(extracted_root, "zephyr/zephyr.hex") == nested_file


def test_firmware_cache_reports_missing_7z_extractor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_bytes = b"7z archive bytes"
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path),
    )
    cache = FakeFirmwareCache(settings, archive_bytes, sha256, file_name="firmware.7z")
    monkeypatch.setattr(FirmwareCache, "_seven_zip_executable", staticmethod(lambda: None))

    with pytest.raises(FirmwareError, match="requires 7z, 7zz, or 7za"):
        cache.get_file("firmware-1", "latest", "zephyr/zephyr.hex")


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()
