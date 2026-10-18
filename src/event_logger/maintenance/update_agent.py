"""Web service userからroot updaterへ安全に依頼を中継する常駐agent。"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_APPLY_LOCK = Path("/run/lock/event-logger-apply.lock")

MAX_MESSAGE_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")


class UpdateAgent:
    """固定work directory内の検証済みassetだけをupdaterへ渡す。"""

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str],
        helper_path: str | os.PathLike[str],
        work_dir: str | os.PathLike[str],
        repository: str | None = None,
        verified_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """socket、root helper、asset staging領域を受け取る。

        `repository`はroot service側の環境設定からだけ受け取り、Web userが
        socket依頼で変更できない。root agent自身がGitHubのchecksumを再取得し、
        service userが置いたchecksumファイルを信頼しない境界を作る。

        Args:
            socket_path: 接続先またはlistenするUnix socket path。
            helper_path: 起動を許可する固定updater helper path。
            work_dir: assetを配置してよい作業directory。
            repository: この処理が利用する保存・照会Repository。
            verified_dir: root所有でservice userが書換できないstaging directory。

        Returns:
            None。
        """
        self._socket_path = Path(socket_path)
        self._helper_path = Path(helper_path)
        self._work_dir = Path(work_dir)
        configured_verified_dir = verified_dir or os.environ.get(
            "EVENT_LOGGER_UPDATE_VERIFIED_DIR"
        )
        # work_dirはWeb service userがarchiveを置く領域なので、その親へ
        # verifiedを作ると親directoryからunlink/raceできる。既定値は
        # service userが書けない専用root配下に置き、明示設定時も同じ
        # root-only検査を通してから使用する。
        self._verified_dir = Path(
            configured_verified_dir or "/var/lib/event-logger-update/verified"
        )
        self._repository = repository or os.environ.get(
            "EVENT_LOGGER_UPDATE_REPOSITORY", ""
        )
        self._process: subprocess.Popen[bytes] | None = None

    def serve_forever(self) -> None:
        """Unix socketをroot serviceとして待ち受ける。

        Args:
            なし。

        Returns:
            None。

        Raises:
            FileExistsError: 同じpathに許可されない既存file/socketがある場合。
        """
        self._work_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._prepare_verified_dir()
        if self._socket_path.exists():
            if not self._socket_path.is_socket():
                raise FileExistsError(
                    f"update socket is not a socket: {self._socket_path}"
                )
            self._socket_path.unlink()
        self._socket_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o660)
            _set_socket_group(self._socket_path)
            server.listen(4)
            while True:
                connection, _ = server.accept()
                with connection:
                    # root側のGitHub checksum再検証に数秒かかる場合がある。
                    # Web clientの依頼を先に切断し、検証後に更新だけ進む状態を
                    # 作らないため、IPC timeoutはnetwork timeoutより長くする。
                    connection.settimeout(30.0)
                    response = self._handle_connection(connection)
                    try:
                        connection.sendall(_encode_response(response))
                    except OSError:
                        # Web側のtimeout・切断はagentの待受継続を妨げない。
                        continue

    def _handle_connection(self, connection: socket.socket) -> dict[str, object]:
        """1接続のJSON依頼を検証してupdaterを起動する。

        Args:
            connection: この関数が使うSQLite connection。

        Returns:
            クライアントへ返すagent応答mapping。更新要求の受理状態または拒否理由を含む。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        staging: Path | None = None
        try:
            request = _read_request(connection)
            # 再起動は配布元未設定でも使える固定操作。Webからcommandや
            # systemd unit名を渡せる汎用実行口にはしない。
            if isinstance(request, dict) and request.get("action") == "reboot":
                if request != {"action": "reboot"}:
                    raise ValueError("再起動要求に余分な項目があります。")
                return self._schedule_reboot()
            archive, _checksum, version = self._validate_request(request)
            self._validate_helper()
            if self._process is not None and self._process.poll() is None:
                raise ValueError("another update is already running")
            staged_archive, staged_checksum, staging_path = self._stage_trusted_archive(
                archive, version
            )
            staging = staging_path
            # WebからIDを受け取らず、実際に起動する管理側プロセスへ発行する。
            attempt_id = secrets.token_hex(16)
            process = subprocess.Popen(
                [
                    str(self._helper_path),
                    "--archive",
                    str(staged_archive),
                    "--sha256",
                    str(staged_checksum),
                    "--version",
                    version,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env={**os.environ, "EVENT_LOGGER_UPDATE_ATTEMPT_ID": attempt_id},
            )
            self._process = process
            threading.Thread(
                target=_remove_staging_after,
                args=(process, staging_path),
                daemon=True,
            ).start()
            return {
                "ok": True,
                "started": True,
                "attempt_id": attempt_id,
                "assets_staged": True,
            }
        except (OSError, ValueError) as error:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "message": str(error)}

    def _schedule_reboot(self) -> dict[str, object]:
        """更新と排他し、応答を返した後にsystemdへ通常の本体再起動を依頼する。

        Args:
            なし。

        Returns:
            クライアントへ返すagent応答mapping。更新要求の受理状態または拒否理由を含む。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if self._process is not None and self._process.poll() is None:
            raise ValueError("更新中は再起動できません。完了までお待ちください。")
        try:
            _APPLY_LOCK.mkdir()
        except FileExistsError as error:
            raise ValueError("更新または再起動の処理中です。") from error
        try:
            # 通常のrebootはloggerdの終了処理を通す。forceや電源断を使わず、
            # Webへの応答猶予を設ける。agent自身のcgroupとは別unitで実行する。
            # 起動失敗・reboot要求の終了時にも共有lockを残さない。
            subprocess.run(
                [
                    "/usr/bin/systemd-run",
                    "--unit=event-logger-reboot",
                    "--on-active=3s",
                    "--timer-property=AccuracySec=1s",
                    "--property=Type=exec",
                    "--property=ExecStopPost=/usr/bin/rmdir " + str(_APPLY_LOCK),
                    "/usr/bin/systemctl",
                    "reboot",
                ],
                check=True,
                capture_output=True,
                timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            _APPLY_LOCK.rmdir()
            raise ValueError("本体の再起動を予約できませんでした。") from error
        return {"ok": True, "started": True}

    def _validate_request(self, request: object) -> tuple[Path, Path, str]:
        """asset path、checksum path、versionの固定境界を検証する。

        Args:
            request: 検証済みのloggerd制御要求。

        Returns:
            検証済みarchive、checksumのPathとversion文字列のtuple。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if not isinstance(request, dict):
            raise ValueError("update request must be an object")
        if set(request) != {"archive", "checksum", "version"}:
            raise ValueError("update request fields are invalid")
        archive_raw = request.get("archive")
        checksum_raw = request.get("checksum")
        version = request.get("version")
        if (
            not isinstance(archive_raw, str)
            or not isinstance(checksum_raw, str)
            or not isinstance(version, str)
            or _VERSION_PATTERN.fullmatch(version) is None
        ):
            raise ValueError("update request values are invalid")
        archive = _safe_asset_path(archive_raw, self._work_dir)
        checksum = _safe_asset_path(checksum_raw, self._work_dir)
        if archive.name != f"event-logger-{version}.tar.zst":
            raise ValueError("archive name does not match version")
        if checksum.name != "SHA256SUMS":
            raise ValueError("checksum filename is invalid")
        for path in (archive, checksum):
            if not path.is_file() or path.is_symlink():
                raise ValueError("update asset must be a regular file")
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("update archive is too large")
        return archive, checksum, version

    def _validate_helper(self) -> None:
        """root helperが差し替え可能な権限でないことを検証する。

        Args:
            なし。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if not self._helper_path.is_file() or not os.access(self._helper_path, os.X_OK):
            raise ValueError("configured update helper is not executable")
        helper_stat = self._helper_path.stat()
        if (
            helper_stat.st_uid != 0
            or helper_stat.st_mode & 0o022
            or self._helper_path.is_symlink()
        ):
            raise ValueError("configured update helper is not root-owned")

    def _prepare_verified_dir(self) -> None:
        """service userから差し替えできないroot専用stagingを作る。

        Args:
            なし。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        for ancestor in (self._verified_dir, *self._verified_dir.parents):
            if ancestor.is_symlink():
                raise ValueError("verified update directory must not contain symlinks")
            if not ancestor.exists():
                continue
            ancestor_stat = ancestor.stat()
            # final directoryだけをroot-ownedにしても、service-owned parent
            # からchildをunlinkしてsymlinkへ置換できる。既存ancestorも
            # root所有かつ非rootの書込み不可であることを確認する。
            if ancestor_stat.st_uid != 0 or ancestor_stat.st_mode & 0o022:
                raise ValueError("verified update directory parent is not root-only")
        if self._verified_dir.exists() and not self._verified_dir.is_dir():
            raise ValueError("verified update directory is not a directory")
        self._verified_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._verified_dir, 0o700)
        directory_stat = self._verified_dir.stat()
        if directory_stat.st_uid != 0 or directory_stat.st_mode & 0o077:
            raise ValueError("verified update directory is not root-only")

    def _stage_trusted_archive(
        self,
        archive: Path,
        version: str,
    ) -> tuple[Path, Path, Path]:
        """archiveをroot-ownedへコピーしてからtrusted checksumを照合する。

        Args:
            archive: trusted checksumと照合するrelease archive file。
            version: update対象releaseのversion文字列。

        Returns:
            root専用領域へstagingしたarchive、checksum、pathのtuple。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        self._prepare_verified_dir()
        staging = Path(
            tempfile.mkdtemp(prefix=f"event-logger-{version}-", dir=self._verified_dir)
        )
        try:
            staged_archive = staging / archive.name
            _copy_update_archive(archive, staged_archive)
            os.chmod(staged_archive, 0o600)
            expected = self._trusted_checksum(version, staged_archive.name)
            if _sha256_file(staged_archive).lower() != expected.lower():
                raise ValueError("archive does not match trusted GitHub checksum")
            staged_checksum = staging / "SHA256SUMS"
            staged_checksum.write_text(
                f"{expected}  {staged_archive.name}\n",
                encoding="ascii",
            )
            os.chmod(staged_checksum, 0o600)
            return staged_archive, staged_checksum, staging
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _verify_trusted_archive(self, archive: Path, version: str) -> None:
        """GitHubの固定repository checksumとarchive本体をroot側で照合する。

        Args:
            archive: trusted checksumと照合するrelease archive file。
            version: update対象releaseのversion文字列。

        Returns:
            None。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        expected = self._trusted_checksum(version, archive.name)
        actual = _sha256_file(archive)
        if actual.lower() != expected.lower():
            raise ValueError("archive does not match trusted GitHub checksum")

    def _trusted_checksum(self, version: str, archive_name: str) -> str:
        """GitHub Releaseから対象archiveのchecksumを取得する。

        Args:
            version: update対象releaseのversion文字列。
            archive_name: Release内で一意にchecksumを探すarchive filename。

        Returns:
            検証済みまたは生成済みの文字列を返す。

        Raises:
            ValueError: 引数の形式・範囲が仕様外、または現在
                の状態では要求を適用できない場合。
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self._repository):
            raise ValueError("trusted update repository is not configured")
        api_url = f"https://api.github.com/repos/{self._repository}/releases/latest"
        request = Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "event-logger-update-agent",
            },
        )
        try:
            with urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ValueError(
                f"trusted release could not be fetched: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise ValueError("trusted release response is invalid")
        tag = payload.get("tag_name")
        assets = payload.get("assets")
        if not isinstance(tag, str) or not isinstance(assets, list):
            raise ValueError("trusted release response is incomplete")
        if payload.get("draft") is True or payload.get("prerelease") is True:
            raise ValueError("trusted release is not stable")
        if tag.removeprefix("v") != version:
            raise ValueError("requested version is not the current trusted release")
        checksum_url: str | None = None
        for raw_asset in assets:
            if not isinstance(raw_asset, dict):
                continue
            if raw_asset.get("name") == "SHA256SUMS":
                raw_url = raw_asset.get("browser_download_url")
                if isinstance(raw_url, str):
                    checksum_url = raw_url
                break
        if checksum_url is None or not checksum_url.startswith(
            f"https://github.com/{self._repository}/releases/download/"
        ):
            raise ValueError("trusted checksum asset is invalid")
        checksum_request = Request(
            checksum_url,
            headers={"User-Agent": "event-logger-update-agent"},
        )
        try:
            with urlopen(checksum_request, timeout=5.0) as response:
                checksum_bytes = response.read(16 * 1024 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise ValueError(
                f"trusted checksum could not be fetched: {error}"
            ) from error
        if len(checksum_bytes) > 16 * 1024 * 1024:
            raise ValueError("trusted checksum asset is too large")
        return _checksum_for_bytes(checksum_bytes, archive_name)


def _safe_asset_path(raw_path: str, work_dir: Path) -> Path:
    """work directory配下の実在pathへ解決する。

    Args:
        raw_path: 要求から受け取った未検証staging path。
        work_dir: assetを配置してよい作業directory。

    Returns:
        指定work_dir内の通常fileとして確認された絶対Path。

    Raises:
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("update asset path must be absolute")
    if path.is_symlink():
        raise ValueError("update asset path must not be a symlink")
    resolved_root = work_dir.resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("update asset path is outside work directory") from error
    return resolved.resolve(strict=True)


