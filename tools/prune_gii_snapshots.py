#!/usr/bin/env python3
"""Safely retain a bounded number of raw GII snapshot directories.

Raw ``acts.jsonl``/``norms.jsonl`` snapshots are redundant only after every
act observation has been committed to the cumulative official-state manifest
and every referenced immutable state still verifies.  This tool proves that
boundary before deleting anything.  Non-date, incomplete, and symlinked
directories are never candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import Any

from official_states import (
    DEFAULT_SNAPSHOTS,
    DEFAULT_STORE,
    StateStoreError,
    canonical_json_bytes,
    load_manifest,
    load_state_verified,
    project_snapshot_observations,
)


DEFAULT_KEEP = 2
_ARCHIVED_RAW_FILES = ("acts.jsonl", "norms.jsonl")


def _complete_dated_snapshots(root: Path) -> list[Path]:
    """Return direct, real directories with a valid ISO date and both files."""
    root = Path(root)
    if not root.is_dir():
        return []
    snapshots: list[Path] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            parsed = date.fromisoformat(path.name)
        except ValueError:
            continue
        if parsed.isoformat() != path.name:
            continue
        if not (path / "acts.jsonl").is_file() or not \
                (path / "norms.jsonl").is_file():
            continue
        snapshots.append(path)
    return sorted(snapshots, key=lambda path: path.name)


def _verify_manifest_object(store: Path, manifest: dict[str, Any],
                            digest: str) -> None:
    """Verify CAS bytes, canonical state identity, and pinned metadata."""
    metadata = manifest["objects"].get(digest)
    if not isinstance(metadata, dict):
        raise StateStoreError(
            f"snapshot observation references unknown state {digest}")
    state = load_state_verified(store, digest)
    path = Path(store) / metadata["path"]
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        raise StateStoreError(
            f"cannot read state object metadata target {digest}: {exc}") \
            from exc
    canonical = canonical_json_bytes(state)
    actual = {
        "path": metadata["path"],
        "canonical_bytes": len(canonical),
        "gzip_bytes": len(compressed),
        "gzip_sha256": hashlib.sha256(compressed).hexdigest(),
    }
    if metadata != actual:
        raise StateStoreError(f"manifest metadata mismatch for {digest}")


def verify_snapshot_archived(snapshot: Path, manifest: dict[str, Any],
                             store: Path) -> None:
    """Prove that a complete raw snapshot is durably and readably archived."""
    expected = project_snapshot_observations(snapshot)
    if not expected:
        raise StateStoreError(
            f"refusing to prune empty GII snapshot: {snapshot}")
    # Compare the complete canonical observation, including source,
    # source_slug and source_doknr.  A matching state hash alone is not enough
    # to discard the source files when their provenance metadata differs.
    archived = {
        canonical_json_bytes(row) for row in manifest["observations"]
    }
    missing = [row for row in expected
               if canonical_json_bytes(row) not in archived]
    if missing:
        sample = ", ".join(row["act_id"] for row in missing[:3])
        suffix = "" if len(missing) <= 3 else ", ..."
        raise StateStoreError(
            f"snapshot {snapshot.name} has {len(missing)} unarchived "
            f"observations: {sample}{suffix}")
    for digest in sorted({row["state_sha256"] for row in expected}):
        _verify_manifest_object(Path(store), manifest, digest)


def prune_gii_snapshots(snapshots_root: Path = DEFAULT_SNAPSHOTS,
                        store: Path = DEFAULT_STORE, *,
                        keep: int = DEFAULT_KEEP) -> dict[str, list[Path]]:
    """Prune archived raw snapshots, retaining the newest ``keep``.

    Verification is deliberately two-phase: every deletion candidate must
    pass before the first directory is removed.  A corrupt CAS object or a
    missing observation therefore leaves the entire raw snapshot set intact.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    root = Path(snapshots_root)
    complete = _complete_dated_snapshots(root)
    candidates = complete[:-keep] if len(complete) > keep else []
    retained = complete[-keep:]
    manifest = load_manifest(Path(store))

    for snapshot in candidates:
        verify_snapshot_archived(snapshot, manifest, Path(store))

    # Guard the narrow deletion boundary again after verification.  In
    # particular, never follow a directory that was replaced by a symlink.
    root_resolved = root.resolve()
    for snapshot in candidates:
        if snapshot.is_symlink() or snapshot.parent.resolve() != root_resolved:
            raise StateStoreError(
                f"snapshot deletion boundary changed: {snapshot}")
    for snapshot in candidates:
        # Only these two files are represented by the verified state archive.
        # catalog.jsonl covers the full GII TOC rather than the curated corpus;
        # auxiliary evidence may also be added later.  Preserve both instead
        # of deleting the whole dated directory.
        for name in _ARCHIVED_RAW_FILES:
            (snapshot / name).unlink()
        if not any(snapshot.iterdir()):
            snapshot.rmdir()
    return {
        "complete": complete,
        "retained": retained,
        "pruned": candidates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune raw GII snapshots only after CAS verification")
    parser.add_argument("--snapshots-root", type=Path,
                        default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"newest complete snapshots to retain "
                             f"(default: {DEFAULT_KEEP})")
    args = parser.parse_args(argv)
    try:
        result = prune_gii_snapshots(
            args.snapshots_root, args.store, keep=args.keep)
    except (OSError, StateStoreError, ValueError) as exc:
        print(f"GII raw snapshot retention failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"GII raw snapshots: {len(result['pruned'])} pruned, "
        f"{len(result['retained'])} retained")
    for path in result["pruned"]:
        print(f"  pruned {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
