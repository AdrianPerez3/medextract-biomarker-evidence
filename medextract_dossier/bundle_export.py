"""Export one complete review as a signed, movable directory."""

import hashlib
from html import escape
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .bundle_format import BundleLimitError, ENVELOPE_NAME, check_member_path, json_bytes, validate_content
from .comparison import make_comparison
from .evidence import EXPORT_LICENSES, load_casebook_inputs
from .presentation import describe_estimate, describe_reason, review_title
from .signing import sign_manifest


def _scope(scope: dict) -> str:
    rows = (f"<dt>{escape(field.replace('_', ' ').capitalize())}</dt><dd>{escape(str(value))}</dd>"
            for field, value in sorted(scope.items()))
    return "<details><summary>Scope</summary><dl>" + "".join(rows) + "</dl></details>"


def _reason(code: str, origin: str) -> str:
    return f"{escape(describe_reason(code, origin))} <code>{escape(code)}</code>"


def render_report(comparison: dict, *, sources: list[dict] | None = None) -> bytes:
    """Render every reviewed passage, including the evidence behind each exclusion."""
    origin = comparison["evidence_origin"]
    title = review_title(origin)
    parts = ['''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>''' + title + '''</title>
<style>
body { max-width: 54rem; margin: 2rem auto; padding: 0 1.2rem; font: 1rem/1.5 system-ui, sans-serif; color: #182923; }
h1,h2,h3,h4 { line-height: 1.25; }
section { margin: 2.5rem 0; }
article { border-top: 1px solid #c6d1ca; padding: .5rem 0 1rem; }
blockquote { margin: .7rem 0; padding: .6rem 1rem; border-left: 3px solid #6a8876; white-space: pre-wrap; }
a { color: #235b48; }
code,dd,blockquote { overflow-wrap: anywhere; }
dt { font-weight: 600; }
dd { margin: 0 0 .4rem; }
</style></head><body><main><h1>''' + title + '''</h1>
<nav aria-label="Bundle records"><a href="question.json">Questions</a> ·
<a href="evidence.json">Annotated evidence</a> · <a href="decision.json">Decision records</a></nav>''']
    if comparison["omitted_passages"]:
        omitted = ", ".join(escape(item) for item in comparison["omitted_passages"])
        parts.append(f"<h2>Passages omitted from this comparison</h2><p>{omitted}</p>")

    for decision in comparison["decisions"]:
        families = ", ".join(escape(item) for item in decision["eligible_families"]) or "none"
        explanation = _reason(decision["reason"], origin)
        if decision["status"] == "effect_uncertain" and decision["reason"] == "interval_includes_null":
            explanation = ("The 95% interval includes both benefit and harm for the specified outcome. "
                           "<code>interval_includes_null</code>")
        parts.extend([
            f"<section><h2>{escape(decision['question_text'])}</h2>",
            f"<p>Question <code>{escape(decision['question_id'])}</code></p>",
            _scope(decision["question_scope"]),
            f"<h3>Decision</h3><p><code>{escape(decision['status'])}</code></p>",
            f"<p>{explanation}</p>",
            f"<p>Eligible study families · {families}</p><h3>Passage ledger</h3>",
        ])
        for row in decision["ledger"]:
            filename = row["source_file"]
            check_member_path(filename)
            href = escape(filename)
            status = "Eligible" if row["reason"] == "direct" else "Excluded"
            estimate = "No estimate reported." if row["reason"] == "no_result" else "No estimate captured in this annotation."
            if row["estimate"] is not None:
                estimate = escape(describe_estimate(row["estimate"]))
            parts.extend([
                f"<article><h4>{status} · {escape(row['passage_id'])}</h4>",
                f"<p>{_reason(row['reason'], origin)}</p>",
                f"<blockquote>{escape(row['quote'])}</blockquote>",
                f"<p>{estimate}</p>",
                f"<p>Source <code>{escape(row['source_id'])}</code> · "
                f"Study family <code>{escape(row['family_id'])}</code></p>",
                f'<p><a href="{href}">{escape(filename)}</a> · UTF-8 bytes '
                f"[{row['start']}, {row['end']})</p>",
                f"<p>Source SHA-256 <code>{escape(row['source_sha256'])}</code></p>",
                _scope(row["scope"]),
                "</article>",
            ])
        parts.append("</section>")
    if origin == "published":
        if sources is None:
            records = {row["source_id"]: row for decision in comparison["decisions"] for row in decision["ledger"]}
        else:
            records = {source["id"]: source for source in sources}
        parts.append("<section><h2>Publication sources and rights</h2><dl>")
        for identifier, source in records.items():
            url = escape(source["url"])
            license_text = escape(source["license"])
            if source["license"] in EXPORT_LICENSES:
                license_text = f'<a href="{EXPORT_LICENSES[source["license"]]}">{license_text}</a>'
            parts.extend([
                f"<dt>{escape(identifier)}</dt><dd><cite>{escape(source['citation'])}</cite><br>",
                f'<a href="{url}">{url}</a> · {license_text}</dd>',
            ])
        parts.append("</dl></section>")
    parts.append("</main></body></html>\n")
    return "\n".join(parts).encode("utf-8")