def _read_request(connection: socket.socket) -> object:
    """agent requestをnewline JSONとして読む。

    Args:
        connection: この関数が使うSQLite connection。

    Returns:
        newline JSONをdecodeしたrequest object。

    Raises:
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    buffer = bytearray()
    while len(buffer) <= MAX_MESSAGE_BYTES:
        chunk = connection.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in chunk:
            break
    if not buffer or len(buffer) > MAX_MESSAGE_BYTES:
        raise ValueError("update request is invalid")
    try:
        return json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("update request is invalid") from error


def _encode_response(response: dict[str, object]) -> bytes:
    """agent responseをnewline JSONへ変換する。

    Args:
        response: newline JSONで返す応答mapping。

    Returns:
        newline終端を含むUTF-8 JSON bytes。
    """
    return json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"


def _set_socket_group(path: Path) -> None:
    """service user groupへsocketを共有する（存在しない環境ではrootのまま）。

    Args:
        path: 対象fileまたはUnix socketのfilesystem path。

    Returns:
        None。
    """
    try:
        group_id = grp.getgrnam("event-logger").gr_gid
    except KeyError:
        return
    os.chown(path, 0, group_id)


def _checksum_for_bytes(raw: bytes, target_name: str) -> str:
    """SHA256SUMS本文から対象名の一意なdigestを取得する。

    Args:
        raw: 検証前のchecksumまたはIPC bytes。
        target_name: checksum一覧で検索するasset filename。

    Returns:
        対象archive名に対応するtrusted SHA-256 digest。

    Raises:
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    matches: list[str] = []
    for line in raw.decode("ascii").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == target_name:
            matches.append(fields[0])
    if len(matches) != 1 or re.fullmatch(r"[0-9A-Fa-f]{64}", matches[0]) is None:
        raise ValueError("trusted checksum entry is invalid")
    return matches[0]


