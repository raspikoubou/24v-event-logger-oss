"""公開用のsource archiveとSHA256SUMSを再現可能に生成する。

Build専用ツールで、製品runtimeには含めない。Python 3.14の標準zstd対応tarfileを
使うため、生成ホストにはPython 3.14以降が必要。製品runtimeはPython 3.13のまま。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("LICENSE", "pyproject.toml")
ROOT_DIRECTORIES = ("packaging", "src")
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", ".venv"}
)
EXCLUDED_FILE_NAMES = frozenset(
    {
        "create_release_asset.py",
        "check_public_tree.py",
        "public_files.json",
        "build_frontend.cjs",
        "check_release_notes.py",
        "test_write_diagnostics.py",
        "test_write_diagnostics.cjs",
        "test_auth_sessions.py",
        "test_auth_sessions.cjs",
        "test_update_monitor.cjs",
        "test_duration_display.cjs",
        "test_timeline_scroll.cjs",
        "test_update_status.py",
        "test_release_asset.py",
        "test_configuration_transactions.py",
        "test_frontend_policies.cjs",
        "test_release_boundaries.py",
        "test_shutdown_boundaries.py",
        "test_update_staging.py",
    }
)
FORBIDDEN_FILE_NAMES = frozenset({".env", "event-logger.env"})
FORBIDDEN_FILE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
)
SOURCE_DATE_EPOCH_NAME = "SOURCE_DATE_EPOCH"
VERSION_PATTERN = re.compile(r"^[0-9][A-Za-z0-9.+_-]*$")


def _read_source_date_epoch() -> int:
    """SOURCE_DATE_EPOCHを検証して返す。

        Args:
            なし。

        Returns:
            SOURCE_DATE_EPOCHのUnix epoch秒整数。未設定時は既定値、
                負値・不正値はValueError。
    """
    raw_value = os.environ.get(SOURCE_DATE_EPOCH_NAME)
    if raw_value is None:
        raise ValueError(f"{SOURCE_DATE_EPOCH_NAME} must be set explicitly")
    try:
        epoch = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{SOURCE_DATE_EPOCH_NAME} must be an integer") from error
    if epoch < 0:
        raise ValueError(f"{SOURCE_DATE_EPOCH_NAME} must not be negative")
    return epoch


def _read_version() -> str:
    """pyproject.tomlの製品Versionを読み込む。

        Args:
            なし。

        Returns:
            pyproject.tomlから読み取ったdistribution version文字列。
    """
    project_config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    version = project_config["project"]["version"]
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("pyproject.tomlのVersionがasset名に使えません")
    return version


def _archive_entries() -> list[tuple[PurePosixPath, Path, bool]]:
    """release allow-listから安全なarchive entryを作る。

        Args:
            なし。

        Returns:
            allowlistに合致した(relative archive path, source path, is_directory)
                tuple列。
                symlink・想定外pathは含めない。
    """
    entries: list[tuple[PurePosixPath, Path, bool]] = []
    # root直下は個別allow-listで追加し、directory treeはsymlinkを辿らず走査する。
    # 配布に不要なcacheは除外し、禁止候補が見つかればarchive生成前に止める。
    for name in ROOT_FILES:
        path = PROJECT_ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"release fileが通常fileではありません: {name}")
        entries.append((PurePosixPath(name), path, False))

    for name in ROOT_DIRECTORIES:
        root = PROJECT_ROOT / name
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"release directoryがありません: {name}")
        entries.append((PurePosixPath(name), root, True))
        for current, directory_names, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            kept_directories: list[str] = []
            for child_name in sorted(directory_names):
                if child_name in EXCLUDED_DIRECTORY_NAMES:
                    continue
                child_path = current_path / child_name
                if child_path.is_symlink():
                    raise ValueError(
                        f"release directoryにsymlinkがあります: {child_path}"
                    )
                kept_directories.append(child_name)
                relative_path = PurePosixPath(
                    child_path.relative_to(PROJECT_ROOT).as_posix()
                )
                entries.append((relative_path, child_path, True))
            directory_names[:] = kept_directories

            for filename in sorted(filenames):
                if (
                    filename in EXCLUDED_FILE_NAMES
                    or filename == ".DS_Store"
                    or filename.endswith(".pyc")
                ):
                    continue
                if filename in FORBIDDEN_FILE_NAMES or filename.endswith(
                    FORBIDDEN_FILE_SUFFIXES
                ):
                    raise ValueError(
                        f"release範囲に機密/DB候補fileがあります: {filename}"
                    )
                file_path = current_path / filename
                if file_path.is_symlink() or not file_path.is_file():
                    raise ValueError(
                        f"release fileが通常fileではありません: {file_path}"
                    )
                relative_path = PurePosixPath(
                    file_path.relative_to(PROJECT_ROOT).as_posix()
                )
                entries.append((relative_path, file_path, False))

    # OS走査順に依存させずarchive member順を固定して再現可能な入力列にする。
    return sorted(entries, key=lambda entry: entry[0].as_posix())


def _add_entry(
    archive: tarfile.TarFile,
    relative_path: PurePosixPath,
    source_path: Path,
    is_directory: bool,
    source_date_epoch: int,
) -> None:
    """正規化したowner、mode、時刻で1 entryをtarへ追加する。

