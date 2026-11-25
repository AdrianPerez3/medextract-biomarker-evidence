"""Authenticate a received manifest, check its exact file inventory, then replay it."""

import hashlib
import os
from pathlib import Path
import re
import stat

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .bundle_format import (
    BundleLimitError, ENVELOPE_NAME, REQUIRED_MEMBERS, check_member_path, parse_json, validate_content,
)
from .signing import SignatureError, load_public_key, verify_signature


def _read_regular(path: Path, before: os.stat_result | None = None) -> bytes:
    if before is None:
        before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{path.name!r} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise ValueError(f"{path.name!r} changed while opening it")
        if opened.st_size != before.st_size:
            raise ValueError(f"{path.name!r} changed while opening it")
        return source.read(opened.st_size + 1)


def _manifest_entries(payload: bytes) -> dict:
    manifest = parse_json(payload)
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "members"}:
        raise ValueError("manifest must contain schema_version and members")
    version = manifest["schema_version"]
    if type(version) is not int or version != 1:
        raise ValueError("unsupported manifest schema_version (expected integer 1)")
    if not isinstance(manifest["members"], list):
        raise ValueError("manifest members must be an array")

    entries = {}
    portable_names = set()
    for i, item in enumerate(manifest["members"]):
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError(f"manifest member {i} must contain path, bytes and sha256")
        name = item["path"]
        if not isinstance(name, str):
            raise ValueError(f"manifest member {i} path must be a string")
        check_member_path(name)
        if name.casefold() in portable_names:
            raise ValueError(f"duplicate manifest path {name!r} (case insensitive)")
        portable_names.add(name.casefold())
        if type(item["bytes"]) is not int or item["bytes"] < 0:
            raise ValueError(f"manifest {name!r} bytes must be a nonnegative integer")
        if not isinstance(item["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", item["sha256"]):
            raise ValueError(f"manifest {name!r} sha256 must be 64 lowercase hex digits")
        entries[name] = item
    if list(entries) != sorted(entries):
        raise ValueError("manifest members must be sorted by path")
    missing = REQUIRED_MEMBERS.difference(entries)
    if missing:
        raise ValueError(f"manifest is missing required members: {', '.join(sorted(missing))}")
    return entries


def _check_inventory(directory: Path, entries: dict, max_entries: int | None = None) -> tuple[dict, dict, list[str]]:
    statuses = dict.fromkeys(entries, "missing")
    contents = {}
    errors = []
    has_sources = any(name.startswith("sources/") for name in entries)

    count = 0

    def check_directory(parent: Path, prefix: str):
        nonlocal count
        try:
            paths = []
            with os.scandir(parent) as directory_entries:
                for item in directory_entries:
                    count += 1
                    if max_entries is not None and count > max_entries:
                        raise BundleLimitError("Bundle exceeds the configured entry limit")
                    paths.append(parent / item.name)
        except OSError as exc:
            errors.append(f"cannot list {prefix or 'bundle directory'}: {exc.strerror}")
            return
        for path in paths:
            name = prefix + path.name
            if name == ENVELOPE_NAME:
                continue
            try:
                if name == "sources" and has_sources:
                    if stat.S_ISDIR(path.lstat().st_mode):
                        check_directory(path, "sources/")
                    else:
                        statuses[name] = "not_directory"
                    continue
                if name not in entries:
                    statuses[name] = "unexpected"
                    continue
                before = path.lstat()
                entry = entries[name]
                if not stat.S_ISREG(before.st_mode):
                    statuses[name] = "not_regular"
                elif before.st_size != entry["bytes"]:
                    statuses[name] = "changed"
                else:
                    data = _read_regular(path, before)
                    if (len(data) == entry["bytes"]
                            and hashlib.sha256(data).hexdigest() == entry["sha256"]):
                        contents[name] = data
                        statuses[name] = "match"
                    else:
                        statuses[name] = "changed"
            except BundleLimitError:
                raise
            except (OSError, ValueError) as exc:
                statuses[name] = "unreadable"
                errors.append(f"{name!r}: {exc.strerror if isinstance(exc, OSError) else exc}")

    check_directory(directory, "")
    return dict(sorted(statuses.items())), contents, errors


def verify_bundle(directory: Path, public_key: Path, *, max_bytes: int | None = None,
                  max_entries: int | None = None, parsed_public_key: Ed25519PublicKey | None = None) -> dict:
    """Verify using an independently supplied public key and return stage results.

    'claimed_keyid' is the envelope's claim. Only a valid signature sets
    'signer_fingerprint'. Unknown directories are reported as unexpected at
    their boundary. Nonregular or unreadable members cannot pass integrity.
    Replay consumes the same binary snapshots whose lengths and hashes matched.
    """
    result = {
        "verified": False, "signature": "not_checked", "integrity": "not_checked",
        "replay": "not_checked", "members": {}, "claimed_keyid": None, "manifest_sha256": None,
        "supplied_key_fingerprint": None, "signer_fingerprint": None, "review_id": None, "error": None,
    }
    directory, public_key = Path(directory), Path(public_key)
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("bundle path must be a directory")
        if public_key.resolve().is_relative_to(directory.resolve()):
            raise ValueError("supply a trusted public key from outside the bundle directory")
        key = parsed_public_key if parsed_public_key is not None else load_public_key(public_key)
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(error=exc.strerror if isinstance(exc, OSError) else str(exc), error_kind="configuration")
        return result
    try:
        envelope_path = directory / ENVELOPE_NAME
        before = envelope_path.lstat()
        if max_bytes is not None and before.st_size > max_bytes:
            raise BundleLimitError("Bundle exceeds the configured byte limit")
        raw_envelope = _read_regular(envelope_path, before)
        if max_bytes is not None and len(raw_envelope) > max_bytes:
            raise BundleLimitError("Bundle exceeds the configured byte limit")
    except BundleLimitError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        result["error"] = f"Cannot open bundle or manifest: {exc.strerror}" if isinstance(exc, OSError) else str(exc)
        return result

    try:
        envelope = parse_json(raw_envelope)
        payload, fingerprint = verify_signature(envelope, key)
    except SignatureError as exc:
        result.update(signature="invalid", error=str(exc), claimed_keyid=exc.claimed_keyid,
                      supplied_key_fingerprint=exc.supplied_keyid)
        return result
    except (ValueError, OSError, RecursionError) as exc:
        result.update(signature="invalid", error=f"cannot verify {ENVELOPE_NAME}: {exc}")
        return result
    result.update(signature="valid", signer_fingerprint=fingerprint,
                  manifest_sha256=hashlib.sha256(payload).hexdigest(),
                  supplied_key_fingerprint=fingerprint, claimed_keyid=envelope["signatures"][0]["keyid"])

    try:
        entries = _manifest_entries(payload)
    except (ValueError, RecursionError) as exc:
        result["error"] = f"invalid signed manifest: {exc}"
        return result

    if max_bytes is not None and sum(e["bytes"] for e in entries.values()) + len(raw_envelope) > max_bytes:
        raise BundleLimitError("Bundle exceeds the configured byte limit")
    if max_entries is not None and len(entries) + 1 > max_entries:
        raise BundleLimitError("Bundle exceeds the configured entry limit")
    statuses, contents, errors = _check_inventory(directory, entries, max_entries)
    result["members"] = statuses
    discrepancies = [f"{name!r} {status}" for name, status in statuses.items() if status != "match"]
    if discrepancies or errors:
        result.update(integrity="mismatch", error=", ".join(discrepancies + errors))
        return result
    result["integrity"] = "match"

    try:
        review_id = validate_content(contents)
    except (ValueError, RecursionError) as exc:
        result.update(replay="mismatch", error=f"cannot replay bundle: {exc}")
        return result
    result.update(replay="match", verified=True, review_id=review_id)
    return result
