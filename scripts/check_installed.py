"""Exercise the installed wheel's entry points from an empty working directory."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sysconfig
from tempfile import TemporaryDirectory


SCRIPTS = Path(sysconfig.get_path("scripts"))


def cli(work, *args, expected=0, extra_env=None):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(extra_env or {})
    result = subprocess.run([SCRIPTS / "medextract-dossier", *args], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    if result.returncode != expected:
        raise RuntimeError(f"CLI returned {result.returncode}, expected {expected}: {result.stderr}")
    return result


def offline_provider(work):
    """Inject replies into the installed process and fail on actual DNS or socket access."""
    fixture = work / "offline-provider"
    fixture.mkdir()
    (fixture / "sitecustomize.py").write_text(
        """import json, os, sys
from pathlib import Path
from medextract_dossier import literature_http
def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        Path("unexpected-network").touch()
        raise RuntimeError("Network disabled in installed-wheel smoke")
sys.addaudithook(guard)
def fetch(url, **kwargs):
    with Path("fixture-calls").open("ab") as counter:
        counter.write(b".")
    if Path("provider-outage").exists():
        raise TimeoutError
    if os.environ.get("DISCOVERY_SMOKE_EPMC_TIMEOUT") and "ebi.ac.uk" in url:
        raise TimeoutError
    if os.environ.get("DISCOVERY_SMOKE_EPMC_RECOVER"):
        if "ebi.ac.uk" not in url:
            raise AssertionError("Recovery contacted a successful provider")
        return b'{"hitCount":1,"resultList":{"result":[{"source":"MED","id":"202"}]}}'
    if "esearch.fcgi" in url:
        return json.dumps({"esearchresult": {"idlist": ["101"], "count": "1"}}).encode()
    return json.dumps({"hitCount": 0, "resultList": {"result": []}}).encode()
