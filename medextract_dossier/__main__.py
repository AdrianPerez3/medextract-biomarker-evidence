"""Run both research questions against a validated example and display the evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .comparison import make_comparison
from .presentation import render_text
from .evidence import example_directory, load_casebook


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
        parser.description = "Export the synthetic review and sign the manifest."
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("keygen", "export", "verify"):
        return _handoff(argv[0], argv[1:])
    if argv[:1] == ["mcp"]:
        from .mcp_config import main as serve
        return serve(argv[1:])
    explicit_build = argv[:1] == ["build"]
    if explicit_build:
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Commands: build, keygen, export, verify, mcp. Run a command with --help for its options."))
    parser.add_argument("example", type=Path, nargs="?" if explicit_build else None,
                        default=example_directory() if explicit_build else None,
                        help="directory containing casebook.json and its sources (build defaults to the MX-1 example)")
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
