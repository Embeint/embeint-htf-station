from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from embeint_htf_station.config import Settings
from embeint_htf_station.http_client import open_no_redirect as urlopen


class FirmwareError(RuntimeError):
    """Raised when firmware cannot be resolved, cached, or extracted."""


def _origin(url: str) -> tuple[str, str, int]:
    try:
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
            raise ValueError("invalid URL")
        return target.scheme, target.hostname, target.port or (443 if target.scheme == "https" else 80)
    except ValueError:
        raise FirmwareError("firmware download URL is invalid") from None


class _SafeDownloadRedirects(HTTPRedirectHandler):
    def __init__(self, api_origin: tuple[str, str, int]) -> None:
        self._api_origin = api_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = _origin(newurl)
        if destination != self._api_origin and destination[0] != "https":
            raise FirmwareError("external firmware downloads require HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and destination != self._api_origin:
            for header in (*redirected.headers, *redirected.unredirected_hdrs):
                if header.lower() == "x-station-key":
                    redirected.remove_header(header)
        return redirected


@dataclass(frozen=True)
class FirmwareVersion:
    id: str
    version: str
    file_name: str
    sha256: str
    download_url: str


class FirmwareCache:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache_dir = Path(settings.firmware_cache_dir)

    def get_file(self, firmware_id: str, selector: str, path_in_archive: str) -> Path:
        version = self._resolve(firmware_id, selector)
        archive = self._cache_archive(firmware_id, version)
        extracted = self._extract_file(firmware_id, version, archive, path_in_archive)
        return extracted

    def _resolve(self, firmware_id: str, selector: str) -> FirmwareVersion:
        selector = selector or "latest"
        url = (
            f"{self._settings.api_base_url.rstrip('/')}/api/v1/stations/"
            f"{self._settings.station_id}/firmware/{firmware_id}/resolve?selector={quote(selector)}"
        )
        data = self._get_json(url)
        version = data.get("version")
        if not isinstance(version, dict):
            raise FirmwareError("firmware resolve response did not include a version")

        try:
            return FirmwareVersion(
                id=str(version["id"]),
                version=str(version["version"]),
                file_name=str(version["fileName"]),
                sha256=str(version["sha256"]),
                download_url=str(data["downloadUrl"]),
            )
        except KeyError as exc:
            raise FirmwareError(f"firmware resolve response missing {exc.args[0]}") from exc

    def _cache_archive(self, firmware_id: str, version: FirmwareVersion) -> Path:
        archive_dir = self._cache_dir / firmware_id / version.id
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / version.file_name
        metadata_path = archive_dir / "metadata.json"

        if archive_path.exists() and self._sha256(archive_path) == version.sha256:
            return archive_path

        tmp_path = archive_path.with_suffix(f"{archive_path.suffix}.tmp")
        self._download_archive(version, tmp_path)

        downloaded_sha = self._sha256(tmp_path)
        if downloaded_sha != version.sha256:
            tmp_path.unlink(missing_ok=True)
            raise FirmwareError(
                f"firmware checksum mismatch for {version.file_name}: expected {version.sha256}, got {downloaded_sha}",
            )

        tmp_path.replace(archive_path)
        metadata_path.write_text(json.dumps({
            "firmwareId": firmware_id,
            "versionId": version.id,
            "version": version.version,
            "fileName": version.file_name,
            "sha256": version.sha256,
        }, indent=2), encoding="utf-8")
        return archive_path

    def _download_archive(self, version: FirmwareVersion, tmp_path: Path) -> None:
        url = version.download_url
        if url.startswith("/"):
            url = f"{self._settings.api_base_url.rstrip('/')}{url}"

        api_origin = _origin(self._settings.api_base_url)
        download_origin = _origin(url)
        if download_origin != api_origin and download_origin[0] != "https":
            raise FirmwareError("external firmware downloads require HTTPS")
        headers = self._headers() if download_origin == api_origin else {"Accept": "application/json"}
        request = Request(url, headers=headers)
        try:
            with build_opener(_SafeDownloadRedirects(api_origin)).open(request, timeout=120) as response, tmp_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        except HTTPError as exc:
            raise FirmwareError(f"failed to download firmware archive (HTTP {exc.code})") from None
        except (URLError, TimeoutError):
            raise FirmwareError("failed to download firmware archive") from None

    def _extract_file(self, firmware_id: str, version: FirmwareVersion, archive: Path, path_in_archive: str) -> Path:
        normalized_path = self._normalize_archive_path(path_in_archive)
        if not normalized_path:
            raise FirmwareError("firmware stage requires a path inside the firmware archive")

        output_path = self._cache_dir / firmware_id / version.id / "extracted" / normalized_path
        if output_path.exists() and output_path.stat().st_mtime >= archive.stat().st_mtime:
            return output_path

        suffix = archive.suffix.lower()
        if suffix == ".zip":
            self._extract_zip_file(archive, normalized_path, output_path)
        elif suffix == ".7z":
            self._extract_7z_file(archive, normalized_path, output_path)
        else:
            raise FirmwareError(
                f"unsupported firmware archive type for {archive.name}; expected .zip or .7z",
            )
        return output_path

    def _extract_zip_file(self, archive: Path, path_in_archive: str, output_path: Path) -> None:
        with zipfile.ZipFile(archive) as zf:
            member = self._find_zip_member(zf, path_in_archive)
            if member is None:
                raise FirmwareError(f"firmware archive does not contain {path_in_archive}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, output_path.open("wb") as output:
                shutil.copyfileobj(source, output)

    def _extract_7z_file(self, archive: Path, path_in_archive: str, output_path: Path) -> None:
        seven_zip = self._seven_zip_executable()
        if seven_zip is None:
            raise FirmwareError("extracting .7z firmware archives requires 7z, 7zz, or 7za on PATH")

        archive_path_parts = Path(path_in_archive).parts
        extract_root = output_path.parents[len(archive_path_parts)] / ".extracting"
        shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                (seven_zip, "x", str(archive), f"-o{extract_root}", "-y"),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout).strip()
                raise FirmwareError(f"failed to extract {archive.name} with 7z: {details or result.returncode}")

            extracted = self._find_extracted_file(extract_root, path_in_archive)
            if extracted is None:
                raise FirmwareError(f"firmware archive does not contain {path_in_archive}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted, output_path)
        except subprocess.TimeoutExpired as exc:
            raise FirmwareError(f"timed out extracting {archive.name} with 7z") from exc
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    @staticmethod
    def _find_zip_member(zf: zipfile.ZipFile, path_in_archive: str) -> str | None:
        normalized = FirmwareCache._normalize_archive_path(path_in_archive)
        names = set(zf.namelist())
        if normalized in names:
            return normalized
        suffix = f"/{normalized}"
        return next((name for name in names if name.endswith(suffix)), None)

    @staticmethod
    def _find_extracted_file(root: Path, path_in_archive: str) -> Path | None:
        normalized = FirmwareCache._normalize_archive_path(path_in_archive)
        exact = root / normalized
        if exact.is_file():
            return exact

        suffix = Path(normalized)
        for candidate in sorted(root.rglob("*")):
            if candidate.is_file() and _path_has_suffix(candidate.relative_to(root), suffix):
                return candidate
        return None

    @staticmethod
    def _normalize_archive_path(path_in_archive: str) -> str:
        if not path_in_archive.strip():
            return ""
        normalized = Path(path_in_archive.strip().lstrip("/"))
        if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
            raise FirmwareError(f"invalid firmware archive path: {path_in_archive}")
        return normalized.as_posix()

    @staticmethod
    def _seven_zip_executable() -> str | None:
        return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")

    def _get_json(self, url: str) -> dict[str, object]:
        request = Request(url, headers=self._headers())
        try:
            with urlopen(request, timeout=30) as response:  # nosec B310
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise FirmwareError(f"failed to resolve firmware (HTTP {exc.code})") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise FirmwareError("failed to resolve firmware") from None
        if not isinstance(data, dict):
            raise FirmwareError("firmware resolve response was not an object")
        return data

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._settings.station_key:
            headers["X-Station-Key"] = self._settings.station_key
        return headers

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _path_has_suffix(path: Path, suffix: Path) -> bool:
    path_parts = path.parts
    suffix_parts = suffix.parts
    return len(path_parts) >= len(suffix_parts) and path_parts[-len(suffix_parts):] == suffix_parts
