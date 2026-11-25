"""Check distributable artifacts, or verify an existing release with this file as a script."""
import json
from email.parser import BytesParser
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build(source, destination, target):
    subprocess.run([sys.executable, "-B", "-m", "hatchling", "build", "-t", target,
                    "-d", str(destination)], cwd=source, capture_output=True, check=True)


def approved_files(source):
    config = tomllib.loads((source / "pyproject.toml").read_text())
    licenses = {"LICENSE", "LICENSES/CC-BY-4.0.txt"}
    assert set(config["project"]["license-files"]) == licenses
    return {name.removeprefix("/") for name in config["tool"]["hatch"]["build"]["include"]}, licenses


def test_distributions_contain_only_reviewed_members_and_rebuild_identically(tmp_path):
    approved, _ = approved_files(ROOT)
    source = tmp_path / "source"
    for name in approved:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, source / name)
    for name in ("medextract_dossier/local_notes.py", "examples/marker-mx1/sources/unlisted.txt",
                 "tests/test_private_notes.py", "schemas/v1/unlisted.schema.json",
                 "LICENSE-LOCAL.txt", "COPYING.foo", "AUTHORS.md", "NOTICE.txt"):
        (source / name).write_text("unlisted synthetic canary\n")
    direct = tmp_path / "direct"
    build(source, direct, "wheel")
    build(source, direct, "sdist")
    check_archives(source, direct, tmp_path)


def check_archives(source, artifacts, scratch):
    approved, licenses = approved_files(source)
    wheel, = artifacts.glob("*.whl")
    sdist, = artifacts.glob("*.tar.gz")
    archive_root = sdist.name.removesuffix(".tar.gz")
    allowed_dirs = {str(parent) for name in approved for parent in (Path(archive_root) / name).parents}
    rebuilt, restored = scratch / "rebuilt", scratch / "restored"
    restored.mkdir()
    with tarfile.open(sdist) as archive:
        members = {}
        for entry in archive.getmembers():
            if entry.isdir():
                assert entry.name in allowed_dirs and entry.name != ".", entry.name
                continue
            assert entry.isfile()
            prefix, name = entry.name.split("/", 1)
            assert prefix == archive_root, entry.name
            assert name in approved | {"PKG-INFO"} and name not in members, entry.name
            members[name] = archive.extractfile(entry).read()
        assert members.keys() == approved | {"PKG-INFO"}
        for name in approved:
            assert members[name] == (source / name).read_bytes()
            (restored / name).parent.mkdir(parents=True, exist_ok=True)
            (restored / name).write_bytes(members[name])
    build(restored, rebuilt, "wheel")
    assert wheel.read_bytes() == next(rebuilt.glob("*.whl")).read_bytes()
    expected = {}
    for name in approved:
        if name.startswith("medextract_dossier/"):
            expected[name] = name
        elif name.startswith(("examples/marker-mx1/", "examples/crp-saa-trial/")):
            expected["medextract_dossier/data/" + name.removeprefix("examples/")] = name
        elif name.startswith("schemas/"):
            expected["medextract_dossier/" + name] = name
    with ZipFile(wheel) as archive:
        assert len(set(archive.namelist())) == len(archive.namelist())
        metadata_dir, = {name.split("/")[0] for name in archive.namelist() if ".dist-info/" in name}
        metadata = {f"{metadata_dir}/{name}" for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")}
        for name in licenses:
            expected[f"{metadata_dir}/licenses/{name}"] = name
        assert set(archive.namelist()) == set(expected) | metadata
        for metadata_bytes in (archive.read(f"{metadata_dir}/METADATA"), members["PKG-INFO"]):
            headers = BytesParser().parsebytes(metadata_bytes)
            assert headers["License-Expression"] == "MIT AND CC-BY-4.0"
            assert set(headers.get_all("License-File")) == licenses
            assert "Private :: Do Not Upload" not in headers.get_all("Classifier", [])
        for member, origin in expected.items():
            assert archive.read(member) == (source / origin).read_bytes()
        assert json.loads(archive.read("medextract_dossier/schemas/v1/casebook.schema.json"))["$defs"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Check the exact release archives against the source tree.")
    parser.add_argument("artifacts", type=Path, help="directory containing one wheel and one source archive")
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        check_archives(ROOT, args.artifacts.resolve(), Path(directory))
    print("Release archive contents, licenses and wheel rebuild verified.")
