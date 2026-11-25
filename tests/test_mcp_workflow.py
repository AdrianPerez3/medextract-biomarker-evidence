"""Exercise the public command through a separate official MCP stdio client."""
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from jsonschema import Draft202012Validator
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "marker-mx1"
BUILD = "evidence_dossier"
EXPORT = "export_dossier"
VERIFY = "verify_dossier"
TOOLS = {
    BUILD: ({"example_id", "without_passages"},
            {"schema_version", "evidence_origin", "review_id", "review_uri", "decisions"}),
    EXPORT: ({"review_id"}, {"schema_version", "review_id", "bundle_id", "review_uri"}),
    VERIFY: ({"bundle_id"}, {"schema_version", "bundle_id", "verified", "signature",
                              "integrity", "replay", "receipt_id", "receipt_uri"}),
}
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def cli(*args, cwd=ROOT, exit_code=0):
    proc = subprocess.run([sys.executable, "-B", "-m", "medextract_dossier", *args],
                          cwd=cwd, capture_output=True, timeout=30)
    assert proc.returncode == exit_code, (proc.stdout, proc.stderr)
    return proc.stdout


def files(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def tree_state(directory):
    state = {}
    for path in [directory, *directory.rglob("*")]:
        info = path.stat()
        state[path.relative_to(directory)] = (info.st_mode, info.st_mtime_ns, info.st_size)
    return state


@pytest.fixture
def connect(tmp_path):
    # Guard the actual child interpreter. A stderr marker proves the guard ran
    # and remains separate from the stdout consumed by the official transport.
    guard = tmp_path / "offline-test"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import sys\n"
        "def offline(event, args):\n"
        "    if event in ('socket.connect', 'socket.sendto', 'socket.getaddrinfo',\n"
        "                 'socket.gethostbyname', 'socket.gethostbyaddr'):\n"
        "        print('NETWORK ATTEMPT', file=sys.stderr, flush=True)\n"
        "        raise RuntimeError('stdio test runs offline')\n"
        "sys.addaudithook(offline)\n"
        "print('stdio test guard active', file=sys.stderr, flush=True)\n", encoding="utf-8")

    @asynccontextmanager
    async def open_session(*args, cwd=ROOT):
        params = StdioServerParameters(command=sys.executable, cwd=str(cwd),
                                       args=["-B", "-m", "medextract_dossier", "mcp", *map(str, args)],
                                       env={"PYTHONPATH": str(guard)})
        malformed_stdout = []

        async def check_message(message):
            # stdio_client forwards every unparsable stdout line as an exception.
            if isinstance(message, Exception):
                malformed_stdout.append(message)

        log = guard / "stderr.log"
        with log.open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20),
                                         message_handler=check_message) as session:
                    initialized = await session.initialize()
                    assert initialized.capabilities.tools and initialized.capabilities.resources
                    yield session
                    await session.send_ping()
        assert not malformed_stdout, malformed_stdout
        stderr = log.read_text(encoding="utf-8")
        assert "stdio test guard active" in stderr
        assert "NETWORK ATTEMPT" not in stderr

    return open_session


