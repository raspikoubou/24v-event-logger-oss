"""GitHub Releaseの確認と、検証済みassetの固定updater起動。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class UpdateError(RuntimeError):
    """Release確認・asset検証・updater起動に失敗した。"""


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
"""Webから受け取るRelease archiveの上限。"""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """GitHub Release assetの固定情報。"""

    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """安定版ReleaseのWeb表示用情報。"""

    tag: str
    version: str
    url: str
    notes: str
    archive: ReleaseAsset
    checksums: ReleaseAsset


class GitHubReleaseClient:
    """許可したrepositoryのlatest Releaseだけを取得する。"""

    def __init__(self, repository: str, *, timeout: float = 5.0) -> None:
        """`owner/name`形式のrepositoryを受け取る。"""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        self._repository = repository
        self._timeout = timeout

    def latest(self) -> ReleaseInfo:
        """GitHub APIからdraft/prereleaseを除く最新Releaseを取得する。"""
        url = f"https://api.github.com/repos/{self._repository}/releases/latest"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "event-logger-update-check",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise UpdateError(
                f"release information could not be fetched: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise UpdateError("release response must be an object")
        if payload.get("draft") is True or payload.get("prerelease") is True:
            raise UpdateError("latest release is not a stable release")
        tag = payload.get("tag_name")
        html_url = payload.get("html_url")
        notes = payload.get("body", "")
        raw_assets = payload.get("assets")
        if not isinstance(tag, str) or not isinstance(html_url, str):
            raise UpdateError("release response is missing tag or URL")
        if not isinstance(notes, str) or not isinstance(raw_assets, list):
            raise UpdateError("release response has invalid assets")
        assets: dict[str, ReleaseAsset] = {}
        for raw_asset in raw_assets:
            if not isinstance(raw_asset, dict):
                continue
            name = raw_asset.get("name")
            download_url = raw_asset.get("browser_download_url")
            size = raw_asset.get("size", 0)
            if (
                isinstance(name, str)
                and isinstance(download_url, str)
                and isinstance(size, int)
            ):
                assets[name] = ReleaseAsset(name, download_url, size)
        version = _normalize_version(tag)
        archive = assets.get(f"event-logger-{version}.tar.zst")
        checksums = assets.get("SHA256SUMS")
        if archive is None or checksums is None:
            raise UpdateError("release assets are incomplete")
        for asset in (archive, checksums):
            if not _is_github_asset_url(asset.url, self._repository):
                raise UpdateError("release asset URL is not a GitHub release asset")
        return ReleaseInfo(tag, version, html_url, notes, archive, checksums)


def is_newer_version(current: str, latest: str) -> bool:
    """latestがcurrentより新しいsemver風Versionか返す。"""
    return _version_key(latest) > _version_key(current)


def validate_update_candidate(
    installed_pyproject_path: str | os.PathLike[str],
    candidate_pyproject_path: str | os.PathLike[str],
    requested_version: str,
) -> None:
    """更新前後のpackage metadataと要求Versionが整合することを検証する。

    root updaterはchecksum検証後、service停止やDB操作を始める前にこの関数を
    呼ぶ。asset名のVersionだけを信用すると、旧版やpre-releaseをchecksum付き
    で受理してしまうため、archive内metadataとinstalled Versionを比較する。

    Args:
        installed_pyproject_path: 現行releaseのpyproject.toml。
        candidate_pyproject_path: checksum検証後に展開した候補releaseのpyproject.toml。
        requested_version: update requestとarchive名に指定されたVersion。

    Raises:
        UpdateError: metadata不正、Version不一致、pre-release、同一版または旧版。
    """
    installed_version = _project_version(installed_pyproject_path)
    candidate_version = _project_version(candidate_pyproject_path)
    if candidate_version != requested_version:
        raise UpdateError("archive metadata does not match requested version")
    try:
        candidate_key = _version_key(candidate_version)
        is_newer = is_newer_version(installed_version, candidate_version)
    except ValueError as error:
        raise UpdateError("release version is invalid") from error
    if candidate_key[1] != 3:
        raise UpdateError("pre-release versions are not accepted by the updater")
    if not is_newer:
        raise UpdateError("update version must be newer than the installed version")


def _project_version(pyproject_path: str | os.PathLike[str]) -> str:
    """pyproject.tomlから検証済みのproject.versionを読む。"""
    try:
        with Path(pyproject_path).open("rb") as project_file:
            document = tomllib.load(project_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise UpdateError("release pyproject.toml is missing or invalid") from error
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise UpdateError("release project.version is missing or invalid")
    return version


def download_and_verify(
    release: ReleaseInfo,
    *,
    work_dir: str | os.PathLike[str],
    timeout: float = 30.0,
) -> tuple[Path, Path]:
    """archiveとSHA256SUMSを一時領域へ取得し、対象archiveを検証する。"""
    root = Path(work_dir)
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"release-{release.version}-", dir=root))
    try:
        archive_path = staging / release.archive.name
        checksum_path = staging / release.checksums.name
        archive_limit = (
            min(MAX_ARCHIVE_BYTES, release.archive.size)
            if release.archive.size > 0
            else MAX_ARCHIVE_BYTES
        )
        _download(release.archive.url, archive_path, timeout, max_bytes=archive_limit)
        _download(
            release.checksums.url,
            checksum_path,
            timeout,
            max_bytes=16 * 1024 * 1024,
        )
        expected = _checksum_for(checksum_path, release.archive.name)
        actual = _sha256_file(archive_path)
        if actual.lower() != expected.lower():
            raise UpdateError("release archive SHA-256 does not match SHA256SUMS")
        return archive_path, checksum_path
    except Exception:
        # 検証に失敗したassetをupdaterへ渡さない。
        for path in staging.iterdir():
            path.unlink(missing_ok=True)
        staging.rmdir()
        raise


def launch_updater(
    *,
    helper_path: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    checksum_path: str | os.PathLike[str],
    version: str,
    socket_path: str | os.PathLike[str] | None = None,
) -> None:
    """固定pathのupdaterだけを引数分離で起動する。

    通常のWeb service userはroot権限を持たないため、productionではrootで
    常駐するupdate-agentのUnix socketへ依頼する。socket未指定時だけ、root
    からの開発・保守実行としてhelperを直接起動する。
    """
    if socket_path is not None:
        _request_update_agent(
            socket_path,
            archive_path=archive_path,
            checksum_path=checksum_path,
            version=version,
        )
        return
    helper = Path(helper_path)
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise UpdateError("configured update helper is not executable")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise UpdateError("direct update helper requires root; configure update-agent")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", version):
        raise UpdateError("release version is invalid")
    try:
        subprocess.Popen(
            [
                str(helper),
                "--archive",
                str(archive_path),
                "--sha256",
                str(checksum_path),
                "--version",
                version,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as error:
        raise UpdateError(f"update helper could not be started: {error}") from error


def _download(url: str, destination: Path, timeout: float, *, max_bytes: int) -> None:
    """HTTPS assetを上限付きstreamとして保存する。"""
    request = Request(url, headers={"User-Agent": "event-logger-updater"})
    try:
        with (
            urlopen(request, timeout=timeout) as response,
            destination.open("wb") as file,
        ):
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                raise UpdateError("release asset exceeds the configured size limit")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateError("release asset exceeds the configured size limit")
                file.write(chunk)
    except UpdateError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        raise UpdateError(f"release asset could not be downloaded: {error}") from error


def _sha256_file(path: Path) -> str:
    """大きなarchiveをメモリへ載せずSHA-256を計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request_update_agent(
    socket_path: str | os.PathLike[str],
    *,
    archive_path: str | os.PathLike[str],
    checksum_path: str | os.PathLike[str],
    version: str,
) -> None:
    """root update-agentへ固定schemaの依頼を送る。"""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", version):
        raise UpdateError("release version is invalid")
    payload = (
        json.dumps(
            {
                "archive": str(archive_path),
                "checksum": str(checksum_path),
                "version": version,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > 64 * 1024:
        raise UpdateError("update request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(30.0)
            connection.connect(str(socket_path))
            connection.sendall(payload)
            response = _read_agent_response(connection)
    except (OSError, TimeoutError) as error:
        raise UpdateError(f"update-agent is unavailable: {error}") from error
    if not isinstance(response, dict):
        raise UpdateError("update-agent returned an invalid response")
    if response.get("ok") is not True:
        message = response.get("message")
        raise UpdateError(
            str(message)
            if isinstance(message, str)
            else "update-agent rejected request"
        )


def request_reboot(socket_path: str | os.PathLike[str]) -> None:
    """管理agentへ本体再起動だけを依頼し、任意コマンドは渡さない。"""
    if not str(socket_path):
        raise UpdateError("再起動サービスが設定されていません。")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(socket_path))
            connection.sendall(b'{"action":"reboot"}\n')
            response = _read_agent_response(connection)
    except OSError as error:
        raise UpdateError("再起動サービスへ接続できませんでした。") from error
    if not isinstance(response, dict) or response.get("ok") is not True:
        message = response.get("message") if isinstance(response, dict) else None
        raise UpdateError(
            str(message)
            if isinstance(message, str)
            else "再起動を開始できませんでした。"
        )


def _read_agent_response(connection: socket.socket) -> object:
    """update-agentのnewline JSONをサイズ制限付きで読む。"""
    buffer = bytearray()
    while len(buffer) <= 64 * 1024:
        chunk = connection.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in chunk:
            break
    if not buffer or len(buffer) > 64 * 1024:
        raise UpdateError("update-agent response is invalid")
    try:
        return json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("update-agent response is invalid") from error


def _checksum_for(path: Path, target_name: str) -> str:
    """SHA256SUMSから対象archive名の一意なchecksumを取り出す。"""
    matches: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        # 別ディレクトリの同名ファイルをbasenameへ丸めて受け入れると、
        # archive本体と異なるchecksum行を検証済みと誤認する。生成元の
        # SHA256SUMSに記載された相対名をそのまま比較する。
        if len(fields) == 2 and fields[1] == target_name:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise UpdateError("SHA256SUMS has no unique checksum for archive")
    return matches[0]


def _normalize_version(tag: str) -> str:
    """v prefixを除去し、固定asset名へ使えるVersionにする。"""
    version = tag.removeprefix("v")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", version):
        raise UpdateError("release tag is invalid")
    return version


def _is_github_asset_url(url: str, repository: str) -> bool:
    """公開GitHub Release assetの初期URLだけを許可する。"""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(f"/{repository}/releases/download/")
    )


def _version_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """本製品のnumeric release/alpha/beta/rc Versionを比較用へ変換する。

    `packaging` はruntime dependencyへ追加せず、生成するVersion文法を明示的に
    制限する。リリース本体は各ドット区切りが数字、pre-releaseはa/b/rcと
    数字だけを許し、誤った辞書順比較で更新を拒否しない。
    """
    normalized = version.removeprefix("v")
    match = re.fullmatch(
        r"(?P<release>\d+(?:\.\d+)*)(?P<pre>a|b|rc)?(?P<serial>\d+)?",
        normalized,
    )
    if match is None:
        raise ValueError("version must be a numeric release or a/b/rc prerelease")
    release_parts = [int(part) for part in match.group("release").split(".")]
    while len(release_parts) > 1 and release_parts[-1] == 0:
        release_parts.pop()
    release = tuple(release_parts)
    pre = match.group("pre")
    if pre is None:
        if match.group("serial") is not None:
            raise ValueError("stable version must not include a prerelease serial")
        return release, 3, 0
    if match.group("serial") is None:
        raise ValueError("prerelease version must include a serial number")
    pre_rank = {"a": 0, "b": 1, "rc": 2}[pre]
    return release, pre_rank, int(match.group("serial"))


__all__ = [
    "GitHubReleaseClient",
    "ReleaseAsset",
    "ReleaseInfo",
    "UpdateError",
    "download_and_verify",
    "is_newer_version",
    "launch_updater",
    "validate_update_candidate",
]
