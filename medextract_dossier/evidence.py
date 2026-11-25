"""Load a version 1 casebook and verify each passage against its local source."""

import hashlib
import json
import math
import os
import stat
from pathlib import Path
import re

from .questions import EXPECTED_DIRECTION

_SOURCE_PATH = re.compile(r"sources/[^/\\\x00]+\.txt")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(token):
    raise ValueError(f"{token} is not a finite JSON number")


def _fields(value, names, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    required = set(names.split())
    missing = required - value.keys()
    extra = value.keys() - required
    if missing or extra:
        raise ValueError(f"{where} fields: missing {sorted(missing)}, extra {sorted(extra)}")


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a nonblank string")


def _choice(value, choices, where):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{where} must be one of {sorted(choices)}")


def _nonnegative_integer(value, where):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is int and value >= 0:
        return value
    raise ValueError(f"{where} must be a nonnegative integer")


def _indexed(casebook, name):
    records = casebook[name]
    if not isinstance(records, list):
        raise ValueError(f"{name} must be an array")
    index = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{name}[{position}] must be an object")
        identifier = record.get("id")
        _text(identifier, f"{name}[{position}].id")
        if identifier in index:
            raise ValueError(f"{name} has duplicate id {identifier!r}")
        index[identifier] = record
    return index


def _reference(value, index, where):
    _text(value, where)
    if value not in index:
        raise ValueError(f"{where} references unknown id {value!r}")


def _scope(scope, where):
    _fields(scope, "intent population factor comparator outcome event_coding direction months", where)
    _choice(scope["intent"], EXPECTED_DIRECTION, f"{where}.intent")
    for name in ("population", "factor", "comparator", "outcome"):
        _text(scope[name], f"{where}.{name}")
    if scope["factor"] == scope["comparator"]:
        raise ValueError(f"{where} factor and comparator must be distinct")
    if scope["direction"] != EXPECTED_DIRECTION[scope["intent"]]:
        raise ValueError(f"{where}.direction is incompatible with {scope['intent']!r}")
    _choice(scope["event_coding"], ("adverse", "favorable"), f"{where}.event_coding")
    scope["months"] = _nonnegative_integer(scope["months"], f"{where}.months")


def _estimate(estimate, where):
    if estimate is None:
        return
    _fields(estimate, "measure point low high unit", where)
    _choice(estimate["measure"], ("risk_ratio", "mean_difference"), f"{where}.measure")
    _text(estimate["unit"], f"{where}.unit")
    for name in ("point", "low", "high"):
        value = estimate[name]
        if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f"{where}.{name} must be a finite number")
    if not estimate["low"] <= estimate["point"] <= estimate["high"]:
        raise ValueError(f"{where} must have low <= point <= high")
    if estimate["measure"] == "risk_ratio":
        if estimate["low"] < 0:
            raise ValueError(f"{where} risk ratio and limits must be nonnegative")
        if estimate["unit"] != "ratio":
            raise ValueError(f"{where}.unit must be 'ratio' for a risk ratio")


def _read_local(directory, relative, max_bytes=None):
    path = directory / relative
    if not path.resolve().is_relative_to(directory):
        raise ValueError(f"{relative!r} resolves outside the casebook directory")
    try:
        if max_bytes is None:
            return path.read_bytes()
        if not path.is_file():
            raise ValueError(f"cannot read {relative!r}: expected a regular file")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise ValueError("Casebook exceeds the configured byte limit")
            data = stream.read(min(info.st_size, max_bytes) + 1)
            if len(data) > max_bytes:
                raise ValueError("Casebook exceeds the configured byte limit")
            if len(data) != info.st_size:
                raise ValueError(f"input {relative!r} changed while being read")
            return data
    except OSError as exc:
        raise ValueError(f"cannot read {relative!r}: {exc}") from exc


def _source_bytes(source, families, read_source):
    where = f"source {source['id']!r}"
    _fields(source, "id family_id file sha256", where)
    _reference(source["family_id"], families, f"{where}.family_id")
    file = source["file"]
    if not isinstance(file, str) or not _SOURCE_PATH.fullmatch(file):
        raise ValueError(f"{where}.file must have the form sources/<basename>.txt")
    digest = source["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{where}.sha256 must have 64 lowercase hexadecimal digits")
    data = read_source(file)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"{where} SHA-256 mismatch for {file!r}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{where} is not valid UTF-8") from exc
    return data