def _sha256_file(path: Path) -> str:
    """ファイルをstreamでSHA-256計算する。

    Args:
        path: 対象fileまたはUnix socketのfilesystem path。

    Returns:
        対象file全体のSHA-256 hex digest。
    """
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_update_archive(source: Path, destination: Path) -> None:
    """検証後の差替えを想定し、開いた通常fileだけを上限付きでコピーする。

    Args:
        source: 読み込み元fileまたは記録上の出力source。
        destination: 生成またはcopyする出力file path。

    Returns:
        None。

    Raises:
        ValueError: 引数の形式・範囲が仕様外、または現在
            の状態では要求を適用できない場合。
    """
    # Web userのstagingはpath検証後も書換え可能。最終pathのsymlinkを辿らず、
    # FIFOへ差し替わってもopenで停止しない。判定対象は同じfdの実体とする。
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as archive:
        source_stat = os.fstat(archive.fileno())
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("update archive must be a regular file")
        if source_stat.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("update archive is too large")
        with destination.open("xb") as staged:
            remaining = MAX_ARCHIVE_BYTES
            while True:
                # fstat後に伸び続けるfileも上限で打ち切り、root領域を無制限に
                # 消費させない。正当性はコピー完了後にtrusted checksumで検証する。
                chunk = archive.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError("update archive is too large")
                staged.write(chunk)
                remaining -= len(chunk)


