"""Search published research, read paper passages, or replay a reviewed casebook."""
from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from .comparison import make_comparison
from .presentation import render_text
from .evidence import example_directory, load_casebook


if TYPE_CHECKING:
    from .discovery import DiscoverySnapshot


def _write_line(text: str) -> None:
    """Emit UTF-8 CLI output even when a pipe inherits a legacy terminal locale."""
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        print(text)
    else:
        stream.write((text + "\n").encode("utf-8", errors="backslashreplace"))


def _handoff(command: str, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m medextract_dossier {command}")
    if command == "keygen":
        parser.description = "Create an Ed25519 identity or recover its matching public key."
        parser.add_argument("--private-key", type=Path, required=True)
        parser.add_argument("--public-key", type=Path, required=True)
    elif command == "export":
        parser.description = "Export the reviewed casebook and sign the manifest."
        parser.add_argument("example", nargs="?", type=Path, help="current example directory for an immediate export")
        parser.add_argument("--review-store", type=Path, help="directory containing a previously frozen review")
        parser.add_argument("--review-id", help="identifier returned by build --review-store")
        parser.add_argument("--out", type=Path, required=True, help="destination bundle directory")
        parser.add_argument("--private-key", type=Path, required=True)
        parser.add_argument("--without-passage", action="append", default=[], metavar="ID")
    else:
        parser.description = "Verify the received files with a separately supplied public key and replay the decisions."
        parser.add_argument("bundle", type=Path)
        parser.add_argument("--public-key", type=Path, required=True)
        parser.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    if command == "export":
        stored = args.review_store is not None or args.review_id is not None
        if stored and (args.example is not None or args.review_store is None or args.review_id is None):
            parser.error("choose an example directory or both --review-store and --review-id")
        if stored and args.without_passage:
            parser.error("a stored review already fixes its passage omissions")
        if not stored and args.example is None:
            parser.error("supply an example directory or --review-store and --review-id")
    try:
        if command == "keygen":
            from .signing import generate_keypair
            fingerprint = generate_keypair(args.private_key, args.public_key)
            _write_line(f"Public key: {args.public_key}\nFingerprint: {fingerprint}")
        elif command == "export":
            from .bundle_export import export_bundle, export_review
            if args.review_store is None:
                destination = export_bundle(args.example, args.out, args.private_key, args.without_passage)
            else:
                from .review_store import load_review
                book, sources, omitted = load_review(args.review_store, args.review_id)
                destination = export_review(book, sources, args.out, args.private_key, omitted)
            _write_line(f"Signed review bundle: {destination}")
        else:
            from .bundle_verify import verify_bundle
            result = verify_bundle(args.bundle, args.public_key)
            if args.json:
                _write_line(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
            else:
                _write_line("Verified" if result["verified"] else "Verification failed")
                for field in ("signature", "integrity", "replay"):
                    _write_line(f"{field}: {result[field]}")
                if result.get("supplied_key_fingerprint"):
                    _write_line(f"Supplied key: {result['supplied_key_fingerprint']}")
                if result.get("claimed_keyid"):
                    _write_line(f"Claimed key: {result['claimed_keyid']}")
                for name, status in result["members"].items():
                    _write_line(f"  {status}  {name}")
                if result.get("error"):
                    _write_line(result["error"])
            if result.get("error_kind") == "configuration":
                return 2
            return 0 if result["verified"] else 1
    except ModuleNotFoundError as exc:
        if exc.name != "cryptography":
            raise
        print("Signing and verification require the handoff extra. From the checkout, "
              "run python -m pip install '.[handoff]', or install your wheel with [handoff].",
              file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"{command} failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _literature(command: str, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"medextract-dossier {command}")
    if command == "research":
        parser.description = "Search Europe PMC and PubMed for published literature."
        parser.add_argument("question", help="population-level research question")
        parser.add_argument("--terms", help="search expression sent to the providers (defaults to the question)")
        parser.add_argument("--limit", type=int, default=5, help="maximum records returned (1–20)")
    else:
        parser.description = "Locate relevant passages in openly licensed paper text from Europe PMC."
        parser.add_argument("pmcid", help="PubMed Central identifier, for example PMC followed by digits")
        parser.add_argument("--terms", required=True, help="words to locate within the paper")
        parser.add_argument("--limit", type=int, default=3, help="maximum passages (1–5)")
    args = parser.parse_args(arguments)
    try:
        if command == "research":
            from .literature_search import search_literature
            result = search_literature(args.question, args.terms, args.limit)
        else:
            from .paper_text import read_paper
            result = read_paper(args.pmcid, args.terms, args.limit)
    except ValueError as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(f"{command}: the literature provider could not be reached.", file=sys.stderr)
        return 1
    _write_line(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 1 if result["status"] == "unavailable" else 0


def _discover(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="medextract-dossier discover",
                                     description="Export a bounded screening set from PubMed and Europe PMC.")
    parser.add_argument("--question", required=True, help="public research question retained in the local manifest")
    parser.add_argument("--concept", action="append", nargs="+", required=True, metavar="PHRASE",
                        help="public synonyms for one concept sent to the indexes; supply 2–6 groups")
    parser.add_argument("--depth", type=int, default=100, help="results per provider and branch (1–100)")
    parser.add_argument("--include-title-search", action="store_true",
                        help="add a search requiring the first concept in the title")
    parser.add_argument("--until-year", type=int, help="inclusive publication year ceiling")
    parser.add_argument("--profile", choices=("pmid_indexed", "all_indexed"), default="pmid_indexed",
                        help="PMID-indexed records (default), or include all Europe PMC sources")
    parser.add_argument("--out", required=True, type=Path, help="new directory for candidates.tsv and search.json")
    args = parser.parse_args(arguments)
    from .discovery import DiscoveryRequest
    from .discovery_providers import collect_discovery

    try:
        request = DiscoveryRequest(args.question, args.concept, args.depth, args.include_title_search,
                                   args.until_year, args.profile)
    except ValueError as exc:
        print(f"Invalid discovery request: {exc}", file=sys.stderr)
        return 2
    return _export_discovery(args.out, lambda: collect_discovery(request))


_DISCOVERY_FILES = ("search.json", "candidates.tsv")
_MAX_CACHE_ENTRIES = 1024
_MAX_DONOR_EXPORTS = 256


@contextmanager
def _discovery_directory(path: Path | str, *, dir_fd: int | None = None):
    from .discovery_recovery import RecoveryError

    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise RecoveryError("Local donor directory scans require macOS or Linux.")
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        raise RecoveryError("Cannot open the discovery directory. Check the supplied folder and permissions.") from None
    try:
        yield directory
    finally:
        os.close(directory)


def _read_discovery_export(source: Path = Path("."), *, dir_fd: int | None = None) -> tuple[str, str]:
    from .discovery import MAX_SNAPSHOT_BYTES
    from .discovery_recovery import RecoveryError

    parts = []
    remaining = MAX_SNAPSHOT_BYTES
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    for name in _DISCOVERY_FILES:
        try:
            with os.fdopen(os.open(source / name, flags, dir_fd=dir_fd), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > remaining:
                    raise RecoveryError("Supply regular search.json and candidates.tsv files totalling at most 2 MiB.")
                data = stream.read(info.st_size + 1)
                if len(data) != info.st_size:
                    raise RecoveryError("The source changed during reading. Use a complete saved discovery export.")
            parts.append(data.decode("utf-8"))
        except (OSError, UnicodeError):
            raise RecoveryError("Cannot read the source. Supply a complete UTF-8 discovery export.") from None
        remaining -= len(data)
    return parts[0], parts[1]


def _has_discovery_member(directory: int) -> bool:
    for name in _DISCOVERY_FILES:
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return True
    return False


def _cached_discovery_exports(cache: Path, destination: Path) -> Iterator[tuple[str, str]]:
    from .discovery_recovery import RecoveryError

    exports = 0
    with _discovery_directory(cache) as directory, os.scandir(directory) as entries:
        if os.path.samestat(os.fstat(directory), destination.stat(follow_symlinks=False)):
            raise RecoveryError("Supply an existing cache directory separate from the new output directory.")
        for count, entry in enumerate(entries, 1):
            if count > _MAX_CACHE_ENTRIES:
                raise RecoveryError(f"The cache exceeds the limit of {_MAX_CACHE_ENTRIES} immediate entries.")
            if entry.is_symlink():
                raise RecoveryError("The discovery cache cannot contain symbolic links among its immediate entries.")
            if not entry.is_dir(follow_symlinks=False):
                continue
            with _discovery_directory(entry.name, dir_fd=directory) as child:
                if not _has_discovery_member(child):
                    continue
                exports += 1
                if exports > _MAX_DONOR_EXPORTS:
                    raise RecoveryError(f"The cache exceeds the limit of {_MAX_DONOR_EXPORTS} discovery exports.")
                yield _read_discovery_export(dir_fd=child)


def _recover_discovery(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="medextract-dossier recover-discovery",
                                     description="Recover timed-out searches from matching local exports.")
    parser.add_argument("parent", type=Path, help="saved discovery directory with timed-out searches")
    parser.add_argument("--cache-dir", required=True, type=Path, metavar="DIR",
                        help=f"scan immediate children for saved exports (up to {_MAX_DONOR_EXPORTS} exports "
                             f"and {_MAX_CACHE_ENTRIES} directory entries)")
    parser.add_argument("--max-gap-seconds", required=True, type=int, metavar="N",
                        help="maximum allowed capture gap in seconds (0 or greater)")
    parser.add_argument("--lookup-at", metavar="WHEN",
                        help="measure donor age from now or an explicit timezone-aware ISO timestamp (offline only "
                             "for explicit timestamps). The default measures the gap from the parent capture.")
    parser.add_argument("--fetch-missing", action="store_true",
                        help="contact the providers once per unresolved timed-out search (default: offline)")
    parser.add_argument("--out", required=True, type=Path, help="new directory for the linked discovery export")
    args = parser.parse_args(arguments)
    from .discovery_recovery import auto_recover_discovery

    def acquire() -> DiscoverySnapshot:
        with _discovery_directory(args.parent) as directory:
            parent = _read_discovery_export(dir_fd=directory)
        return auto_recover_discovery(*parent, _cached_discovery_exports(args.cache_dir, args.out),
                                      max_gap_seconds=args.max_gap_seconds, lookup_at=args.lookup_at,
                                      fetch_missing=args.fetch_missing)

    return _export_discovery(args.out, acquire)


def _retry_discovery(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="medextract-dossier retry-discovery",
                                     description="Retry timed-out searches and save a linked discovery export.")
    parser.add_argument("source", type=Path, help="saved discovery directory containing search.json and candidates.tsv")
    parser.add_argument("--out", required=True, type=Path, help="new directory for the recovery export")
    args = parser.parse_args(arguments)
    from .discovery_recovery import recover_discovery

    return _export_discovery(args.out, lambda: recover_discovery(*_read_discovery_export(args.source)))


def _reuse_discovery(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="medextract-dossier reuse-discovery",
                                     description="Fill timed-out searches offline using a compatible saved export.")
    parser.add_argument("parent", type=Path, help="saved discovery directory with timed-out searches")
    parser.add_argument("--from", dest="donor", required=True, type=Path, metavar="DONOR",
                        help="saved discovery directory supplying complete search results")
    parser.add_argument("--max-gap-seconds", required=True, type=int, metavar="INT",
                        help="maximum allowed time difference between matching captures, in seconds (0 or greater)")
    parser.add_argument("--out", required=True, type=Path, help="new directory for the linked discovery export")
    args = parser.parse_args(arguments)
    from .discovery_recovery import reuse_discovery

    def acquire() -> DiscoverySnapshot:
        return reuse_discovery(*_read_discovery_export(args.parent), *_read_discovery_export(args.donor),
                               max_gap_seconds=args.max_gap_seconds)

    return _export_discovery(args.out, acquire)


def _export_discovery(out: Path, acquire: Callable[[], DiscoverySnapshot]) -> int:
    from .discovery_recovery import RecoveryError

    try:
        out.mkdir(mode=0o700)
    except FileExistsError:
        print("Discovery needs a new output directory. Choose an unused name.", file=sys.stderr)
        return 2
    except OSError:
        print("Cannot create the discovery directory. Check its parent and permissions.", file=sys.stderr)
        return 2

    # Only remove members this invocation actually created. Other files may have
    # been placed in the directory while the providers were being contacted.
    created = []
    try:
        snapshot = acquire()
        summary = snapshot.summary()
        summary.pop("candidates_uri")
        summary.pop("manifest_uri")
        summary.update(candidates_file="candidates.tsv", manifest_file="search.json")
        output = json.dumps(summary, ensure_ascii=False, allow_nan=False)
        for name, content in (("candidates.tsv", snapshot.tsv), (".search.json.part", snapshot.manifest)):
            member = out / name
            with member.open("xb") as stream:
                created.append(member)
                stream.write(content.encode("utf-8"))
        # The directory was exclusively reserved. Expose the completion marker
        # only after both streams close successfully.
        os.link(created[-1], out / "search.json")
    except (OSError, ValueError, RuntimeError) as exc:
        for path in reversed(created):
            with suppress(OSError):
                path.unlink()
        with suppress(OSError):
            out.rmdir()
        message = str(exc) if isinstance(exc, RecoveryError) else "Check the destination and retry with a fresh directory."
        print(f"Discovery export failed. {message}", file=sys.stderr)
        return 2
    # The two public files are now complete. A leftover staging name after a
    # cleanup error does not invalidate the published manifest.
    with suppress(OSError):
        created[-1].unlink()
    try:
        _write_line(output)
    except OSError:
        print("Discovery files were saved, but stdout could not receive the preview.", file=sys.stderr)
        return 2
    return 0 if summary["status"] == "ok" else 3


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["recover-discovery"]:
        return _recover_discovery(argv[1:])
    if argv[:1] == ["reuse-discovery"]:
        return _reuse_discovery(argv[1:])
    if argv[:1] == ["retry-discovery"]:
        return _retry_discovery(argv[1:])
    if argv[:1] == ["discover"]:
        return _discover(argv[1:])
    if argv[:1] in (["research"], ["read-paper"]):
        return _literature(argv[0], argv[1:])
    if argv and argv[0] in ("keygen", "export", "verify"):
        return _handoff(argv[0], argv[1:])
    if argv[:1] == ["mcp"]:
        from .mcp_config import main as serve
        return serve(argv[1:])
    explicit_build = argv[:1] == ["build"]
    if explicit_build:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Commands: discover, recover-discovery, retry-discovery, reuse-discovery, research, read-paper, build, "
        "keygen, export, verify, mcp. "
        "Run a command with --help for its options."))
    parser.add_argument("example", type=Path, nargs="?" if explicit_build else None,
                        default=example_directory() if explicit_build else None,
                        help="directory containing casebook.json and its sources (build defaults to the published CRP/SAA trial)")
    parser.add_argument("--without-passage", action="append", default=[], metavar="ID",
                        help="omit one passage from both questions; repeat to omit more")
    parser.add_argument("--review-store", type=Path, help="freeze this review and return its ID for exact later export")
    parser.add_argument("--json", action="store_true", help="print decisions with the full passage ledgers")
    args = parser.parse_args(argv)
    try:
        if args.review_store is None:
            book = load_casebook(args.example)
            comparison = make_comparison(book, args.without_passage)
        else:
            from .review_store import freeze_review
            review_id, comparison = freeze_review(args.example, args.review_store, args.without_passage)
    except (ValueError, OSError) as exc:
        print(f"Cannot review example: {exc}", file=sys.stderr)
        return 2
    if args.json:
        result = comparison if args.review_store is None else {"review_id": review_id, "comparison": comparison}
        _write_line(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        if args.review_store is not None:
            _write_line(f"Review ID: {review_id}")
        _write_line(render_text(comparison))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
