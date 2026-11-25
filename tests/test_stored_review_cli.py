"""A persisted CLI review survives process exit and changes to the original example."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from medextract_dossier.service import DossierService


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "marker-mx1"


def cli(*args, expected=0, stdlib=False):
    command = [sys.executable, "-B", *(["-S"] if stdlib else []), "-m", "medextract_dossier"]
    result = subprocess.run([*command, *map(str, args)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == expected, (result.stdout, result.stderr)
    return result


@pytest.mark.parametrize("omitted", [[], ["cohort-result"]])
def test_export_by_id_uses_only_the_previously_seen_review(tmp_path, omitted):
    original = tmp_path / "original"
    shutil.copytree(EXAMPLE, original)
    store = tmp_path / "reviews"
    flags = ["--without-passage", *omitted] if omitted else []
    decision = json.loads(cli("build", original, *flags, "--json", stdlib=True).stdout)
    frozen = json.loads(cli("build", original, *flags, "--review-store", store, "--json", stdlib=True).stdout)
    assert frozen["comparison"] == decision
    assert DossierService(example=original).build(without_passages=omitted)["review_id"] == frozen["review_id"]
    book = original / "casebook.json"
    changed = json.loads(book.read_bytes())
    next(p for p in changed["passages"] if p["id"] == "trial-result")["review"] = "unreviewed"
    book.write_text(json.dumps(changed), encoding="utf-8")
    current = json.loads(cli("build", original, *flags, "--json", stdlib=True).stdout)
    assert current != frozen["comparison"]
    shutil.rmtree(original)

    key, public = tmp_path / "private.pem", tmp_path / "public.pem"
    cli("keygen", "--private-key", key, "--public-key", public)
    out = tmp_path / "bundle"
    args = ["export", "--review-store", store, "--review-id", frozen["review_id"],
            "--out", out, "--private-key", key]
    cli(*args)
    cli(*args)
    assert json.loads((out / "decision.json").read_bytes()) == decision
    receipt = json.loads(cli("verify", out, "--public-key", public, "--json").stdout)
    assert receipt["verified"] and receipt["review_id"] == frozen["review_id"]


def test_concurrent_cli_builds_publish_one_reusable_record(tmp_path):
    store = tmp_path / "store"
    def run(_):
        return json.loads(cli("build", EXAMPLE, "--review-store", store, "--json", stdlib=True).stdout)
    with ThreadPoolExecutor(max_workers=8) as pool:
        builds = list(pool.map(run, range(8)))
    assert all(out == builds[0] for out in builds)
    saved, = store.iterdir()
    assert saved.name == f"{builds[0]['review_id']}.json"
    prior = saved.stat().st_mtime_ns
    assert run(None) == builds[0]
    assert saved.stat().st_mtime_ns == prior


@pytest.mark.parametrize("change", ["scope", "source", "omissions", "source_name", "encoding", "symlink"])
def test_inconsistent_store_never_exports_or_overwrites_a_record(tmp_path, change):
    store = tmp_path / "store"
    build_args = ["build", EXAMPLE, "--review-store", store, "--json"]
    frozen = json.loads(cli(*build_args, stdlib=True).stdout)
    record, = store.iterdir()
    prior = record.read_bytes()
    data = json.loads(prior)
    if change == "scope":
        data["casebook"]["questions"][0]["scope"]["months"] += 1
    elif change == "source":
        data["sources"]["sources/cohort.txt"] = "YQ=="
    elif change == "omissions":
        data["omitted_passages"] = ["cohort-result"]
    elif change == "source_name":
        data["sources"]["outside.txt"] = data["sources"].pop("sources/cohort.txt")
    if change == "symlink":
        record.rename(tmp_path / "existing")
        try:
            record.symlink_to(tmp_path / "existing")
        except OSError as exc:
            pytest.skip(f"Creating symbolic links is unavailable: {exc}")
    elif change == "encoding":
        record.write_bytes(prior + b"\n")
    else:
        record.write_bytes(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    tampered = record.read_bytes()
    out = tmp_path / "out"
    assert cli("export", "--review-store", store, "--review-id", frozen["review_id"], "--out", out,
               "--private-key", tmp_path / "unused", expected=2).stderr
    assert not out.exists()
    assert cli(*build_args, expected=2, stdlib=True).stderr
    assert record.read_bytes() == tampered


def test_select_exactly_one_export_input_and_preserve_stored_omissions(tmp_path):
    suffix = ["--out", tmp_path / "out", "--private-key", tmp_path / "key"]
    store = ["--review-store", tmp_path / "store"]
    rid = ["--review-id", "a" * 64]
    for args in [store, rid, [EXAMPLE, *store, *rid], [*store, *rid, "--without-passage", "cohort-result"]]:
        assert cli("export", *args, *suffix, expected=2).stderr
    assert "review ID" in cli("export", *store, "--review-id", "../unrelated", *suffix, expected=2).stderr


def test_record_and_input_size_limits(tmp_path):
    example = tmp_path / "example"
    shutil.copytree(EXAMPLE, example)
    book = example / "casebook.json"
    book.write_bytes(book.read_bytes() + b" " * 1048576)
    store = tmp_path / "store"
    assert "byte limit" in cli("build", example, "--review-store", store, expected=2, stdlib=True).stderr
    assert not store.exists()
    store.mkdir()
    record = store / ("f" * 64 + ".json")
    with record.open("wb") as stream:
        stream.truncate((2 << 20) + 1)
    assert "size limit" in cli("export", "--review-store", store, "--review-id", record.stem,
                               "--out", tmp_path / "out", "--private-key", tmp_path / "key", expected=2).stderr


def test_interrupted_publication_is_retryable_and_leaves_no_partial_record(tmp_path, monkeypatch):
    from medextract_dossier import review_store

    original_link = review_store.os.link
    def unavailable(*args):
        raise OSError("publisher interrupted")

    store = tmp_path / "store"
    monkeypatch.setattr(review_store.os, "link", unavailable)
    with pytest.raises(OSError, match="interrupted"):
        review_store.freeze_review(EXAMPLE, store, [])
    assert list(store.iterdir()) == []
    monkeypatch.setattr(review_store.os, "link", original_link)
    reference, comparison = review_store.freeze_review(EXAMPLE, store, [])
    assert comparison["decisions"] and (store / f"{reference}.json").is_file()
