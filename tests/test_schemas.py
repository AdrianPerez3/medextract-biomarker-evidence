"""Validate the shipped data and the actual CLI JSON using the local v1 schemas."""
import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


def test_shipped_schemas_describe_inputs_and_both_cli_scenarios():
    root = Path(__file__).resolve().parents[1]
    schema_dir = root / "schemas" / "v1"
    registry = Registry()
    for path in schema_dir.glob("*.schema.json"):
        schema = json.loads(path.read_bytes())
        Draft202012Validator.check_schema(schema)
        registry = registry.with_resource(path.as_uri(), Resource.from_contents(schema))

    def validator(name):
        ref = (schema_dir / f"{name}.schema.json").as_uri()
        return Draft202012Validator({"$ref": ref}, registry=registry)

    example = root / "examples" / "marker-mx1"
    validator("casebook").validate(json.loads((example / "casebook.json").read_bytes()))
    for flags in [[], ["--without-passage", "cohort-result"]]:
        proc = subprocess.run([sys.executable, "-B", "-m", "medextract_dossier",
                               str(example), "--json", *flags], cwd=root, capture_output=True, check=True)
        report = json.loads(proc.stdout)
        validator("comparison").validate(report)
        assert not validator("comparison").is_valid({**report, "evidence_origin": "real_world"})