def _passage(passage, source_bytes):
    where = f"passage {passage['id']!r}"
    _fields(passage, "id source_id start end quote kind review scope estimate", where)
    _reference(passage["source_id"], source_bytes, f"{where}.source_id")
    _choice(passage["kind"], ("result", "objective"), f"{where}.kind")
    _choice(passage["review"], ("reviewed", "unreviewed"), f"{where}.review")
    _scope(passage["scope"], f"{where}.scope")
    _estimate(passage["estimate"], f"{where}.estimate")
    if passage["kind"] == "objective" and passage["estimate"] is not None:
        raise ValueError(f"{where} objective requires a null estimate")

    data = source_bytes[passage["source_id"]]
    start = passage["start"] = _nonnegative_integer(passage["start"], f"{where}.start")
    end = passage["end"] = _nonnegative_integer(passage["end"], f"{where}.end")
    if not start < end <= len(data):
        raise ValueError(f"{where} byte range [{start}, {end}) invalid for {len(data)} bytes")
    try:
        located = data[start:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{where} byte range splits a UTF-8 character") from exc
    _text(passage["quote"], f"{where}.quote")
    if located != passage["quote"]:
        raise ValueError(f"{where} quote differs from the source byte range [{start}, {end})")


def example_directory() -> Path:
    """Locate the synthetic example in a regular installation or a source checkout."""
    package = Path(__file__).resolve().parent
    bundled = package / "data" / "marker-mx1"
    return bundled if bundled.is_dir() else package.parent / "examples" / "marker-mx1"


def load_casebook(directory: Path) -> dict:
    """Validate an example directory and return its casebook."""
    return load_casebook_inputs(directory)[0]


def load_casebook_inputs(directory: Path, *, max_bytes: int | None = None) -> tuple[dict, dict[str, bytes]]:
    """Capture the very same source bytes used for validation and later export."""
    directory = Path(directory).resolve()
    raw = _read_local(directory, "casebook.json", max_bytes)
    remaining = None if max_bytes is None else max_bytes - len(raw)
    try:
        book = parse_json(raw)
    except ValueError as exc:
        raise ValueError(f"invalid casebook.json: {exc}") from exc
    contents = {}

    def read_source(name):
        nonlocal remaining
        if name not in contents:
            contents[name] = _read_local(directory, name, remaining)
            if remaining is not None:
                remaining -= len(contents[name])
        return contents[name]

    return validate_casebook(book, read_source), contents


def json_bytes(obj: object) -> bytes:
    """Stable UTF-8 representation used for review IDs and signed JSON members."""
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       indent=2) + "\n").encode("utf-8")


def parse_json(raw: bytes):
    """Parse a UTF-8 JSON document with unique keys and finite numbers."""
    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        return value
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                      parse_constant=_reject_constant, parse_float=finite_float)


def validate_casebook(casebook: dict, read_source) -> dict:
    """Validate a decoded casebook against the source bytes supplied by its reader."""
    _fields(casebook, "schema_version evidence_origin questions families sources passages", "casebook")
    casebook["schema_version"] = _nonnegative_integer(casebook["schema_version"], "schema_version")
    if casebook["schema_version"] != 1:
        raise ValueError("casebook schema_version must be integer 1")
    if casebook["evidence_origin"] != "synthetic":
        raise ValueError("casebook evidence_origin must be 'synthetic'")

    questions = _indexed(casebook, "questions")
    if not questions:
        raise ValueError("casebook requires at least one question")
    families = _indexed(casebook, "families")
    sources = _indexed(casebook, "sources")
    passages = _indexed(casebook, "passages")
    for identifier, question in questions.items():
        where = f"question {identifier!r}"
        _fields(question, "id text scope", where)
        _choice(identifier, EXPECTED_DIRECTION, f"{where}.id")
        _text(question["text"], f"{where}.text")
        _scope(question["scope"], f"{where}.scope")
        if question["scope"]["intent"] != identifier:
            raise ValueError(f"{where}.id must agree with scope.intent")
    for identifier, family in families.items():
        _fields(family, "id label", f"family {identifier!r}")
        _text(family["label"], f"family {identifier!r}.label")
    source_bytes = {}
    families_by_digest = {}
    for identifier, source in sources.items():
        source_bytes[identifier] = _source_bytes(source, families, read_source)
        family = families_by_digest.setdefault(source["sha256"], source["family_id"])
        if family != source["family_id"]:
            raise ValueError("Identical source bytes cannot describe different study families in v1")
    for passage in passages.values():
        _passage(passage, source_bytes)
    return casebook
