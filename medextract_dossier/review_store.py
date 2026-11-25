"""Freeze a reviewed synthetic casebook for a later, independent CLI export."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .bundle_format import check_member_path
from .comparison import make_comparison, review_identifier
from .evidence import load_casebook_inputs, parse_json, validate_casebook


_INPUT_LIMIT = 1024 * 1024
_RECORD_LIMIT = 2 * _INPUT_LIMIT


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _path(directory: Path, review_id: str) -> Path:
    if not isinstance(review_id, str) or not re.fullmatch(r"[0-9a-f]{64}", review_id):
        raise ValueError("Expected a 64-character lowercase review ID")
    if directory.is_symlink():
        raise ValueError("The review store must be a regular directory")
    return directory / f"{review_id}.json"


def _read(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > _RECORD_LIMIT:
        raise ValueError("Expected a regular review record within the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags | getattr(os, "O_NONBLOCK", 0)), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(before, opened) or before.st_size != opened.st_size:
            raise ValueError("Review record changed while opening it")
        raw = stream.read(opened.st_size + 1)
    if len(raw) != opened.st_size:
        raise ValueError("Review record changed while reading it")
    return raw


def _check_sources(book: dict, sources: dict[str, bytes]) -> None:
    if sources.keys() != {row["file"] for row in book["sources"]}:
        raise ValueError("Stored source inventory differs from the casebook")
    if len({name.casefold() for name in sources}) != len(sources):
        raise ValueError("Source names must be distinct on case-insensitive filesystems")
    for name in sources:
        check_member_path(name)
    if len(_encode(book)) + sum(map(len, sources.values())) > _INPUT_LIMIT:
        raise ValueError("Captured review inputs exceed the size limit")


def _record_bytes(book: dict, sources: dict[str, bytes], omitted: list[str]) -> bytes:
    return _encode({"schema_version": 1, "casebook": book, "omitted_passages": omitted,
                    "sources": {name: base64.b64encode(data).decode("ascii") for name, data in sources.items()}})


def freeze_review(example: Path, directory: Path, omitted: list[str]) -> tuple[str, dict]:
    """Atomically publish a validated review's inputs and return the result shown to the operator."""
    book, sources = load_casebook_inputs(example, max_bytes=_INPUT_LIMIT)
    _check_sources(book, sources)
    comparison = make_comparison(book, omitted)
    review_id = review_identifier(book, comparison)
    raw = _record_bytes(book, sources, comparison["omitted_passages"])
    if len(raw) > _RECORD_LIMIT:
        raise ValueError("Captured review record exceeds the size limit")
    target = _path(directory, review_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".review-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _read(target) != raw:
                raise ValueError("An inconsistent record already exists for this review ID") from None
    finally:
        os.unlink(temporary)
    return review_id, comparison


def load_review(directory: Path, review_id: str) -> tuple[dict, dict[str, bytes], list[str]]:
    """Verify frozen inputs against their reference before supplying them to the signer."""
    raw = _read(_path(directory, review_id))
    try:
        record = parse_json(raw)
    except RecursionError as exc:
        raise ValueError("Stored review nesting exceeds the supported format") from exc
    if (not isinstance(record, dict) or set(record) != {"schema_version", "casebook", "sources", "omitted_passages"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1):
        raise ValueError("Unsupported stored review format")
    encoded = record["sources"]
    if not isinstance(encoded, dict) or not all(isinstance(data, str) for data in encoded.values()):
        raise ValueError("Invalid stored source bytes")
    sources = {}
    for name, data in encoded.items():
        check_member_path(name)
        sources[name] = base64.b64decode(data, validate=True)
    try:
        book = validate_casebook(record["casebook"], sources.__getitem__)
    except KeyError as exc:
        raise ValueError(f"Missing source in stored review: {exc}") from exc
    _check_sources(book, sources)
    omitted = record["omitted_passages"]
    if not isinstance(omitted, list) or not all(isinstance(item, str) for item in omitted):
        raise ValueError("Invalid stored passage omissions")
    comparison = make_comparison(book, omitted)
    if raw != _record_bytes(book, sources, omitted):
        raise ValueError("Stored review is outside its canonical encoding")
    if (omitted != comparison["omitted_passages"]
            or review_identifier(book, comparison) != review_id):
        raise ValueError("Stored review does not match the requested review ID")
    return book, sources, omitted
