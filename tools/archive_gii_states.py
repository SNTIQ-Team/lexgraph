#!/usr/bin/env python3
"""Archive complete GII snapshots in the official federal state CAS."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from official_states import (
    DEFAULT_SNAPSHOTS,
    DEFAULT_STORE,
    StateStoreError,
    archive_gii_states,
    discover_gii_snapshots,
    transitions,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Archive complete GII states without inferring legal dates")
    parser.add_argument(
        "snapshots", nargs="*", type=Path,
        help="dated GII snapshot directories; default: discover all")
    parser.add_argument("--snapshots-root", type=Path,
                        default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    args = parser.parse_args(argv)
    snapshots = (args.snapshots or
                 discover_gii_snapshots(args.snapshots_root))
    try:
        manifest = archive_gii_states(snapshots, args.store)
        change_rows = transitions(manifest, args.store)
    except StateStoreError as exc:
        print(f"official-state archive failed: {exc}", file=sys.stderr)
        return 1
    print(f"{len(snapshots)} GII snapshots -> {args.store}")
    print(f"  {len(manifest['observations'])} observations")
    print(f"  {len(manifest['objects'])} immutable states")
    print(f"  {len(change_rows)} observed transitions")
    return 0


if __name__ == "__main__":
    sys.exit(main())

