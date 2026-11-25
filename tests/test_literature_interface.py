"""Exercise the public literature interfaces with deterministic, offline provider data."""
import asyncio
from io import BytesIO
import json

import pytest


def test_provider_transport_has_fixed_destinations_and_size_limit(monkeypatch):
    from medextract_dossier import literature_http

    with pytest.raises(ValueError):
        literature_http.fetch_bytes("https://example.org/not-a-provider")
    with pytest.raises(ValueError):
        literature_http.fetch_bytes("http://www.ebi.ac.uk/europepmc/webservices/rest/search")

    class Reply(BytesIO):
        headers = {}

    monkeypatch.setattr(literature_http._OPENER, "open", lambda *args, **kwargs: Reply(b"1234"))
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    assert literature_http.fetch_bytes(url, max_bytes=4) == b"1234"
    with pytest.raises(ValueError, match="size"):
        literature_http.fetch_bytes(url, max_bytes=3)


def test_literature_cli_preserves_question_and_actual_search_terms(monkeypatch, capsys):
    from medextract_dossier.__main__ import main
    from medextract_dossier import literature_search

    def search(question, search_terms, limit):
        assert (question, search_terms, limit) == ("What was studied?", "biomarker cohort", 2)
        return {"records": [{"title": "Example returned by provider"}], "status": "partial"}

    monkeypatch.setattr(literature_search, "search_literature", search)
    assert main(["research", "What was studied?", "--terms", "biomarker cohort", "--limit", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_live_tools_require_operator_mode_and_advertise_remote_access(monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    from medextract_dossier import literature_search, paper_text
    from medextract_dossier.mcp_server import create_server
    from medextract_dossier.service import DossierService

    def search(question, search_terms, limit):
        assert (question, search_terms, limit) == ("Which studies?", "marker", 2)
        return {"records": [{"pmcid": "PMC123"}], "status": "partial"}

    def read(pmcid, terms, limit):
        assert (pmcid, terms, limit) == ("PMC123", "marker", 1)
        return {"pmcid": pmcid, "status": "available", "passages": [{"text": "Located passage"}]}

    monkeypatch.setattr(literature_search, "search_literature", search)
    monkeypatch.setattr(paper_text, "read_paper", read)

    async def run():
        for online in (False, True):
            server = create_server(DossierService(), literature=online)
            async with create_connected_server_and_client_session(server) as session:
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                if not online:
                    assert "research_biomarker" not in tools and "read_paper" not in tools
                    continue
                for name in ("research_biomarker", "read_paper"):
                    assert tools[name].annotations.openWorldHint and tools[name].annotations.readOnlyHint
                result = await session.call_tool("research_biomarker", {
                    "question": "Which studies?", "search_terms": "marker", "limit": 2})
                assert not result.isError and result.structuredContent["records"][0]["pmcid"] == "PMC123"
                result = await session.call_tool("read_paper", {"pmcid": "PMC123", "terms": "marker", "limit": 1})
                assert not result.isError and result.structuredContent["passages"][0]["text"] == "Located passage"

    asyncio.run(run())


def test_transport_failure_reports_outage_without_operator_details():
    from mcp.server.fastmcp.exceptions import ToolError
    from medextract_dossier.mcp_server import _literature_call

    def offline():
        raise OSError("private proxy configuration")

    with pytest.raises(ToolError, match="provider could not be reached") as caught:
        asyncio.run(_literature_call(offline))
    assert "proxy" not in str(caught.value)