literature_http.fetch_bytes = fetch
""", encoding="utf-8")
    return {"PYTHONPATH": str(fixture)}


async def check_recipient(work, public_key, record, fixture_env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=str(SCRIPTS / "medextract-dossier-mcp"), cwd=str(work),
                                   args=["--no-example", "--bundle", f"received={work / 'bundle'}",
                                         "--public-key", str(public_key)])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"evidence_dossier", "export_dossier", "verify_dossier"}
            result = await session.call_tool("verify_dossier", {"bundle_id": "received"})
            assert not result.isError and result.structuredContent["verified"] is True
            receipt = await session.read_resource(result.structuredContent["receipt_uri"])
            assert json.loads(receipt.contents[0].text) == record
    # Enabling online tools must not contact either provider during startup or discovery.
    params = StdioServerParameters(command=str(SCRIPTS / "medextract-dossier-mcp"), cwd=str(work),
                                   args=["--literature"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            for name in ("research_biomarker", "read_paper"):
                assert tools[name].annotations.openWorldHint and tools[name].annotations.readOnlyHint

    outage = work / "provider-outage"
    outage.touch()
    params = StdioServerParameters(command=str(SCRIPTS / "medextract-dossier-mcp"), cwd=str(work),
                                   args=["--discovery"], env=fixture_env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 8
            assert tools["recover_discovery"].annotations == tools["retry_discovery"].annotations
            assert tools["reuse_discovery"].annotations.idempotentHint
            assert not tools["reuse_discovery"].annotations.openWorldHint
            assert len((await session.list_resource_templates()).resourceTemplates) == 4
            args = {"question": "Published biomarker studies?", "concept_groups": [["cystatin C"], ["kidney"]],
                    "include_title_search": True}
            assert (await session.call_tool("discover_candidates", {**args, "depth": 0})).isError
            result = await session.call_tool("discover_candidates", args)
            assert not result.isError
            summary = result.structuredContent
            assert summary["status"] == "unavailable" and summary["candidate_count"] == 0
            receipt = await session.read_resource(summary["manifest_uri"])
            assert [lane["status"] for lane in json.loads(receipt.contents[0].text)["lanes"]] == ["unavailable"] * 4
            page = await session.call_tool("get_discovery_page", {"search_id": summary["search_id"]})
            assert not page.isError and page.structuredContent["rows"] == []
            retry = await session.call_tool("retry_discovery", {"search_id": summary["search_id"]})
            assert not retry.isError and retry.structuredContent["status"] == "unavailable"
            assert retry.structuredContent["recovery"]["request_count"] == 4
            assert retry.structuredContent["recovery"]["parent_search_id"] == summary["search_id"]
            assert retry.structuredContent["search_id"] != summary["search_id"]
            assert (await session.read_resource(summary["manifest_uri"])).contents == receipt.contents
            outage.unlink()
            donor = await session.call_tool("discover_candidates", args)
            assert not donor.isError and donor.structuredContent["status"] == "ok"
            calls = (work / "fixture-calls").stat().st_size
            automatic = await session.call_tool("recover_discovery", {
                "search_id": summary["search_id"], "max_gap_seconds": 120,
            })
            assert not automatic.isError, automatic.content
            receipt_auto = automatic.structuredContent["automatic_recovery"]
            assert receipt_auto["request_count"] == 0 and receipt_auto["lookup_at"] is None
            assert len(receipt_auto["lanes"]) == 4
            assert all(lane["action"] == "reused" and lane["donor_search_id"] == donor.structuredContent["search_id"]
                       for lane in receipt_auto["lanes"])
            reused = await session.call_tool("reuse_discovery", {
                "search_id": summary["search_id"], "donor_search_id": donor.structuredContent["search_id"],
                "max_gap_seconds": 120,
            })
            assert not reused.isError, reused.content
            child = reused.structuredContent
            assert child["status"] == "ok" and child["reuse"]["request_count"] == 0
            assert len(child["reuse"]["reused_lanes"]) == 4
            assert child["preview"] == donor.structuredContent["preview"]
            manifest = await session.read_resource(child["manifest_uri"])
            assert json.loads(manifest.contents[0].text)["reuse"] == child["reuse"]
            assert (work / "fixture-calls").stat().st_size == calls
            assert not (work / "unexpected-network").exists()


def check(work, core_only):
    fixture_env = offline_provider(work)
    run = cli(work, "discover", "--question", "Published biomarker studies?", "--concept", "cystatin C",
              "--concept", "kidney", "--out", "capture", extra_env=fixture_env)
    summary = json.loads(run.stdout)
    assert summary["status"] == "ok" and summary["preview"][0]["pmid"] == "101"
    assert (work / "capture" / "candidates.tsv").is_file() and not (work / "unexpected-network").exists()
    partial = cli(work, "discover", "--question", "Published biomarker studies?", "--concept", "cystatin C",
                  "--concept", "kidney", "--out", "primary", expected=3,
                  extra_env={**fixture_env, "DISCOVERY_SMOKE_EPMC_TIMEOUT": "1"})
    child = cli(work, "retry-discovery", "primary", "--out", "recovered",
                extra_env={**fixture_env, "DISCOVERY_SMOKE_EPMC_RECOVER": "1"})
    receipt = json.loads(child.stdout)["recovery"]
    assert receipt["parent_search_id"] == json.loads(partial.stdout)["search_id"]
    assert receipt["retried_lanes"] == ["epmc_broad"]
    assert receipt["candidate_count_added"] == receipt["request_count"] == 1
    calls = (work / "fixture-calls").stat().st_size
    reuse_args = ["reuse-discovery", "primary", "--from", "capture", "--max-gap-seconds", "120", "--out", "reused"]
    reused = json.loads(cli(work, *reuse_args, extra_env=fixture_env).stdout)
    assert reused["status"] == "ok" and reused["reuse"]["request_count"] == 0
    assert reused["reuse"]["reused_lanes"] == ["epmc_broad"]
    assert reused["reuse"]["donor_search_id"] == summary["search_id"]
    assert reused["reuse"]["parent_search_id"] == json.loads(partial.stdout)["search_id"]
    assert json.loads((work / "reused" / "search.json").read_text(encoding="utf-8"))["reuse"] == reused["reuse"]
    cli(work, *reuse_args, expected=2, extra_env=fixture_env)
    assert (work / "fixture-calls").stat().st_size == calls
    assert not (work / "unexpected-network").exists()

    assert "--lookup-at" in cli(work, "recover-discovery", "--help").stdout
    cache = work / "cache"
    cache.mkdir()
    (work / "capture").rename(cache / "capture")
    automatic = ["recover-discovery", "primary", "--cache-dir", "cache", "--max-gap-seconds", "120"]
    report = json.loads(cli(work, *automatic, "--out", "automatic", extra_env=fixture_env).stdout)
    recovery = report["automatic_recovery"]
    assert recovery["request_count"] == 0 and recovery["lookup_at"] is None
    assert recovery["lanes"][0]["donor_search_id"] == summary["search_id"]
    assert (work / "fixture-calls").stat().st_size == calls
    (cache / "capture").rename(work / "capture")
    fetched = cli(work, *automatic, "--lookup-at", "now", "--fetch-missing", "--out", "fetched",
                  extra_env={**fixture_env, "DISCOVERY_SMOKE_EPMC_RECOVER": "1"})
    receipt = json.loads(fetched.stdout)["automatic_recovery"]
    assert receipt["request_count"] == 1 and receipt["lanes"][0]["action"] == "retried" and receipt["lookup_at"]
    assert (work / "fixture-calls").stat().st_size == calls + 1 and not (work / "unexpected-network").exists()

    def decisions(*args):
        report = json.loads(cli(work, "build", *args, "--json").stdout)
        assert report["evidence_origin"] == "published"
        return {entry["question_id"]: entry["status"] for entry in report["decisions"]}

    assert decisions() == {"intervention": "effect_estimate_reported"}
    assert decisions("--without-passage", "index-antibiotic-prescribing") == {"intervention": "insufficient"}
    private, public = work / "keys" / "producer.pem", work / "keys" / "producer.pub.pem"
    keygen = ["keygen", "--private-key", private, "--public-key", public]
    if core_only:
        assert "[handoff]" in cli(work, *keygen, expected=2).stderr
        assert "[mcp]" in cli(work, "mcp", expected=2).stderr
        return

    frozen = json.loads(cli(work, "build", "--review-store", "store", "--json").stdout)
    cli(work, *keygen)
    cli(work, "export", "--review-store", "store", "--review-id", frozen["review_id"],
        "--out", "bundle", "--private-key", private)
    private.unlink()
    verify = ["verify", "bundle", "--public-key", public, "--json"]
    record = json.loads(cli(work, *verify).stdout)
    assert record["verified"] is True and record["review_id"] == frozen["review_id"]
    asyncio.run(check_recipient(work, public, record, fixture_env))
    (work / "bundle" / "report.html").write_text("Changed after signing.", encoding="utf-8")
    changed = json.loads(cli(work, *verify, expected=1).stdout)
    assert changed["verified"] is False and changed["members"]["report.html"] == "changed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="check a fresh install without extras")
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        check(Path(directory), args.core_only)
    print("Installed wheel checks passed.")
