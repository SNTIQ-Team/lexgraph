"""Durable, content-addressed states from official GII snapshots.

The GII source exposes only the current consolidated text.  Retaining each
retrieval as an immutable state is therefore the evidence boundary for exact
forward history: a transition proves that GII served two complete states on
two retrieval dates, but it does not by itself prove an effective date.

State objects are the same compact act projection used by the web data plane.
Their SHA-256 is calculated over canonical, *uncompressed* UTF-8 JSON.  The
object on disk is a deterministic gzip stream (mtime zero, empty filename).
The manifest is cumulative and atomically replaced; removing an old source
snapshot never removes its observation or CAS object.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOTS = ROOT / "data" / "snapshots" / "gii"
DEFAULT_STORE = ROOT / "data" / "federal_states"
MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
DATE_BASIS = "retrieval_observation_not_effective_date"
VERIFICATION = "exact"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class StateStoreError(ValueError):
    """A source snapshot, manifest, or CAS object failed verification."""


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical bytes used as the state identity."""
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StateStoreError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _sha256(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _deterministic_gzip(data: bytes) -> bytes:
    target = io.BytesIO()
    with gzip.GzipFile(
            filename="", mode="wb", fileobj=target,
            compresslevel=9, mtime=0) as stream:
        stream.write(data)
    return target.getvalue()


def _validate_deterministic_gzip_header(data: bytes) -> None:
    """Verify the stable part of our gzip representation.

    Recompressing and comparing the entire stream would make a durable store
    depend on a particular zlib version.  The writer fixes the header fields;
    the manifest pins the complete compressed-byte hash.
    """
    if len(data) < 18 or data[:3] != b"\x1f\x8b\x08" or data[3] != 0 or \
            data[4:8] != b"\x00\x00\x00\x00":
        raise StateStoreError("state object has a non-deterministic gzip header")


def _object_relpath(digest: str) -> Path:
    if not _DIGEST_RE.fullmatch(digest):
        raise StateStoreError(f"invalid state SHA-256: {digest!r}")
    return Path("objects") / "sha256" / digest[:2] / f"{digest}.json.gz"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write and fsync a sibling temporary file, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _empty_manifest() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "lexgraph-official-federal-state-store",
        "state_identity": "sha256-canonical-uncompressed-json",
        "compression": "gzip-mtime-0-empty-filename",
        "objects": {},
        "observations": [],
    }


