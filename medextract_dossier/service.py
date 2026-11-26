"""A process-local handle to reviewed casebooks and operator-selected signed bundles."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
from typing import TYPE_CHECKING, Generic, TypeVar

from .bundle_format import BundleLimitError, BundlePathError, json_bytes
from .comparison import make_comparison, review_identifier
from .evidence import load_casebook_inputs, parse_json

if TYPE_CHECKING:
    from .discovery import DiscoveryRequest, DiscoverySnapshot

_ALIAS = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_DIGEST = r"[0-9a-f]{64}"
BUNDLE_ID_PATTERN = rf"^(?:{_ALIAS}|review-{_DIGEST})$"

_CACHE_ENTRIES = 16
_MISSING_DISCOVERY = "Snapshot unavailable. Run discovery again or open a saved export."
_REVIEW_INPUT_BYTES = 1024 * 1024
_REVIEW_RECORD_BYTES = 4 * 1024 * 1024
_BUNDLE_BYTES = 8 * 1024 * 1024
_BUNDLE_ENTRIES = 256
_KEY_BYTES = 64 * 1024


class ServiceError(ValueError):
    """An operational error suitable for returning to a client."""


@contextmanager
def _operation(message: str):
    try:
        yield
    except ServiceError:
        raise
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography":
            raise ServiceError("Signing and verification require the handoff dependency.") from None
        raise
    except (OSError, ValueError, RuntimeError):
        # File errors and validation exceptions can contain absolute operator paths or input text.
        raise ServiceError(message) from None


T = TypeVar("T")


class _Cache(Generic[T]):
    def __init__(self):
        self._items: OrderedDict[str, T] = OrderedDict()
        self._lock = Lock()

    def put(self, identifier: str, value: T) -> None:
        with self._lock:
            self._items[identifier] = value
            self._items.move_to_end(identifier)
            if len(self._items) > _CACHE_ENTRIES:
                self._items.popitem(last=False)

    def get(self, identifier: str, missing: str) -> T:
        with self._lock:
            return self._lookup(identifier, missing)

    def get_with_peers(self, identifier: str, missing: str) -> tuple[T, tuple[T, ...]]:
        with self._lock:
            item = self._lookup(identifier, missing)
            return item, tuple(value for key, value in self._items.items() if key != identifier)

    def _lookup(self, identifier: str, missing: str) -> T:
        if not isinstance(identifier, str) or identifier not in self._items:
            raise ServiceError(missing)
        self._items.move_to_end(identifier)
        return self._items[identifier]


@dataclass(frozen=True)
class _Review:
    example_directory: Path
    casebook: bytes
    sources: tuple[tuple[str, bytes], ...]
    omitted: tuple[str, ...]
    comparison: bytes


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_key_path(key: Path, directory: Path) -> None:
    if (key.parent.resolve() / key.name).is_relative_to(directory) or key.resolve().is_relative_to(directory):
        raise ServiceError("The configured key must be stored outside the evidence and bundle directories.")


def _require_key(key: Path) -> None:
    if not key.is_file() or key.stat().st_size > _KEY_BYTES:
        raise ServiceError("The configured key must be an available regular PEM file within the size limit.")


def _configured_path(path: Path | None) -> Path | None:
    return None if path is None else Path(path).absolute()


def _check_omissions(items: list[str]) -> None:
    if not isinstance(items, list):
        raise ServiceError("Omissions must be a list of passage IDs.")
    remaining = _REVIEW_INPUT_BYTES - len(items)
    limit_error = "Requested omissions exceed the input size limit."
    for item in items:
        if not isinstance(item, str):
            raise ServiceError("Omissions must be a list of passage IDs.")
        if len(item) > remaining:
            raise ServiceError(limit_error)
        remaining -= len(item.encode("utf-8"))
        if remaining < 0:
            raise ServiceError(limit_error)


class DossierService:
    """Scope all filesystem access to the paths chosen by the stdio server operator."""

    def __init__(self, *, example: Path | None = None, output_dir: Path | None = None,
                 private_key: Path | None = None, public_key: Path | None = None,
                 bundles: dict[str, Path] | None = None):
        self._example = _configured_path(example)
        self._output_dir = _configured_path(output_dir)
        self._private_key = _configured_path(private_key)
        self._public_key = _configured_path(public_key)
        self._bundles = {name: Path(path).absolute() for name, path in (bundles or {}).items()}
        if any(not isinstance(name, str) or not re.fullmatch(_ALIAS, name) for name in self._bundles):
            raise ServiceError("A registered bundle name must be a short portable identifier.")
        self._reviews: _Cache[_Review] = _Cache()
        self._receipts: _Cache[bytes] = _Cache()
        self._discoveries: _Cache[DiscoverySnapshot] = _Cache()

    def _save_discovery(self, snapshot: DiscoverySnapshot) -> dict:
        summary = snapshot.summary()
        self._discoveries.put(snapshot.search_id, snapshot)
        return summary

    def discover(self, request: DiscoveryRequest) -> dict:
        from .discovery_providers import collect_discovery

        with _operation("Cannot capture the requested discovery snapshot. Check the search bounds and retry."):
            return self._save_discovery(collect_discovery(request))

    def retry_discovery(self, search_id: str) -> dict:
        from .discovery_recovery import RecoveryError, recover_discovery

        parent = self.discovery_snapshot(search_id)
        with _operation("Cannot recover this discovery snapshot. Open the saved export and try again."):
            try:
                child = recover_discovery(parent.manifest, parent.tsv)
            except RecoveryError as exc:
                raise ServiceError(str(exc)) from None
            return self._save_discovery(child)

    def reuse_discovery(self, search_id: str, donor_search_id: str, max_gap_seconds: int) -> dict:
        from .discovery_recovery import RecoveryError, reuse_discovery

        parent = self.discovery_snapshot(search_id)
        donor = self.discovery_snapshot(donor_search_id)
        with _operation("Cannot reuse these discovery snapshots. Check the saved exports."):
            try:
                child = reuse_discovery(parent.manifest, parent.tsv, donor.manifest, donor.tsv,
                                        max_gap_seconds=max_gap_seconds)
            except RecoveryError as exc:
                raise ServiceError(str(exc)) from None
            return self._save_discovery(child)

    def discovery_snapshot(self, search_id: str) -> DiscoverySnapshot:
        return self._discoveries.get(search_id, _MISSING_DISCOVERY)

    def recover_discovery(self, search_id: str, max_gap_seconds: int, fetch_missing: bool = False,
                          lookup_at: str | None = None) -> dict:
        from .discovery_recovery import RecoveryError, auto_recover_discovery

        parent, peers = self._discoveries.get_with_peers(search_id, _MISSING_DISCOVERY)
        with _operation("Cannot recover this discovery snapshot. Check the cached captures and time window."):
            try:
                child = auto_recover_discovery(parent.manifest, parent.tsv,
                                               ((item.manifest, item.tsv) for item in peers),
                                               max_gap_seconds=max_gap_seconds, fetch_missing=fetch_missing,
                                               lookup_at=lookup_at)
            except RecoveryError as exc:
                raise ServiceError(str(exc)) from None
            return self._save_discovery(child)

    def discovery_page(self, search_id: str, offset: int, limit: int, view: str) -> dict:
        snapshot = self.discovery_snapshot(search_id)
        try:
            return snapshot.page(offset, limit, view)
        except ValueError as exc:
            # The pure page validator emits public recovery messages only.
            raise ServiceError(str(exc)) from None

    def build(self, example_id: str = "configured", without_passages: list[str] | None = None) -> dict:
        if example_id not in ("configured", "marker-mx1") or self._example is None:
            raise ServiceError("The requested casebook is unavailable.")
        omitted = [] if without_passages is None else without_passages
        with _operation("Omissions must contain valid UTF-8 passage IDs."):
            _check_omissions(omitted)
        with _operation("Cannot read the configured casebook. Check its source files and size limit."):
            example_directory = self._example.resolve()
            if self._private_key is not None:
                _check_key_path(self._private_key, example_directory)
            book, sources = load_casebook_inputs(example_directory, max_bytes=_REVIEW_INPUT_BYTES)
            if example_id == "marker-mx1" and book["evidence_origin"] != "synthetic":
                raise ServiceError("The requested synthetic example is unavailable.")
            if self._private_key is not None and self._private_key.exists():
                from .bundle_export import check_signing_source

                check_signing_source(example_directory, self._private_key, sources)

        with _operation("Unknown passage ID in the requested omissions."):
            comparison = make_comparison(book, omitted)
        with _operation("Cannot encode the configured review."):
            casebook = json_bytes(book)
            record = json_bytes(comparison)
            if len(record) > _REVIEW_RECORD_BYTES:
                raise ServiceError("The full comparison exceeds the configured review size limit.")
            review_id = review_identifier(book, comparison)
            review = _Review(example_directory, casebook, tuple(sources.items()),
                             tuple(comparison["omitted_passages"]), record)
            self._reviews.put(review_id, review)
        return {
            "schema_version": 1,
            "evidence_origin": comparison["evidence_origin"],
            "review_id": review_id,
            "review_uri": f"dossier://reviews/{review_id}",
            "decisions": [
                {"question_id": item["question_id"], "status": item["status"], "reason": item["reason"],
                 "eligible_study_count": len(item["eligible_families"])}
                for item in comparison["decisions"]
            ],
        }

    def _review(self, review_id: str) -> _Review:
        return self._reviews.get(review_id, "Review unavailable. Build the review again to obtain its current ID.")

    def read_review(self, review_id: str) -> str:
        return self._review(review_id).comparison.decode("utf-8")

    def read_receipt(self, receipt_id: str) -> str:
        return self._receipts.get(receipt_id, "Receipt unavailable. Verify the bundle again.").decode("utf-8")

    def _output(self, bundle_id: str) -> Path:
        if self._output_dir is None:
            raise ServiceError("An output directory must be configured to access exported bundles.")
        if self._output_dir.is_symlink():
            raise ServiceError("The configured output directory cannot be a symbolic link.")
        return self._output_dir / bundle_id

    def export(self, review_id: str) -> dict:
        review = self._review(review_id)
        bundle_id = f"review-{review_id}"
        if self._private_key is None:
            raise ServiceError("Export requires a configured private signing key.")
        with _operation("Cannot export this review. Check the signing key and the configured destination for conflicts."):
            from .bundle_export import export_review

            destination = self._output(bundle_id)
            _require_key(self._private_key)
            _check_key_path(self._private_key, review.example_directory)
            _check_key_path(self._private_key, destination.parent.resolve())
            try:
                export_review(parse_json(review.casebook), dict(review.sources), destination,
                              self._private_key, list(review.omitted),
                              max_bytes=_BUNDLE_BYTES, max_entries=_BUNDLE_ENTRIES)
            except BundleLimitError:
                raise ServiceError("The review exceeds the configured bundle verification limit.") from None
            except BundlePathError:
                raise ServiceError("Export requires portable source filenames. Rename the source and rebuild.") from None
        return {"schema_version": 1, "review_id": review_id, "bundle_id": bundle_id,
                "review_uri": f"dossier://reviews/{review_id}"}

    def verify(self, bundle_id: str) -> dict:
        if not isinstance(bundle_id, str) or not re.fullmatch(BUNDLE_ID_PATTERN, bundle_id):
            raise ServiceError("Invalid bundle ID. Use a registered name or a generated review bundle ID.")
        with _operation("Cannot access the selected bundle or its configured trusted public key."):
            if bundle_id in self._bundles:
                directory = self._bundles[bundle_id]
            elif re.fullmatch(rf"review-{_DIGEST}", bundle_id):
                directory = self._output(bundle_id)
            else:
                raise ServiceError("Unknown bundle ID. Use a registered name or an exported bundle ID.")
            if directory.is_symlink() or not directory.is_dir():
                raise ServiceError("The selected bundle directory is unavailable.")
            if self._public_key is None:
                raise ServiceError("Verification requires a configured trusted public key.")
            _check_key_path(self._public_key, directory.resolve())
            _require_key(self._public_key)
            from .signing import load_public_key

            trusted_key = load_public_key(self._public_key)

        with _operation("Cannot verify the selected bundle. Check the bundle and trusted public key."):
            from .bundle_verify import verify_bundle

            try:
                result = verify_bundle(directory, self._public_key, parsed_public_key=trusted_key,
                                       max_bytes=_BUNDLE_BYTES, max_entries=_BUNDLE_ENTRIES)
            except BundleLimitError:
                raise ServiceError("The selected bundle exceeds the configured verification limit.") from None
            if result.get("error_kind") == "configuration":
                raise ServiceError("The configured bundle or trusted public key is unavailable.")
            if result["verified"] and bundle_id not in self._bundles and bundle_id != f"review-{result['review_id']}":
                raise ServiceError("The authenticated review differs from the requested review bundle ID.")
            # Escape even an invalid Unicode surrogate copied from an unauthenticated envelope.
            receipt = (json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
                       + "\n").encode("ascii")
            receipt_id = _digest(json_bytes({"bundle_id": bundle_id, "receipt_sha256": _digest(receipt)}))
            self._receipts.put(receipt_id, receipt)
        return {
            "schema_version": 1, "bundle_id": bundle_id,
            **{key: result[key] for key in ("verified", "signature", "integrity", "replay")},
            "receipt_id": receipt_id, "receipt_uri": f"dossier://receipts/{receipt_id}",
        }
