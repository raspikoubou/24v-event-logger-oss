"""公開範囲をindexと全ローカルGit履歴から検査する開発専用ツール。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "packaging/public_files.json"
SECRET_PATTERNS = (
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"gh[pousr]_[A-Za-z0-9]{30,}",
    rb"github_pat_[A-Za-z0-9_]{40,}",
    rb"AKIA[0-9A-Z]{16}",
    rb"/Us" + rb"ers/[^/\s]+/",
    rb"munetaka" + rb"sanada|munemune" + rb"1104",
)
INTERNAL_LINK = re.compile(
    r"(?:docs/)?0[0-8]_[^\s)]+\.md|利用ガイド\.md|"
    r"(?:docs/)?(?:証跡|参考)/|リリース作業\.md"
)
FORBIDDEN_PARTS = {
    "AGENTS.md",
    "tests",
    "qa",
    "sites-demo",
    "__pycache__",
    ".env",
    "event-logger.env",
    ".DS_Store",
}
FORBIDDEN_SUFFIXES = (".pyc", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".img")


def git(*arguments: str) -> bytes:
    """shellを介さず、検査対象repositoryのGit結果を取得する。"""
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments])


# 公開対象から除外すべきファイルがGit履歴に残っていないか確認する。
def inspect(revision: str, approved: set[str]) -> list[str]:
    """snapshotのfile名、mode、内容を検査し、値を含まない診断を返す。"""
    errors: list[str] = []
    args = (
        ["ls-files", "--stage", "-z"]
        if revision == ":"
        else [
            "ls-tree",
            "-r",
            "-z",
            revision,
        ]
    )
    for record in git(*args).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = (
            metadata.split()
            if revision == ":"
            else (metadata.split()[0], metadata.split()[2], b"0")
        )
        name = raw_path.decode("utf-8")
        path = PurePosixPath(name)
        if name not in approved:
            errors.append(f"{revision}: unapproved path: {name}")
        if (
            set(path.parts) & FORBIDDEN_PARTS
            or name.endswith(FORBIDDEN_SUFFIXES)
            or any(
                part.startswith(".env") and not part.endswith(".example")
                for part in path.parts
            )
        ):
            errors.append(f"{revision}: forbidden path: {name}")
        if mode not in {b"100644", b"100755"} or stage != b"0":
            errors.append(f"{revision}: non-regular/unmerged entry: {name}")
            continue
        content = git("cat-file", "blob", object_id.decode("ascii"))
        if any(re.search(pattern, content) for pattern in SECRET_PATTERNS):
            errors.append(f"{revision}: credential/personal-data candidate: {name}")
        if path.suffix in {".md", ".html"} and INTERNAL_LINK.search(
            content.decode("utf-8", errors="replace")
        ):
            errors.append(f"{revision}: internal document reference: {name}")
    return errors


# 公開ファイルの許容リストを読み込んで検査する。
def main() -> int:
    """公開snapshot検査、またはローカルGit hook設定を実行する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--install-hooks", action="store_true")
    args = parser.parse_args()
    if args.install_hooks:
        hook_dir = (
            Path(git("rev-parse", "--absolute-git-dir").decode().strip()) / "hooks"
        )
        hook_dir.mkdir(exist_ok=True)
        for name, option in [("pre-commit", "--staged"), ("pre-push", "--history")]:
            target = hook_dir / name
            body = f"#!/bin/sh\nexec python3 packaging/check_public_tree.py {option}\n"
            if target.exists() and target.read_text() != body:
                raise FileExistsError(f"既存hookは上書きしません: {target}")
            target.write_text(body)
            target.chmod(0o755)
        print("Public scope hooks installed")
        return 0
    # index内のmanifestを使い、未stageの編集を誤って検査へ反映しない。
    manifest_ref = f":{MANIFEST}" if args.staged else f"HEAD:{MANIFEST}"
    approved = set(json.loads(git("show", manifest_ref)))
    revisions = [":"] if args.staged else ["HEAD"]
    if args.history:
        revisions = git("rev-list", "--all").decode().splitlines()
    if not revisions:
        raise ValueError("検査対象のcommitがありません")
    errors = [error for revision in revisions for error in inspect(revision, approved)]
    for error in errors:
        print(error)
    if errors:
        return 1
    print(
        f"Public scope passed: {len(revisions)} snapshot(s), "
        f"{len(approved)} approved paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
