"""Exercise the public discovery entry points with captured, offline responses."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event
from urllib.parse import urlsplit

from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from medextract_dossier.__main__ import main
from medextract_dossier.mcp_server import create_server
from medextract_dossier.service import DossierService, ServiceError


ARGUMENTS = {"question": "Which studies?", "concept_groups": [["marker"], ["population"]]}
CLI_ARGS = ["discover", "--question", "Which studies?", "--concept", "marker", "--concept", "population"]


@pytest.fixture
def offline(monkeypatch):
    from medextract_dossier import discovery_providers

    collect = discovery_providers.collect_discovery
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        if urlsplit(url).hostname == "eutils.ncbi.nlm.nih.gov":
            body = {"esearchresult": {"count": "12", "idlist": [str(n) for n in range(1, 13)]}}
        else:
            body = {"hitCount": 1, "resultList": {"result": [{"id": "21", "source": "MED", "pmid": "21"}]}}
        return json.dumps(body).encode()

    monkeypatch.setattr(discovery_providers, "collect_discovery", lambda request: collect(request, fetch=fetch))
    return calls


def test_independent_discovery_flag_and_annotations():
    async def run():
        for literature, discovery, ntools, nresources in (
                (False, False, 3, 2), (True, False, 5, 2), (False, True, 8, 4), (True, True, 10, 4)):
            server = create_server(DossierService(), literature=literature, discovery=discovery)
            async with create_connected_server_and_client_session(server) as session:
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert len(tools) == ntools
                templates = (await session.list_resource_templates()).resourceTemplates
                assert len(templates) == nresources
                assert ("research_biomarker" in tools) == literature
                assert ("discover_candidates" in tools) == discovery
                assert ("retry_discovery" in tools) == discovery
                assert ("reuse_discovery" in tools) == discovery
                assert ("recover_discovery" in tools) == discovery
                if discovery:
                    capture = tools["discover_candidates"]
                    retry = tools["retry_discovery"]
                    assert retry.annotations == capture.annotations
                    page = tools["get_discovery_page"]
                    assert capture.annotations.openWorldHint and not capture.annotations.idempotentHint
                    assert not capture.annotations.readOnlyHint
                    assert page.annotations.readOnlyHint and page.annotations.idempotentHint
                    assert not page.annotations.openWorldHint
                    reuse = tools["reuse_discovery"]
                    assert reuse.annotations.idempotentHint and not reuse.annotations.openWorldHint
                    assert not reuse.annotations.readOnlyHint and not reuse.annotations.destructiveHint
                    assert set(reuse.inputSchema["required"]) == {"search_id", "donor_search_id", "max_gap_seconds"}
                    automatic = tools["recover_discovery"]
                    assert automatic.annotations == capture.annotations
                    assert set(automatic.inputSchema["required"]) == {"search_id", "max_gap_seconds"}
                    assert automatic.inputSchema["properties"]["fetch_missing"]["default"] is False
                    for tool in (capture, retry, page, reuse, automatic):
                        assert tool.inputSchema["additionalProperties"] is False
                        assert tool.outputSchema["additionalProperties"] is False

    asyncio.run(run())


def test_cli_help_and_unusable_input(tmp_path, capsys):
    with pytest.raises(SystemExit) as help_result:
        main(["discover", "--help"])
    assert help_result.value.code == 0
    assert "--until-year" in capsys.readouterr().out
    out = tmp_path / "new"
    assert main([*CLI_ARGS, "--depth", "0", "--out", str(out)]) == 2
    assert not out.exists()


def test_cli_reserves_once_and_writes_matching_pair(tmp_path, offline, capsys):
    out = tmp_path / "snapshot"
    assert main([*CLI_ARGS, "--out", str(out)]) == 0
    preview = json.loads(capsys.readouterr().out)
    tsv = (out / preview["candidates_file"]).read_bytes()
    manifest = json.loads((out / preview["manifest_file"]).read_bytes())
    assert "manifest_uri" not in preview and "candidates_uri" not in preview
    assert manifest["search_id"] == preview["search_id"]
    assert hashlib.sha256(tsv).hexdigest() == manifest["candidates_sha256"]
    assert preview["candidate_count"] == 13 and len(preview["preview"]) == 10
    assert main([*CLI_ARGS, "--out", str(out)]) == 2
    assert (out / "candidates.tsv").read_bytes() == tsv and len(offline) == 2


def test_cli_cleans_failed_reservation_and_preserves_other_files(tmp_path, offline, monkeypatch, capsys):
    from medextract_dossier import discovery_providers

    collect = discovery_providers.collect_discovery
    out = tmp_path / "snapshot"
    def fail(request):
        assert out.is_dir()
        raise ValueError("deliberate internal error at /private/operator/path")
    monkeypatch.setattr(discovery_providers, "collect_discovery", fail)
    assert main([*CLI_ARGS, "--out", str(out)]) == 2
    assert "/private/operator/path" not in capsys.readouterr().err
    assert not out.exists()
    def foreign(request):
        (out / "unrelated").write_bytes(b"keep")
        return fail(request)
    monkeypatch.setattr(discovery_providers, "collect_discovery", foreign)
    assert main([*CLI_ARGS, "--out", str(out)]) == 2
    assert (out / "unrelated").read_bytes() == b"keep"
    (out / "unrelated").unlink()
    out.rmdir()
    monkeypatch.setattr(discovery_providers, "collect_discovery", collect)
    assert main([*CLI_ARGS, "--out", str(out)]) == 0


def test_concurrent_export_reserves_before_provider_io(tmp_path, offline, monkeypatch):
    from medextract_dossier import discovery_providers

    entered, release = Event(), Event()
    collect = discovery_providers.collect_discovery
    def blocked(request):
        entered.set()
        assert release.wait(5)
        return collect(request)
    monkeypatch.setattr(discovery_providers, "collect_discovery", blocked)
    args = [*CLI_ARGS, "--out", str(tmp_path / "same")]
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(main, args)
        try:
            assert entered.wait(5)
            assert pool.submit(main, args).result(timeout=4) == 2
        finally:
            release.set()
        assert first.result(timeout=5) == 0
    assert len(offline) == 2


def test_paging_and_resources_share_snapshot_and_make_no_requests(offline):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("discover_candidates", ARGUMENTS)
            assert not result.isError, result.content
            summary = result.structuredContent
            assert summary["candidate_count"] == 13 and summary["next_offset"] == 10
            args = {"search_id": summary["search_id"], "offset": summary["next_offset"]}
            page = await session.call_tool("get_discovery_page", args)
            assert not page.isError, page.content
            rows = summary["preview"] + page.structuredContent["rows"]
            assert len(rows) == 13 and page.structuredContent["next_offset"] is None
            first = await session.read_resource(summary["candidates_uri"])
            again = await session.read_resource(summary["candidates_uri"])
            assert first.contents == again.contents
            assert first.contents[0].mimeType == "text/tab-separated-values"
            tsv = first.contents[0].text
            assert [row["pmid"] for row in rows] == [line.split("\t")[0] for line in tsv.splitlines()[1:]]
            manifest = await session.read_resource(summary["manifest_uri"])
            assert hashlib.sha256(tsv.encode()).hexdigest() == json.loads(manifest.contents[0].text)["candidates_sha256"]
            invalid = await session.call_tool("get_discovery_page", {**args, "offset": 14})
            assert invalid.isError
            unknown = await session.call_tool("get_discovery_page", {**args, "search_id": "f" * 64})
            assert unknown.isError and "again" in unknown.content[0].text.lower()
            for bad in ({"depth": True}, {"output_dir": "/private/operator/path"}, {"concept_groups": "bad"}):
                assert (await session.call_tool("discover_candidates", {**ARGUMENTS, **bad})).isError
            assert len(offline) == 2

    asyncio.run(run())


def test_profile_all_indexed_reaches_stdio_contract(offline):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("discover_candidates", {**ARGUMENTS, "profile": "all_indexed"})
            assert not result.isError, result.content
            assert result.structuredContent["profile"] == "all_indexed"
            assert result.structuredContent["preview"][0]["row_id"]
    asyncio.run(run())


def test_discovery_cache_eviction_and_restart(offline):
    from medextract_dossier.discovery import DiscoveryRequest

    def request(question):
        return DiscoveryRequest(question, ARGUMENTS["concept_groups"])

    service = DossierService()
    ids = [service.discover(request(f"Research question {index}"))["search_id"] for index in range(17)]
    with pytest.raises(ServiceError, match="again"):
        service.discovery_snapshot(ids[0])
    assert service.discovery_snapshot(ids[-1]).search_id == ids[-1]
    with pytest.raises(ServiceError, match="again"):
        DossierService().discovery_snapshot(ids[-1])


@pytest.mark.parametrize("available,expected", [(True, "partial"), (False, "unavailable")])
def test_cli_writes_reviewable_export_even_on_provider_outage(tmp_path, monkeypatch, capsys, available, expected):
    from medextract_dossier import discovery_providers

    collect = discovery_providers.collect_discovery
    def fetch(url, **kwargs):
        if available and "eutils.ncbi.nlm.nih.gov" in url:
            return b'{"esearchresult":{"count":"1","idlist":["3"]}}'
        raise OSError("private proxy path /private/operator")
    monkeypatch.setattr(discovery_providers, "collect_discovery", lambda request: collect(request, fetch=fetch))
    out = tmp_path / "snapshot"
    assert main([*CLI_ARGS, "--out", str(out)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == expected
    manifest = (out / "search.json").read_text(encoding="utf-8")
    assert "/private/operator" not in manifest
    assert json.loads(manifest)["candidate_count"] == int(available)


def test_operator_discovery_mode_does_not_select_a_casebook(monkeypatch):
    from medextract_dossier import mcp_config, mcp_server
    captured = []
    monkeypatch.setattr(mcp_server, "run_server", lambda service, **flags: captured.append((service, flags)))
    assert mcp_config.main(["--discovery"]) == 0
    model, flags = captured.pop()
    assert flags == {"discovery": True, "literature": False}
    with pytest.raises(ServiceError, match="unavailable"):
        model.build()


def test_discovery_cancellation_releases_worker_after_fetch_returns(offline, monkeypatch):
    from queue import Queue
    from mcp import types
    from mcp.shared.exceptions import McpError
    from medextract_dossier import discovery_providers

    ids, release, finished = Queue(), Event(), Event()
    collect = discovery_providers.collect_discovery
    server = create_server(DossierService(), discovery=True)
    def block(request):
        ids.put(server.get_context().request_context.request_id)
        try:
            assert release.wait(5)
            return collect(request)
        finally:
            finished.set()
    monkeypatch.setattr(discovery_providers, "collect_discovery", block)
    async def run():
        async with create_connected_server_and_client_session(server) as session:
            pending = asyncio.create_task(session.call_tool("discover_candidates", ARGUMENTS))
            try:
                request_id = await asyncio.to_thread(ids.get, True, 4)
                await session.send_notification(types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=request_id))))
                with pytest.raises(McpError, match="(?i)cancel"):
                    await asyncio.wait_for(pending, 4)
            finally:
                release.set()
            assert await asyncio.to_thread(finished.wait, 4)
            monkeypatch.setattr(discovery_providers, "collect_discovery", collect)
            recovered = await session.call_tool("discover_candidates", ARGUMENTS)
            assert not recovered.isError, recovered.content
    asyncio.run(run())


def test_concurrent_foreign_completion_marker_is_never_replaced(tmp_path, offline, monkeypatch):
    from medextract_dossier import discovery_providers

    collect = discovery_providers.collect_discovery
    out = tmp_path / "snapshot"
    def concurrent(request):
        (out / "search.json").write_bytes(b"foreign marker")
        return collect(request)
    monkeypatch.setattr(discovery_providers, "collect_discovery", concurrent)
    assert main([*CLI_ARGS, "--out", str(out)]) == 2
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {"search.json": b"foreign marker"}


@pytest.fixture
def epmc_timeout(monkeypatch):
    from medextract_dossier import literature_http

    calls = []
    def fetch(url):
        host = urlsplit(url).hostname
        calls.append(host)
        if host == "eutils.ncbi.nlm.nih.gov":
            return b'{"esearchresult":{"count":"1","idlist":["101"]}}'
        if calls.count(host) == 1:
            raise TimeoutError
        return b'{"hitCount":1,"resultList":{"result":[{"source":"MED","id":"202"}]}}'
    monkeypatch.setattr(literature_http, "fetch_bytes", fetch)
    return calls


def test_retry_cli_preserves_source_and_reports_targeted_attempt(tmp_path, epmc_timeout, capsys):
    source, child = tmp_path / "primary", tmp_path / "followup"
    assert main([*CLI_ARGS, "--out", str(source)]) == 3
    saved = {p.name: p.read_bytes() for p in source.iterdir()}
    original = json.loads(capsys.readouterr().out)
    args = ["retry-discovery", str(source), "--out"]
    assert main([*args, str(source)]) == 2
    assert len(epmc_timeout) == 2
    assert main([*args, str(child)]) == 0
    summary = json.loads(capsys.readouterr().out)
    receipt = json.loads((child / "search.json").read_bytes())["recovery"]
    assert summary["search_id"] != original["search_id"]
    assert summary["recovery"] == receipt
    assert receipt["parent_search_id"] == original["search_id"]
    assert receipt["retried_lanes"] == ["epmc_broad"]
    assert receipt["candidate_count_added"] == receipt["request_count"] == 1
    assert {p.name: p.read_bytes() for p in source.iterdir()} == saved
    assert len(epmc_timeout) == 3 and epmc_timeout[-1] == "www.ebi.ac.uk"

    unused = tmp_path / "complete"
    assert main(["retry-discovery", str(child), "--out", str(unused)]) == 2
    assert "no requested provider search timed out" in capsys.readouterr().err.lower()
    assert len(epmc_timeout) == 3 and not unused.exists()


@pytest.mark.parametrize("problem", ["absent", "oversize", "tampered", "invalid_utf8", "fifo"])
def test_retry_cli_rejects_invalid_export_before_network(tmp_path, epmc_timeout, capsys, problem):
    import os
    from medextract_dossier.discovery import MAX_SNAPSHOT_BYTES

    source, child = tmp_path / "primary", tmp_path / "child"
    assert main([*CLI_ARGS, "--out", str(source)]) == 3
    manifest = source / "search.json"
    if problem == "absent":
        manifest.unlink()
    elif problem == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation requires POSIX")
        manifest.unlink()
        os.mkfifo(manifest)
    elif problem == "invalid_utf8":
        manifest.write_bytes(b"\xff")
    elif problem == "oversize":
        with manifest.open("wb") as handle:
            handle.truncate(MAX_SNAPSHOT_BYTES + 1)
    else:
        (source / "candidates.tsv").write_bytes(b"changed")
    assert main(["retry-discovery", str(source), "--out", str(child)]) == 2
    assert len(epmc_timeout) == 2 and not child.exists()
    assert str(tmp_path) not in capsys.readouterr().err


def test_mcp_retry_returns_child_and_keeps_parent_browsable(epmc_timeout):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("discover_candidates", ARGUMENTS)
            assert not result.isError
            parent = result.structuredContent
            manifest = (await session.read_resource(parent["manifest_uri"])).contents
            for args in ({"search_id": "f" * 64}, {"search_id": parent["search_id"], "path": "other"}):
                assert (await session.call_tool("retry_discovery", args)).isError
            assert len(epmc_timeout) == 2
            reply = await session.call_tool("retry_discovery", {"search_id": parent["search_id"]})
            assert not reply.isError, reply.content
            child = reply.structuredContent
            assert child["status"] == "ok" and child["candidate_count"] == 2
            assert child["recovery"]["parent_search_id"] == parent["search_id"]
            assert child["recovery"]["retried_lanes"] == ["epmc_broad"]
            assert child["recovery"]["request_count"] == 1
            new_manifest = (await session.read_resource(child["manifest_uri"])).contents[0].text
            assert json.loads(new_manifest)["recovery"] == child["recovery"]
            assert (await session.read_resource(parent["manifest_uri"])).contents == manifest
            again = await session.call_tool("retry_discovery", {"search_id": child["search_id"]})
            assert again.isError and "no requested provider search timed out" in again.content[0].text.lower()
            assert len(epmc_timeout) == 3 and epmc_timeout[-1] == "www.ebi.ac.uk"

    asyncio.run(run())


def test_reuse_cli_creates_deterministic_export_without_fetching(tmp_path, epmc_timeout, monkeypatch, capsys):
    from medextract_dossier import discovery_providers
    from medextract_dossier.discovery import MAX_SNAPSHOT_BYTES

    monkeypatch.setattr(discovery_providers, "_utc", lambda: "2026-01-01T12:00:00+00:00")
    parent, donor, child = (tmp_path / name for name in ("parent", "donor", "child"))
    assert main([*CLI_ARGS, "--out", str(parent)]) == 3
    assert main([*CLI_ARGS, "--out", str(donor)]) == 0
    capsys.readouterr()
    originals = {p: p.read_bytes() for export in (parent, donor) for p in export.iterdir()}
    args = ["reuse-discovery", str(parent), "--from", str(donor), "--max-gap-seconds", "0", "--out"]
    assert main([*args, str(child)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["reuse"] == {
        "parent_search_id": json.loads(originals[parent / "search.json"])["search_id"],
        "donor_search_id": json.loads(originals[donor / "search.json"])["search_id"],
        "max_gap_seconds": 0, "reused_lanes": ["epmc_broad"], "candidate_count_before": 1,
        "candidate_count_after": 2, "candidate_count_added": 1, "request_count": 0,
    }
    manifest = json.loads((child / "search.json").read_bytes())
    assert manifest["reuse"] == summary["reuse"] and "recovery" not in manifest
    assert summary["candidates_file"] == "candidates.tsv" and summary["manifest_file"] == "search.json"
    for taken in (parent, donor, child):
        assert main([*args, str(taken)]) == 2
    second = tmp_path / "second"
    assert main([*args, str(second)]) == 0
    for member in ("search.json", "candidates.tsv"):
        assert (child / member).read_bytes() == (second / member).read_bytes()
    assert all(p.read_bytes() == saved for p, saved in originals.items())
    with (donor / "search.json").open("wb") as stream:
        stream.truncate(MAX_SNAPSHOT_BYTES + 1)
    invalid = tmp_path / "invalid"
    assert main([*args, str(invalid)]) == 2
    assert not invalid.exists() and len(epmc_timeout) == 4


def test_mcp_reuse_requires_both_cached_ids_and_never_fetches(epmc_timeout):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            parent = (await session.call_tool("discover_candidates", ARGUMENTS)).structuredContent
            donor = (await session.call_tool("discover_candidates", ARGUMENTS)).structuredContent
            args = {"search_id": parent["search_id"], "donor_search_id": donor["search_id"], "max_gap_seconds": 60}
            manifests = [(await session.read_resource(item["manifest_uri"])).contents for item in (parent, donor)]
            for invalid in ({"search_id": "f" * 64}, {"donor_search_id": "e" * 64}, {"path": "unexpected"},
                            *({"max_gap_seconds": gap} for gap in (True, -1, 1.0, 1.5, "1"))):
                assert (await session.call_tool("reuse_discovery", {**args, **invalid})).isError
            response = await session.call_tool("reuse_discovery", args)
            assert not response.isError, response.content
            child = response.structuredContent
            assert child["reuse"]["request_count"] == 0 and child["reuse"]["reused_lanes"] == ["epmc_broad"]
            assert child["reuse"]["donor_search_id"] == donor["search_id"]
            assert child["reuse"]["parent_search_id"] == parent["search_id"]
            assert child["candidate_count"] == 2 and child["status"] == "ok"
            repeat = await session.call_tool("reuse_discovery", args)
            assert repeat.structuredContent == child
            page = await session.call_tool("get_discovery_page", {"search_id": child["search_id"]})
            assert page.structuredContent["rows"] == child["preview"]
            child_manifest = (await session.read_resource(child["manifest_uri"])).contents[0].text
            assert json.loads(child_manifest)["reuse"] == child["reuse"]
            for source, content in zip((parent, donor), manifests):
                assert (await session.read_resource(source["manifest_uri"])).contents == content
            assert len(epmc_timeout) == 4

    asyncio.run(run())


def test_recover_discovery_help(capsys):
    with pytest.raises(SystemExit) as status:
        main(["recover-discovery", "--help"])
    assert status.value.code == 0
    help_text = capsys.readouterr().out
    assert all(option in help_text for option in ("--cache-dir", "--max-gap-seconds", "--lookup-at", "--fetch-missing"))


def test_recover_cli_searches_immediate_exports_offline(tmp_path, epmc_timeout, capsys):
    parent, cache, child = (tmp_path / name for name in ("parent", "cache", "child"))
    cache.mkdir()
    unrelated = cache / "unrelated"
    unrelated.mkdir()
    nested = unrelated / "nested"
    nested.mkdir()
    (nested / "search.json").write_bytes(b"malformed nested file outside search scope")
    (cache / "notes").write_text("not an export")
    assert main([*CLI_ARGS, "--out", str(parent)]) == 3
    donor = cache / "capture"
    assert main([*CLI_ARGS, "--out", str(donor)]) == 0
    capsys.readouterr()
    originals = {p: p.read_bytes() for export in (parent, donor) for p in export.iterdir()}
    args = ["recover-discovery", str(parent), "--cache-dir", str(cache), "--max-gap-seconds", "120", "--out"]
    assert main([*args, str(child)]) == 0
    summary = json.loads(capsys.readouterr().out)
    receipt = summary["automatic_recovery"]
    assert receipt["parent_search_id"] == json.loads(originals[parent / "search.json"])["search_id"]
    assert receipt["donors_considered"] == 1 and receipt["request_count"] == 0 and receipt["lookup_at"] is None
    assert receipt["lanes"] == [{
        "lane": "epmc_broad", "match": "selected", "action": "reused", "eligible_donor_count": 1,
        "donor_search_id": json.loads(originals[donor / "search.json"])["search_id"],
    }]
    assert receipt == json.loads((child / "search.json").read_bytes())["automatic_recovery"]
    for destination in (parent, donor, child):
        assert main([*args, str(destination), "--fetch-missing"]) == 2
    assert all(p.read_bytes() == original for p, original in originals.items())
    assert len(epmc_timeout) == 4


def test_recover_cli_retains_or_fetches_missing_lane(tmp_path, epmc_timeout, capsys):
    parent, cache = tmp_path / "parent", tmp_path / "cache"
    assert main([*CLI_ARGS, "--out", str(parent)]) == 3
    cache.mkdir()
    capsys.readouterr()
    args = ["recover-discovery", str(parent), "--cache-dir", str(cache), "--max-gap-seconds", "0"]
    assert main([*args, "--out", str(tmp_path / "offline")]) == 3
    receipt = json.loads(capsys.readouterr().out)["automatic_recovery"]
    assert receipt["lanes"] == [{
        "lane": "epmc_broad", "match": "missing", "action": "retained", "eligible_donor_count": 0,
        "donor_search_id": None,
    }]
    assert receipt["request_count"] == 0 and receipt["fetch_missing"] is False and len(epmc_timeout) == 2
    assert main([*args, "--lookup-at", "2026-01-01T00:00:00Z", "--fetch-missing",
                 "--out", str(tmp_path / "retrospective")]) == 2
    assert len(epmc_timeout) == 2
    assert main([*args, "--lookup-at", "now", "--fetch-missing", "--out", str(tmp_path / "fetched")]) == 0
    receipt = json.loads(capsys.readouterr().out)["automatic_recovery"]
    assert receipt["lanes"][0]["action"] == "retried" and receipt["request_count"] == 1 and receipt["lookup_at"]
    assert len(epmc_timeout) == 3 and epmc_timeout[-1] == "www.ebi.ac.uk"


@pytest.mark.parametrize("invalid", ["cache_link", "parent_link", "child_link", "member_link", "missing_member",
                                     "tampered", "too_many_entries", "too_many_exports"])
def test_recover_cli_rejects_bad_cache_before_opt_in_fetch(tmp_path, epmc_timeout, capsys, invalid):
    import shutil

    parent, cache, out = (tmp_path / name for name in ("parent", "cache", "out"))
    assert main([*CLI_ARGS, "--out", str(parent)]) == 3
    cache.mkdir()
    if invalid in ("cache_link", "parent_link"):
        link = tmp_path / "linked"
        link.symlink_to(cache if invalid == "cache_link" else parent, target_is_directory=True)
        if invalid == "cache_link":
            cache = link
        else:
            parent = link
    elif invalid == "child_link":
        (cache / "linked").symlink_to(parent, target_is_directory=True)
    elif invalid == "too_many_entries":
        for i in range(1025):
            (cache / str(i)).touch()
    elif invalid == "too_many_exports":
        for i in range(257):
            shutil.copytree(parent, cache / str(i))
    else:
        captured = cache / "captured"
        shutil.copytree(parent, captured)
        member = captured / "candidates.tsv"
        member.unlink()
        if invalid == "member_link":
            member.symlink_to(parent / "candidates.tsv")
        elif invalid == "tampered":
            member.write_text("corrupt")
    assert main(["recover-discovery", str(parent), "--cache-dir", str(cache), "--max-gap-seconds", "10",
                 "--fetch-missing", "--out", str(out)]) == 2
    assert not out.exists() and len(epmc_timeout) == 2
    assert str(tmp_path) not in capsys.readouterr().err


def test_mcp_recover_uses_process_cache_and_explicit_fetch(epmc_timeout):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            parent = (await session.call_tool("discover_candidates", ARGUMENTS)).structuredContent
            parent_manifest = (await session.read_resource(parent["manifest_uri"])).contents
            args = {"search_id": parent["search_id"], "max_gap_seconds": 120}
            for bad in ({"search_id": "f" * 64}, {"cache_dir": "/private/operator"}, {"fetch_missing": "true"},
                        {"fetch_missing": 1}, {"max_gap_seconds": -1}, {"max_gap_seconds": True},
                        {"lookup_at": "2026-01-01T00:00:00+00:00", "fetch_missing": True}, {"lookup_at": 1}):
                assert (await session.call_tool("recover_discovery", {**args, **bad})).isError
            offline = await session.call_tool("recover_discovery", args)
            assert not offline.isError, offline.content
            receipt = offline.structuredContent["automatic_recovery"]
            assert receipt["lanes"][0]["action"] == "retained" and receipt["request_count"] == 0
            assert len(epmc_timeout) == 2
            live = await session.call_tool("recover_discovery", {**args, "fetch_missing": True, "lookup_at": "now"})
            assert not live.isError, live.content
            assert live.structuredContent["automatic_recovery"]["lanes"][0]["action"] == "retried"
            assert live.structuredContent["automatic_recovery"]["request_count"] == 1
            assert len(epmc_timeout) == 3
            assert (await session.read_resource(parent["manifest_uri"])).contents == parent_manifest

    asyncio.run(run())


def test_mcp_recover_selects_donor_by_cached_search_id(epmc_timeout):
    async def run():
        server = create_server(DossierService(), discovery=True)
        async with create_connected_server_and_client_session(server) as session:
            parent = (await session.call_tool("discover_candidates", ARGUMENTS)).structuredContent
            donor = (await session.call_tool("discover_candidates", ARGUMENTS)).structuredContent
            args = {"search_id": parent["search_id"], "max_gap_seconds": 120}
            result = await session.call_tool("recover_discovery", args)
            assert not result.isError, result.content
            child = result.structuredContent
            receipt = child["automatic_recovery"]
            assert receipt["request_count"] == 0 and receipt["donors_considered"] == 1
            assert receipt["lanes"][0]["donor_search_id"] == donor["search_id"]
            page = await session.call_tool("get_discovery_page", {"search_id": child["search_id"]})
            assert child["preview"] == page.structuredContent["rows"]
            manifest = (await session.read_resource(child["manifest_uri"])).contents[0].text
            assert json.loads(manifest)["automatic_recovery"] == receipt and len(epmc_timeout) == 4

    asyncio.run(run())


def test_recover_cli_output_reservation_cannot_create_missing_cache(tmp_path, epmc_timeout):
    parent, missing_cache = tmp_path / "parent", tmp_path / "new"
    assert main([*CLI_ARGS, "--out", str(parent)]) == 3
    assert main(["recover-discovery", str(parent), "--cache-dir", str(missing_cache), "--max-gap-seconds", "60",
                 "--fetch-missing", "--out", str(missing_cache)]) == 2
    assert not missing_cache.exists() and len(epmc_timeout) == 2
