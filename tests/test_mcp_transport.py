"""Check error boundaries and input validation before any service work starts."""
import asyncio
import json
from pathlib import Path
from queue import Queue
import shutil
from threading import Event
from unittest.mock import Mock

import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from medextract_dossier.mcp_server import create_server
from medextract_dossier.service import DossierService, ServiceError


@pytest.mark.parametrize("name,arguments", [
    ("evidence_dossier", {"example_dir": "/operator/input"}),
    ("evidence_dossier", {"without_passages": '["cohort-result"]'}),
    ("export_dossier", {"review_id": "a" * 64, "private_key": "unexpected-key-material"}),
    ("verify_dossier", {"bundle_id": "../other"}),
])
def test_invalid_arguments_do_not_reach_service(name, arguments):
    service = Mock()
    server = create_server(service)
    with pytest.raises(ToolError, match="Invalid arguments") as exc:
        asyncio.run(server.call_tool(name, arguments))
    assert service.mock_calls == []
    for value in arguments.values():
        assert value not in str(exc.value)


def test_only_service_errors_disclose_their_message():
    service = Mock()
    server = create_server(service)
    service.build.side_effect = ServiceError("Rebuild this review.")
    with pytest.raises(ToolError, match="Rebuild this review"):
        asyncio.run(server.call_tool("evidence_dossier", {}))

    service.build.side_effect = OSError("unavailable /operator/private/input")
    service.read_review.side_effect = service.build.side_effect
    for call in (server.call_tool("evidence_dossier", {}),
                 server.read_resource("dossier://reviews/" + "a" * 64)):
        with pytest.raises((ToolError, ValueError), match="local dossier operation") as exc:
            asyncio.run(call)
        assert "/operator" not in str(exc.value)


def test_large_valid_casebook_keeps_full_question_text_in_resource(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "marker-mx1"
    directory = shutil.copytree(example, tmp_path / "example")
    casebook = directory / "casebook.json"
    book = json.loads(casebook.read_bytes())
    question = book["questions"][0]
    question["text"] += " Additional synthetic context." * 34_000
    casebook.write_text(json.dumps(book), encoding="utf-8")
    assert 950_000 < casebook.stat().st_size < 1_048_576

    async def run():
        server = create_server(DossierService(example=directory))
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("evidence_dossier", {})
            assert not result.isError
            payload = result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
            assert len(payload) <= 4096
            summary = result.structuredContent
            assert json.loads(result.content[0].text) == summary
            assert [row["question_id"] for row in summary["decisions"]] == ["prognosis", "intervention"]
            resource = await session.read_resource(summary["review_uri"])
            assert json.loads(resource.contents[0].text)["decisions"][0]["question_text"] == question["text"]

    asyncio.run(run())


def test_client_can_cancel_an_active_request_and_call_again():
    service = Mock()
    server = create_server(service)
    request_ids = Queue()
    release = Event()
    digest = "a" * 64
    review = {"schema_version": 1, "review_id": digest, "review_uri": f"dossier://reviews/{digest}",
              "evidence_origin": "synthetic", "decisions": []}

    def blocked_review(*args):
        request_ids.put(server.get_context().request_context.request_id)
        assert release.wait(timeout=10)
        return review

    async def run():
        async with create_connected_server_and_client_session(server) as session:
            service.build.side_effect = blocked_review
            pending = asyncio.create_task(session.call_tool("evidence_dossier", {}))
            try:
                request_id = await asyncio.to_thread(request_ids.get, True, 5)
                await session.send_notification(types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=request_id))))
                with pytest.raises(McpError, match="(?i)cancel"):
                    await asyncio.wait_for(pending, timeout=5)
            finally:
                release.set()
            service.build.side_effect = None
            service.build.return_value = review
            recovered = await session.call_tool("evidence_dossier", {})
            assert not recovered.isError
            assert recovered.structuredContent == review

    asyncio.run(run())