def _validate_observation(row: Any) -> None:
    if not isinstance(row, dict):
        raise StateStoreError("manifest observation must be an object")
    required = {
        "act_id": str,
        "jurabk": str,
        "observed_at": str,
        "state_sha256": str,
        "builddate": str,
        "norm_count": int,
        "source_url": str,
        "date_basis": str,
        "verification": str,
    }
    for key, expected in required.items():
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, expected):
            raise StateStoreError(
                f"manifest observation has invalid {key!r}")
    try:
        if date.fromisoformat(row["observed_at"]).isoformat() != \
                row["observed_at"]:
            raise ValueError
    except ValueError as exc:
        raise StateStoreError("observation date must be YYYY-MM-DD") from exc
    if not _DIGEST_RE.fullmatch(row["state_sha256"]):
        raise StateStoreError("observation has invalid state SHA-256")
    if row["norm_count"] < 0:
        raise StateStoreError("observation norm_count must be non-negative")
    if row["date_basis"] != DATE_BASIS:
        raise StateStoreError("observation has unsupported date basis")
    if row["verification"] != VERIFICATION:
        raise StateStoreError("observation has unsupported verification")
    if not row["source_url"].startswith(
            "https://www.gesetze-im-internet.de/"):
        raise StateStoreError("observation source_url is not official GII")


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise StateStoreError("state manifest must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION or \
            manifest.get("kind") != \
            "lexgraph-official-federal-state-store":
        raise StateStoreError("unsupported state manifest schema")
    if manifest.get("state_identity") != \
            "sha256-canonical-uncompressed-json" or \
            manifest.get("compression") != \
            "gzip-mtime-0-empty-filename":
        raise StateStoreError("unsupported state manifest encoding")
    objects = manifest.get("objects")
    observations = manifest.get("observations")
    if not isinstance(objects, dict) or not isinstance(observations, list):
        raise StateStoreError("manifest objects/observations have wrong shape")
    for digest, metadata in objects.items():
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise StateStoreError("manifest contains an invalid object digest")
        if not isinstance(metadata, dict):
            raise StateStoreError("manifest object metadata must be an object")
        expected_path = _object_relpath(digest).as_posix()
        if metadata.get("path") != expected_path:
            raise StateStoreError("manifest object path does not match digest")
        for field in ("canonical_bytes", "gzip_bytes"):
            value = metadata.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StateStoreError(
                    f"manifest object has invalid {field!r}")
        if not isinstance(metadata.get("gzip_sha256"), str) or not \
                _DIGEST_RE.fullmatch(metadata["gzip_sha256"]):
            raise StateStoreError("manifest object has invalid gzip SHA-256")
    for observation in observations:
        _validate_observation(observation)
        if observation["state_sha256"] not in objects:
            raise StateStoreError(
                "manifest observation references an unknown object")
    return manifest


def load_manifest(store: Path = DEFAULT_STORE) -> dict[str, Any]:
    """Load and structurally verify the cumulative manifest.

    A missing store is a valid empty store.  CAS bytes are verified lazily by
    :func:`load_state_verified` and eagerly before any archive update.
    """
    path = Path(store) / MANIFEST_NAME
    if not path.is_file():
        return _empty_manifest()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateStoreError(f"cannot read state manifest: {exc}") from exc
    return _validate_manifest(manifest)


def _validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateStoreError("state object must be a JSON object")
    expected_keys = {
        "id", "jurabk", "juris", "title", "stand", "build",
        "norm_count", "norms",
    }
    if set(state) != expected_keys:
        raise StateStoreError("state is not the exact web act projection")
    required = {
        "id": str, "jurabk": str, "juris": str,
        "build": str, "norm_count": int, "norms": list,
    }
    for key, expected in required.items():
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, expected):
            raise StateStoreError(f"state has invalid {key!r}")
    if state["juris"] != "DE" or not state["id"].startswith("fed_"):
        raise StateStoreError("state is not a federal web-state projection")
    for field in ("title", "stand"):
        if state[field] is not None and not isinstance(state[field], str):
            raise StateStoreError(f"state has invalid {field!r}")
    if state["norm_count"] != len(state["norms"]):
        raise StateStoreError("state norm_count does not match norm bodies")
    for norm in state["norms"]:
        if not isinstance(norm, dict):
            raise StateStoreError("state norm must be an object")
        if set(norm) != {"enbez", "titel", "text", "glied"}:
            raise StateStoreError("state norm is not the exact web projection")
        if any(not isinstance(norm[key], str)
               for key in ("enbez", "titel", "text", "glied")):
            raise StateStoreError("state norm fields must be strings")
    return state


def load_state_verified(store: Path, digest: str) -> dict[str, Any]:
    """Load one CAS object and verify gzip, canonical JSON, and SHA-256."""
    path = Path(store) / _object_relpath(digest)
    try:
        compressed = path.read_bytes()
        _validate_deterministic_gzip_header(compressed)
        canonical = gzip.decompress(compressed)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise StateStoreError(f"cannot read state object {digest}: {exc}") \
            from exc
    if _sha256(canonical) != digest:
        raise StateStoreError(f"state object {digest} hash mismatch")
    try:
        state = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateStoreError(f"state object {digest} is invalid JSON") from exc
    if canonical_json_bytes(state) != canonical:
        raise StateStoreError(f"state object {digest} is not canonical JSON")
    return _validate_state(state)


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StateStoreError(
                        f"{path}:{line_number}: invalid JSON") from exc
                if not isinstance(row, dict):
                    raise StateStoreError(
                        f"{path}:{line_number}: row must be an object")
                rows.append(row)
    except OSError as exc:
        raise StateStoreError(f"cannot read snapshot file {path}: {exc}") \
            from exc
    return rows


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _snapshot_date(path: Path) -> str:
    try:
        parsed = date.fromisoformat(path.name)
    except ValueError as exc:
        raise StateStoreError(
            f"snapshot directory is not YYYY-MM-DD: {path}") from exc
    if parsed.isoformat() != path.name:
        raise StateStoreError(
            f"snapshot directory is not YYYY-MM-DD: {path}")
    return path.name