def _remove_staging_after(process: subprocess.Popen[bytes], staging: Path) -> None:
    """root helper終了後にverified archiveを削除する。

    Args:
        process: staging削除前に終了を待つupdater process。
        staging: helper終了後に削除する一時directory。

    Returns:
        None。
    """
    try:
        process.wait()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    """環境変数・引数からroot update-agentを起動する。

    Args:
        なし。

    Returns:
        None。
    """
    parser = argparse.ArgumentParser(description="event-logger root update agent")
    parser.add_argument("--socket", default="/run/event-logger-update/update.sock")
    parser.add_argument(
        "--helper", default="/opt/event-logger/current/packaging/update.sh"
    )
    parser.add_argument("--work-dir", default="/var/lib/event-logger/updates")
    parser.add_argument(
        "--verified-dir",
        default=os.environ.get(
            "EVENT_LOGGER_UPDATE_VERIFIED_DIR",
            "/var/lib/event-logger-update/verified",
        ),
    )
    args = parser.parse_args()
    UpdateAgent(
        socket_path=args.socket,
        helper_path=args.helper,
        work_dir=args.work_dir,
        repository=os.environ.get("EVENT_LOGGER_UPDATE_REPOSITORY"),
        verified_dir=args.verified_dir,
    ).serve_forever()


if __name__ == "__main__":
    main()
