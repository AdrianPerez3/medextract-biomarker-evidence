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


def cli(work, *args, expected=0):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run([SCRIPTS / "medextract-dossier", *args], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    if result.returncode != expected:
        raise RuntimeError(f"CLI returned {result.returncode}, expected {expected}: {result.stderr}")
    return result


async def check_recipient(work, public_key, record):
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


def check(work, core_only):
    def decisions(*args):
        report = json.loads(cli(work, "build", *args, "--json").stdout)
        assert report["evidence_origin"] == "synthetic"
        return {entry["question_id"]: entry["status"] for entry in report["decisions"]}

    assert decisions() == {"prognosis": "association_observed", "intervention": "effect_uncertain"}
    assert decisions("--without-passage", "cohort-result") == {
        "prognosis": "insufficient", "intervention": "effect_uncertain"}
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
    asyncio.run(check_recipient(work, public, record))
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