Args:
    archive: entryを追加する開いたtar archive writer。
    relative_path: archive root基準のentry path。
    source_path: entryの内容を読むworkspace上のsource path。
    is_directory: entryがdirectoryかを表す真偽値。
    source_date_epoch: 配布archiveのmtimeに使う検証済みUnix epoch秒。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    info = tarfile.TarInfo(relative_path.as_posix())
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = source_date_epoch
    if is_directory:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
        archive.addfile(info)
        return

    source_mode = stat.S_IMODE(source_path.stat().st_mode)
    info.type = tarfile.REGTYPE
    info.mode = 0o755 if source_mode & 0o111 else 0o644
    info.size = source_path.stat().st_size
    with source_path.open("rb") as source_file:
        archive.addfile(info, source_file)


def _write_archive(
    path: Path, entries: list[tuple[PurePosixPath, Path, bool]], epoch: int
) -> None:
    """正規化したsource allow-listをzstd tarへ書く。

Args:
    path: 読み書きするfileのpath。呼出元で認可・配置を限定した値。
    entries: archiveへ格納する許可済みentry列。
    epoch: archive entryに設定する正規化済みUnix epoch秒。

Returns:
    なし。結果は保存、応答送信、route登録などの副作用で反映する。"""
    # mypy typeshedの一部にはPython 3.14のw:zst overloadがまだ含まれない。
    with tarfile.open(  # type: ignore[call-overload]
        path, mode="w:zst", format=tarfile.PAX_FORMAT, level=19
    ) as archive:
        for relative_path, source_path, is_directory in entries:
            _add_entry(archive, relative_path, source_path, is_directory, epoch)


def _sha256(path: Path) -> str:
    """fileのSHA-256を返す。

        Args:
            path: 読み書きするfileのpath。呼出元で認可・配置を限定した値。

        Returns:
            file bytesから計算したlowercase SHA-256 hex digest。
    """
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_release_asset(
    output_directory: Path, source_date_epoch: int
) -> tuple[Path, Path]:
    """release tar.zstと対応するSHA256SUMSを生成する。

        Args:
            output_directory: 生成物の保存先。
            source_date_epoch: 全archive entryへ設定する固定時刻。

        Returns:
            作成したtar.zst pathとSHA256SUMS pathの組。
                allowlist検証やfile IO失敗は例外。
    """
    if sys.version_info < (3, 14):
        raise RuntimeError("release assetの生成にはPython 3.14以降が必要です")
    if source_date_epoch < 0:
        raise ValueError("source_date_epoch must not be negative")

    # Versionとallow-listを検証し、既存成果物を上書きしない名前を先に決める。
    # 一時archiveとchecksumを作る前に入力境界を確定する。
    version = _read_version()
    archive_name = f"event-logger-{version}.tar.zst"
    archive_path = output_directory / archive_name
    checksum_path = output_directory / "SHA256SUMS"
    if archive_path.exists() or checksum_path.exists():
        raise FileExistsError("出力先にarchiveまたはSHA256SUMSが既にあります")
    output_directory.mkdir(parents=True, exist_ok=True)
    entries = _archive_entries()

    archive_temporary: Path | None = None
    checksum_temporary: Path | None = None
    checksum_published = False
    # 完成物は同じ出力directoryのtemp fileで作り、checksumとarchiveのrenameだけを
    # 公開境界にする。途中失敗なら一方だけ残さずtempもcleanupする。
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_directory, delete=False
        ) as temporary:
            archive_temporary = Path(temporary.name)
        _write_archive(archive_temporary, entries, source_date_epoch)
        checksum_line = f"{_sha256(archive_temporary)}  {archive_name}\n"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=output_directory, delete=False
        ) as temporary:
            checksum_temporary = Path(temporary.name)
            temporary.write(checksum_line)
        os.replace(checksum_temporary, checksum_path)
        checksum_temporary = None
        checksum_published = True
        os.replace(archive_temporary, archive_path)
        archive_temporary = None
    except Exception:
        if checksum_published:
            checksum_path.unlink(missing_ok=True)
        raise
    finally:
        if archive_temporary is not None:
            archive_temporary.unlink(missing_ok=True)
        if checksum_temporary is not None:
            checksum_temporary.unlink(missing_ok=True)

    return archive_path, checksum_path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

        Args:
            argv: CLIへ渡されたargument列。Noneならprocess既定のargvを使う。

        Returns:
            CLI終了code。作成成功は0、生成失敗は非0。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "dist", help="生成物の保存先"
    )
    args = parser.parse_args(argv)
    try:
        epoch = _read_source_date_epoch()
        archive_path, checksum_path = create_release_asset(args.output_dir, epoch)
    except (
        FileExistsError,
        OSError,
        RuntimeError,
        ValueError,
        tarfile.TarError,
    ) as error:
        print(f"release asset generation failed: {error}", file=sys.stderr)
        return 1
    print(f"created {archive_path}")
    print(f"created {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