def _require_identical(directory: Path, files: dict[str, bytes]) -> None:
    """Permit a retry only when the entire existing tree matches, including names."""
    def check(folder: Path, prefix: str) -> None:
        if folder.is_symlink() or not folder.is_dir():
            raise FileExistsError(f"conflicting export destination: {folder}")
        paths = {name[len(prefix):].split("/", 1)[0] for name in files if name.startswith(prefix)}
        if {entry.name for entry in folder.iterdir()} != paths:
            raise FileExistsError(f"conflicting contents in export destination: {folder}")
        for name in paths:
            entry = folder / name
            relative = prefix + name
            if relative not in files:
                check(entry, relative + "/")
            elif (entry.is_symlink() or not entry.is_file()
                  or entry.stat().st_size != len(files[relative])
                  or entry.read_bytes() != files[relative]):
                raise FileExistsError(f"conflicting export member: {entry}")

    check(directory, "")


def check_signing_source(example: Path, private_key: Path, source_names) -> None:
    """Check that a signer is separate from the configured research inputs."""
    root = Path(example).resolve()
    key = Path(private_key)
    if (key.parent.resolve() / key.name).is_relative_to(root) or key.resolve().is_relative_to(root):
        raise ValueError("the signing key must be outside the example")
    for name in source_names:
        if (root / name).samefile(key):
            raise ValueError(f"source {name} aliases the signing key")


def export_bundle(example: Path, destination: Path, private_key: Path,
                  omitted: list[str] | None = None) -> Path:
    """Capture the casebook and publish one complete signed review."""
    book, sources = load_casebook_inputs(example)
    check_signing_source(example, private_key, sources)
    return export_review(book, sources, destination, private_key, omitted)


def export_review(book: dict, source_bytes: dict[str, bytes], destination: Path, private_key: Path,
                  omitted: list[str] | None = None, *, max_bytes: int | None = None,
                  max_entries: int | None = None) -> Path:
    """Export the captured and validated source bytes even if the original files have changed."""
    destination = Path(destination)
    out = destination.parent.resolve() / destination.name
    key = Path(private_key)
    for path in (key.parent.resolve() / key.name, key.resolve()):
        if path.is_relative_to(out) or out.is_relative_to(path):
            raise ValueError("the signing key must be outside the export destination")
    if out.is_symlink():
        raise FileExistsError(f"export destination is a symbolic link: {out}")
    if set(source_bytes) != {source["file"] for source in book["sources"]}:
        raise ValueError("source filenames differ from the captured casebook")
    for name in source_bytes:
        check_member_path(name)
        if not name.startswith("sources/"):
            raise ValueError("captured source must be under sources/")
    comparison = make_comparison(book, omitted or [])
    header = {"schema_version": book["schema_version"], "evidence_origin": book["evidence_origin"]}
    files = {
        "question.json": json_bytes({**header, "questions": book["questions"]}),
        "evidence.json": json_bytes({**header, **{k: book[k] for k in ("families", "sources", "passages")}}),
        "decision.json": json_bytes(comparison),
        "report.html": render_report(comparison, sources=book["sources"]),
        **source_bytes,
    }
    validate_content(files)
    members = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in sorted(files.items())]
    payload = json_bytes({"schema_version": 1, "members": members})
    files[ENVELOPE_NAME] = json_bytes(sign_manifest(payload, key, source_bytes=source_bytes.values()))
    # V1 permits one source directory; the verifier counts it as an entry.
    if max_entries is not None and len(files) + bool(source_bytes) > max_entries:
        raise BundleLimitError("Bundle exceeds the configured entry limit")
    if max_bytes is not None and sum(map(len, files.values())) > max_bytes:
        raise BundleLimitError("Bundle exceeds the configured byte limit")

    if os.path.lexists(out):
        _require_identical(out, files)
        return destination
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".medextract-", dir=out.parent) as temp:
        stage = Path(temp) / "bundle"
        stage.mkdir()
        for name, data in files.items():
            member = stage / name
            member.parent.mkdir(exist_ok=True)
            member.write_bytes(data)
        try:
            stage.rename(out)
        except OSError:
            # A concurrent publisher may have completed since our existence check.
            if not os.path.lexists(out):
                raise
            _require_identical(out, files)
    return destination