def _project_snapshot(path: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``(web_state, observation)`` rows for one complete snapshot."""
    path = Path(path)
    observed_at = _snapshot_date(path)
    acts_path, norms_path = path / "acts.jsonl", path / "norms.jsonl"
    if not acts_path.is_file() or not norms_path.is_file():
        raise StateStoreError(f"incomplete GII snapshot directory: {path}")
    acts = _read_jsonl_strict(acts_path)
    norms = _read_jsonl_strict(norms_path)

    by_jurabk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for norm in norms:
        jurabk = norm.get("jurabk")
        if not isinstance(jurabk, str) or not jurabk:
            raise StateStoreError("GII norm is missing jurabk")
        by_jurabk[jurabk].append(norm)

    seen_jurabk: set[str] = set()
    seen_ids: set[str] = set()
    projected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for act in acts:
        jurabk = act.get("jurabk")
        slug = act.get("slug")
        count = act.get("norm_count")
        if not isinstance(jurabk, str) or not jurabk or \
                not isinstance(slug, str) or not slug:
            raise StateStoreError("GII act is missing jurabk/slug")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StateStoreError(f"GII act {jurabk} has invalid norm_count")
        if jurabk in seen_jurabk:
            raise StateStoreError(f"duplicate GII act jurabk: {jurabk}")
        seen_jurabk.add(jurabk)
        source_norms = by_jurabk.get(jurabk, [])
        if len(source_norms) != count:
            raise StateStoreError(
                f"GII act {jurabk} expected {count} norms, "
                f"captured {len(source_norms)}")
        act_id = "fed_" + _slug(jurabk)
        if act_id in seen_ids:
            raise StateStoreError(f"federal act id collision: {act_id}")
        seen_ids.add(act_id)
        web_norms = []
        for norm in source_norms:
            enbez = norm.get("enbez")
            if not isinstance(enbez, str):
                raise StateStoreError(f"GII norm in {jurabk} has no enbez")
            web_norms.append({
                "enbez": enbez,
                "titel": str(norm.get("titel") or ""),
                "text": str(norm.get("text") or ""),
                "glied": str(norm.get("gliederung") or ""),
            })
        builddate = str(act.get("builddate") or "")
        state = {
            "id": act_id,
            "jurabk": jurabk,
            "juris": "DE",
            "title": act.get("long_title"),
            "stand": act.get("stand"),
            "build": builddate[:8],
            "norm_count": count,
            "norms": web_norms,
        }
        _validate_state(state)
        canonical = canonical_json_bytes(state)
        digest = _sha256(canonical)
        observation = {
            "act_id": act_id,
            "jurabk": jurabk,
            "observed_at": observed_at,
            "state_sha256": digest,
            "builddate": builddate,
            "norm_count": count,
            "source_url": f"https://www.gesetze-im-internet.de/{slug}/",
            "date_basis": DATE_BASIS,
            "verification": VERIFICATION,
            "source": "GII",
            "source_slug": slug,
            "source_doknr": str(act.get("doknr") or ""),
        }
        projected.append((state, observation))

    orphans = sorted(set(by_jurabk) - seen_jurabk)
    if orphans:
        raise StateStoreError(
            "GII snapshot has norms without act metadata: "
            + ", ".join(orphans))
    return projected


def discover_gii_snapshots(root: Path = DEFAULT_SNAPSHOTS) -> list[Path]:
    """Discover dated snapshot directories containing both required files."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_dir()
        and (path / "acts.jsonl").is_file()
        and (path / "norms.jsonl").is_file()
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
    )


def project_snapshot_observations(path: Path) -> list[dict[str, Any]]:
    """Project the exact manifest observations for one complete snapshot.

    This is intentionally read-only.  Retention tooling uses it to prove that
    a raw snapshot is represented in the durable manifest before removing the
    redundant source JSONL files.
    """
    return [copy.deepcopy(observation)
            for _state, observation in _project_snapshot(Path(path))]


