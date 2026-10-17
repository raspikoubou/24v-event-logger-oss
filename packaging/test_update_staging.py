"""検証後に置き換わった更新archiveのコピー境界を確認する。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from event_logger.maintenance import update_agent
from event_logger.maintenance.update_agent import UpdateAgent


@pytest.mark.parametrize("replacement", ["symlink", "oversize", "unchanged"])
def test_staging_revalidates_opened_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """path検証後も、開いたfileの種類と実際のコピー量を検証する。"""
    work = tmp_path / "work"
    work.mkdir()
    verified = tmp_path / "verified"
    verified.mkdir()
    archive = work / "event-logger-1.0.2.tar.zst"
    archive.write_bytes(b"original")
    checksum = work / "SHA256SUMS"
    checksum.write_text("unused")
    agent = UpdateAgent(
        socket_path=tmp_path / "socket",
        helper_path=tmp_path / "helper",
        work_dir=work,
        verified_dir=verified,
    )
    monkeypatch.setattr(update_agent, "MAX_ARCHIVE_BYTES", 8)
    source, _, version = agent._validate_request(
        {
            "archive": str(archive),
            "checksum": str(checksum),
            "version": "1.0.2",
        }
    )

    def prepare() -> None:
        """元path検証とrootコピーの間の差替えを再現する。"""
        if replacement == "symlink":
            outside = tmp_path / "outside"
            outside.write_bytes(b"original")
            archive.unlink()
            archive.symlink_to(outside)
        elif replacement == "oversize":
            archive.write_bytes(b"replaced!")

    monkeypatch.setattr(agent, "_prepare_verified_dir", prepare)
    monkeypatch.setattr(
        agent,
        "_trusted_checksum",
        lambda *_: hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    if replacement == "unchanged":
        staged, sums, _ = agent._stage_trusted_archive(source, version)
        assert staged.read_bytes() == b"original"
        assert sums.read_text().startswith(hashlib.sha256(b"original").hexdigest())
    else:
        with pytest.raises((OSError, ValueError)):
            agent._stage_trusted_archive(source, version)
        assert list(verified.iterdir()) == []


def test_archive_growth_after_fstat_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open後のサイズ確認を通っても、上限を越えた実データはコピーしない。"""
    archive = tmp_path / "archive"
    archive.write_bytes(b"123456789")
    monkeypatch.setattr(update_agent, "MAX_ARCHIVE_BYTES", 8)
    monkeypatch.setattr(
        "event_logger.maintenance.update_agent.os.fstat",
        lambda _: Mock(st_mode=stat.S_IFREG, st_size=1),
    )
    with pytest.raises(ValueError, match="too large"):
        update_agent._copy_update_archive(archive, tmp_path / "copy")
    assert (tmp_path / "copy").stat().st_size <= 8


def test_fifo_archive_is_rejected_without_waiting(tmp_path: Path) -> None:
    """FIFOを通常fileと誤認してroot agentの待受を止めない。"""
    archive = tmp_path / "archive"
    os.mkfifo(archive)
    with pytest.raises(ValueError, match="regular file"):
        update_agent._copy_update_archive(archive, tmp_path / "copy")
