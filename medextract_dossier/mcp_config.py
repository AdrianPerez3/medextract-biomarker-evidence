"""Operator configuration and entry point for the local stdio server."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


from .evidence import example_directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="medextract-dossier mcp",
                                     description="Run the MedExtract Biomarker Evidence MCP server over stdio.")
    examples = parser.add_mutually_exclusive_group()
    examples.add_argument("--casebook", "--example", dest="example", type=Path, help="operator-selected annotated casebook directory")
    examples.add_argument("--no-example", action="store_true", help="start a recipient without a casebook")
    parser.add_argument("--literature", action="store_true",
                        help="enable online searches of public literature and openly licensed paper text")
    parser.add_argument("--discovery", action="store_true",
                        help="enable public concept searches with frozen candidate pages and exports")
    parser.add_argument("--output-dir", type=Path, help="parent for signed exports")
    parser.add_argument("--private-key", type=Path, help="existing producer signing key")
    parser.add_argument("--public-key", type=Path, help="independently obtained public verification key")
    parser.add_argument("--bundle", metavar="NAME=DIR", action="append", default=[],
                        help="make one selected bundle available for verification; repeat for more")
    args = parser.parse_args(argv)
    bundles = {}
    for item in args.bundle:
        name, sep, directory = item.partition("=")
        if not sep or not name or not directory:
            parser.error("--bundle expects NAME=DIR")
        if name in bundles:
            parser.error("duplicate --bundle name")
        bundles[name] = Path(directory)
    try:
        from .mcp_server import run_server
        from .service import DossierService
    except ModuleNotFoundError as exc:
        if exc.name not in {"mcp", "pydantic", "cryptography"}:
            raise
        print("The stdio server requires the mcp extra. From the checkout, "
              "run python -m pip install '.[mcp]', or install your wheel with [mcp].",
              file=sys.stderr)
        return 2
    try:
        casebook = args.example
        if casebook is None and not (args.no_example or args.literature or args.discovery):
            casebook = example_directory()
        service = DossierService(example=casebook,
                                 output_dir=args.output_dir, private_key=args.private_key,
                                 public_key=args.public_key, bundles=bundles)
        run_server(service, literature=args.literature, discovery=args.discovery)
    except (OSError, ValueError) as exc:
        print(f"Cannot start MCP server: {exc}", file=sys.stderr)
        return 2
    return 0