def _store_state(store: Path, state: dict[str, Any]) -> tuple[str, dict]:
    canonical = canonical_json_bytes(state)
    digest = _sha256(canonical)
    compressed = _deterministic_gzip(canonical)
    path = Path(store) / _object_relpath(digest)
    if path.exists():
        existing = load_state_verified(store, digest)
        if existing != state:
            raise StateStoreError(f"CAS collision for state {digest}")
    else:
        _atomic_write(path, compressed)
    return digest, {
        "path": _object_relpath(digest).as_posix(),
        "canonical_bytes": len(canonical),
        "gzip_bytes": len(compressed),
        "gzip_sha256": _sha256(compressed),
    }


def store_state_object(store: Path, state: dict[str, Any]) -> tuple[str, dict]:
    """Store one validated complete state in a compatible deterministic CAS.

    This does not add an official GII observation or mutate a manifest.  It is
    also used for separately reviewed ``derived_verified`` states whose bytes
    share the same canonical identity and gzip layout but whose provenance
    must remain distinct from source-exact observations.
    """
    _validate_state(state)
    return _store_state(Path(store), state)


def archive_gii_states(snapshot_dirs: Iterable[Path],
                       store: Path = DEFAULT_STORE) -> dict[str, Any]:
    """Merge complete GII snapshots into the cumulative official state store."""
    store = Path(store)
    manifest = copy.deepcopy(load_manifest(store))

    # Existing evidence must remain readable before new observations can be
    # committed.  This turns a missing/corrupt CAS object into a hard failure.
    for digest, metadata in manifest["objects"].items():
        state = load_state_verified(store, digest)
        path = store / _object_relpath(digest)
        compressed = path.read_bytes()
        canonical = canonical_json_bytes(state)
        if metadata != {
            "path": _object_relpath(digest).as_posix(),
            "canonical_bytes": len(canonical),
            "gzip_bytes": len(compressed),
            "gzip_sha256": _sha256(compressed),
        }:
            raise StateStoreError(f"manifest metadata mismatch for {digest}")

    known = {
        canonical_json_bytes(row) for row in manifest["observations"]
    }
    for snapshot in sorted({Path(path) for path in snapshot_dirs},
                           key=lambda path: (path.name, str(path))):
        for state, observation in _project_snapshot(snapshot):
            digest, metadata = _store_state(store, state)
            if digest != observation["state_sha256"]:
                raise StateStoreError("projected state digest changed in storage")
            existing = manifest["objects"].get(digest)
            if existing is not None and existing != metadata:
                raise StateStoreError(
                    f"conflicting manifest metadata for {digest}")
            manifest["objects"][digest] = metadata
            marker = canonical_json_bytes(observation)
            if marker not in known:
                manifest["observations"].append(observation)
                known.add(marker)

    manifest["observations"].sort(key=lambda row: (
        row["act_id"], row["observed_at"], row["state_sha256"],
        row["builddate"],
    ))
    _validate_manifest(manifest)
    _atomic_write(store / MANIFEST_NAME,
                  canonical_json_bytes(manifest) + b"\n")
    return manifest