async def completed(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    assert result.isError is False, result.content
    answer = result.structuredContent
    assert isinstance(answer, dict) and answer["schema_version"] == 1
    assert set(answer) == TOOLS[name][1]
    assert len(result.content) == 1 and result.content[0].type == "text"
    legacy_text = result.content[0].text
    assert len(legacy_text.encode("utf-8")) <= 4096
    assert json.loads(legacy_text) == answer
    return answer


async def operational_error(session, name, hidden_path, **arguments):
    result = await session.call_tool(name, arguments)
    assert result.isError is True
    assert result.structuredContent is None
    message = " ".join(block.text for block in result.content if block.type == "text")
    assert message.strip() and str(hidden_path) not in message
    return message


async def resource(session, uri):
    result = await session.read_resource(AnyUrl(uri))
    assert len(result.contents) == 1
    item = result.contents[0]
    assert str(item.uri) == uri and item.mimeType == "application/json"
    return json.loads(item.text)


def check_reference(answer, name, collection):
    identifier = answer[f"{name}_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", identifier)
    assert answer[f"{name}_uri"] == f"dossier://{collection}/{identifier}"


async def test_discovery_lists_only_the_public_contract(connect):
    async with connect() as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == set(TOOLS)
        for name, (inputs, outputs) in TOOLS.items():
            tool = tools[name]
            assert tool.description and tool.outputSchema
            Draft202012Validator.check_schema(tool.inputSchema)
            Draft202012Validator.check_schema(tool.outputSchema)
            assert set(tool.inputSchema["properties"]) == inputs
            assert set(tool.inputSchema.get("required", [])) == (set() if name == BUILD else inputs)
            assert tool.inputSchema["additionalProperties"] is False
            assert tool.annotations.readOnlyHint is (name != EXPORT)
            assert tool.annotations.openWorldHint is False
            assert set(tool.outputSchema["properties"]) == outputs
        listed = (await session.list_resource_templates()).resourceTemplates
        assert {str(t.uriTemplate): t.mimeType for t in listed} == {
            "dossier://reviews/{review_id}": "application/json",
            "dossier://receipts/{receipt_id}": "application/json",
        }
        opened = await completed(session, BUILD)
        assert opened["evidence_origin"] == "published"
        assert [item["status"] for item in opened["decisions"]] == ["effect_estimate_reported"]


async def test_producer_exports_the_exact_review_seen_by_the_client(tmp_path, connect):
    example = shutil.copytree(EXAMPLE, tmp_path / "operator-example")
    private, public = tmp_path / "sign.pem", tmp_path / "trust.pub"
    cli("keygen", "--private-key", private, "--public-key", public)
    output = tmp_path / "exports"
    reviews = {}
    original_inputs = tree_state(example)
    async with connect("--example", example, "--output-dir", output,
                       "--private-key", private, "--public-key", public) as session:
        for name, omissions in [("baseline", []), ("ablation", ["cohort-result"])]:
            flags = ["--without-passage", "cohort-result"] if omissions else []
            full = json.loads(cli("build", example, "--json", *flags))
            reference = tmp_path / name
            cli("export", example, "--out", reference, "--private-key", private, *flags)
            args = {"without_passages": omissions}
            answer = await completed(session, BUILD, **args)
            check_reference(answer, "review", "reviews")
            assert answer["evidence_origin"] == "synthetic"
            assert answer == await completed(session, BUILD, example_id="marker-mx1", **args)
            assert await resource(session, answer["review_uri"]) == full
            assert answer["decisions"] == [
                {"question_id": d["question_id"], "status": d["status"], "reason": d["reason"],
                 "eligible_study_count": len(d["eligible_families"])} for d in full["decisions"]
            ]
            if omissions:
                prognosis = next(d for d in answer["decisions"] if d["question_id"] == "prognosis")
                assert (prognosis["status"], prognosis["reason"]) == ("insufficient", "no_direct_result")
            reviews[name] = answer

        assert reviews["baseline"]["review_id"] != reviews["ablation"]["review_id"]
        assert tree_state(example) == original_inputs
        assert not output.exists()
        # The signed bytes must survive even the loss of an operator input.
        (example / "sources" / "cohort.txt").unlink()
        await operational_error(session, BUILD, tmp_path)
        for name, review in reviews.items():
            args = {"review_id": review["review_id"]}
            exported = await completed(session, EXPORT, **args)
            assert exported == {"schema_version": 1, **args, "bundle_id": "review-" + args["review_id"],
                                "review_uri": review["review_uri"]}
            assert exported == await completed(session, EXPORT, **args)
            bundle_id = exported["bundle_id"]
            bundle = output / bundle_id
            assert files(bundle) == files(tmp_path / name)
            assert await resource(session, review["review_uri"]) == json.loads((bundle / "decision.json").read_bytes())
            receipt = await completed(session, VERIFY, bundle_id=bundle_id)
            check_reference(receipt, "receipt", "receipts")
            assert receipt["verified"] is True
            assert receipt == await completed(session, VERIFY, bundle_id=bundle_id)
            report = await resource(session, receipt["receipt_uri"])
            assert report["review_id"] == review["review_id"]
            assert report == json.loads(cli("verify", bundle, "--public-key", public, "--json"))

    async with connect("--example", example, "--output-dir", output, "--private-key", private) as session:
        old_review = reviews["baseline"]
        message = await operational_error(session, EXPORT, tmp_path, review_id=old_review["review_id"])
        assert "build" in message.lower() or "again" in message.lower()
        with pytest.raises(McpError):
            await resource(session, old_review["review_uri"])


async def test_recipient_checks_a_selected_copy_without_producer_state(tmp_path, connect):
    producer = tmp_path / "producer"
    producer.mkdir()
    private, public = producer / "private.pem", producer / "public.pem"
    cli("keygen", "--private-key", private, "--public-key", public)
    original = producer / "original"
    cli("export", EXAMPLE, "--out", original, "--private-key", private)
    cli("keygen", "--private-key", producer / "other.pem", "--public-key", producer / "other.pub")

    # Source-only recipient runtime has no examples, fixture oracle or signing key.
    receiver = tmp_path / "recipient"
    receiver.mkdir()
    shutil.copytree(ROOT / "medextract_dossier", receiver / "medextract_dossier",
                    ignore=shutil.ignore_patterns("__pycache__", "data"))
    bundle = shutil.copytree(original, receiver / "incoming")
    trusted = receiver / "trusted.pub"
    shutil.copyfile(public, trusted)
    other = receiver / "other.pub"
    shutil.copyfile(producer / "other.pub", other)
    shutil.rmtree(producer)

    async with connect("--no-example", "--public-key", trusted,
                       "--bundle", f"received={bundle}", cwd=receiver) as session:
        receipts = []
        for valid in [True, False]:
            if not valid:
                path = bundle / "report.html"
                path.write_bytes(path.read_bytes() + b"\n")
            before = tree_state(receiver)
            checked = await completed(session, VERIFY, bundle_id="received")
            check_reference(checked, "receipt", "receipts")
            assert checked["bundle_id"] == "received" and checked["verified"] is valid
            report = await resource(session, checked["receipt_uri"])
            assert report == json.loads(cli("verify", bundle, "--public-key", trusted, "--json",
                                             cwd=receiver, exit_code=0 if valid else 1))
            assert all(checked[k] == report[k] for k in ("signature", "integrity", "replay"))
            assert report["members"]["report.html"] == ("match" if valid else "changed")
            assert tree_state(receiver) == before
            receipts.append((checked["receipt_uri"], report))
        assert receipts[0][0] != receipts[1][0]
        assert await resource(session, receipts[0][0]) == receipts[0][1]

        await operational_error(session, VERIFY, tmp_path, bundle_id="unregistered")
        await operational_error(session, BUILD, tmp_path)
        bundle.rename(receiver / "moved")
        await operational_error(session, VERIFY, tmp_path, bundle_id="received")

    bundle = receiver / "moved"
    async with connect("--no-example", "--public-key", other,
                       "--bundle", f"received={bundle}", cwd=receiver) as session:
        checked = await completed(session, VERIFY, bundle_id="received")
        assert checked["verified"] is False and checked["signature"] == "invalid"
        assert checked["integrity"] == checked["replay"] == "not_checked"
        report = await resource(session, checked["receipt_uri"])
        assert report == json.loads(cli("verify", bundle, "--public-key", other, "--json",
                                         cwd=receiver, exit_code=1))
        assert report["claimed_keyid"] != report["supplied_key_fingerprint"]


async def test_operational_failures_use_tool_errors_and_leave_session_usable(tmp_path, connect):
    async with connect("--example", EXAMPLE) as session:
        await operational_error(session, BUILD, tmp_path, example_id="no-such-example")
        await operational_error(session, BUILD, tmp_path, without_passages=["no-such-passage"])
        await operational_error(session, BUILD, tmp_path, without_passages='["cohort-result"]')
        await operational_error(session, BUILD, tmp_path, example_dir="unconfigured")
        review = await completed(session, BUILD)
        await operational_error(session, EXPORT, tmp_path, review_id=review["review_id"])
        await operational_error(session, VERIFY, tmp_path, bundle_id="received")
        assert await completed(session, BUILD) == review