def _state_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict]:
    old_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    new_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    labels: list[str] = []
    for norm in old["norms"]:
        if norm["enbez"] not in old_groups:
            labels.append(norm["enbez"])
        old_groups[norm["enbez"]].append(norm)
    for norm in new["norms"]:
        if norm["enbez"] not in old_groups and \
                norm["enbez"] not in new_groups:
            labels.append(norm["enbez"])
        new_groups[norm["enbez"]].append(norm)

    # Labels are normally unique.  GII also has legitimate duplicate labels
    # such as two "Anlage" rows and has briefly served parallel § 55/§ 57
    # states.  Match byte-identical rows first, then pair remaining rows in
    # source order.  This prevents one unchanged duplicate from becoming a
    # fabricated replace+delete pair.
    pairs: list[tuple[str, dict[str, str] | None,
                      dict[str, str] | None]] = []
    for label in labels:
        before = list(old_groups.get(label, []))
        after = list(new_groups.get(label, []))
        remaining_after = list(after)
        remaining_before = []
        for row in before:
            try:
                match_at = remaining_after.index(row)
            except ValueError:
                remaining_before.append(row)
            else:
                remaining_after.pop(match_at)
        paired = min(len(remaining_before), len(remaining_after))
        pairs.extend((label, remaining_before[index],
                      remaining_after[index]) for index in range(paired))
        pairs.extend((label, row, None)
                     for row in remaining_before[paired:])
        pairs.extend((label, None, row)
                     for row in remaining_after[paired:])

    changes = []
    for label, before, after in pairs:
        old_present, new_present = before is not None, after is not None
        old_text = before["text"] if before else ""
        new_text = after["text"] if after else ""
        operation = ("add" if not old_present else
                     "delete" if not new_present else "replace")
        changes.append({
            "para": label,
            "old": old_text,
            "new": new_text,
            "old_present": old_present,
            "new_present": new_present,
            "old_sha256": _sha256(old_text),
            "new_sha256": _sha256(new_text),
            "operation": operation,
            # Preserve heading/outline changes that body-only fields cannot
            # express, while keeping the public old/new contract intact.
            "old_title": before["titel"] if before else None,
            "new_title": after["titel"] if after else None,
            "old_glied": before["glied"] if before else None,
            "new_glied": after["glied"] if after else None,
            "old_norm_sha256": (
                _sha256(canonical_json_bytes(before)) if before else None),
            "new_norm_sha256": (
                _sha256(canonical_json_bytes(after)) if after else None),
        })
    return changes


def transitions(manifest: dict[str, Any],
                store: Path = DEFAULT_STORE) -> list[dict[str, Any]]:
    """Return exact adjacent state pairs grouped by federal act.

    Repeated observations of the same state are retained in the manifest but
    collapsed here.  The last repeated observation is the lower boundary for
    the next change.  Two different states on the same retrieval date are
    rejected because their order cannot be proven.
    """
    manifest = _validate_manifest(copy.deepcopy(manifest))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in manifest["observations"]:
        grouped[observation["act_id"]].append(observation)

    output: list[dict[str, Any]] = []
    for act_id, observations in grouped.items():
        observations.sort(key=lambda row: (
            row["observed_at"], row["state_sha256"], row["builddate"]))
        per_day: dict[str, set[str]] = defaultdict(set)
        for row in observations:
            per_day[row["observed_at"]].add(row["state_sha256"])
        ambiguous = [day for day, hashes in per_day.items()
                     if len(hashes) > 1]
        if ambiguous:
            raise StateStoreError(
                f"{act_id} has unordered distinct states on "
                + ", ".join(sorted(ambiguous)))

        previous = observations[0] if observations else None
        for current in observations[1:]:
            if previous is None:
                previous = current
                continue
            if current["state_sha256"] == previous["state_sha256"]:
                previous = current
                continue
            old_state = load_state_verified(
                store, previous["state_sha256"])
            new_state = load_state_verified(store, current["state_sha256"])
            if old_state["id"] != act_id or new_state["id"] != act_id or \
                    old_state["jurabk"] != previous["jurabk"] or \
                    new_state["jurabk"] != current["jurabk"] or \
                    old_state["norm_count"] != previous["norm_count"] or \
                    new_state["norm_count"] != current["norm_count"]:
                raise StateStoreError(
                    f"manifest observation does not match state {act_id}")
            changes = _state_changes(old_state, new_state)
            if not changes:
                # Build/stand metadata is still preserved in both CAS states
                # and observations, but is not a normative text transition.
                previous = current
                continue
            output.append({
                "act_id": act_id,
                "act": current["jurabk"],
                "jurabk": current["jurabk"],
                "date": current["observed_at"],
                "observed_at": current["observed_at"],
                "previous_observed_at": previous["observed_at"],
                "state_sha256": current["state_sha256"],
                "previous_state_sha256": previous["state_sha256"],
                "old_builddate": previous["builddate"],
                "new_builddate": current["builddate"],
                "changes": changes,
                "full_state_pair": True,
                "date_basis": DATE_BASIS,
                "verification": VERIFICATION,
                "effective_at": None,
                "source_url": current["source_url"],
            })
            previous = current
    output.sort(key=lambda row: (
        row["observed_at"], row["act_id"], row["state_sha256"]))
    return output
