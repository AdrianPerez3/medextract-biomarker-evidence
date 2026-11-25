"""Expose the offline dossier service using the optional official MCP SDK."""
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from anyio import to_thread
from anyio.lowlevel import checkpoint_if_cancelled
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from .service import BUNDLE_ID_PATTERN, DossierService, ServiceError


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BundleId = Annotated[str, Field(pattern=BUNDLE_ID_PATTERN)]
ReviewUri = Annotated[str, Field(pattern=r"^dossier://reviews/[0-9a-f]{64}$")]
ReceiptUri = Annotated[str, Field(pattern=r"^dossier://receipts/[0-9a-f]{64}$")]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class DecisionSummary(_Output):
    question_id: str
    status: str
    reason: str
    eligible_study_count: Annotated[int, Field(ge=0)]


class ReviewSummary(_Output):
    schema_version: Literal[1]
    evidence_origin: Literal["synthetic"]
    review_id: Digest
    review_uri: ReviewUri
    decisions: list[DecisionSummary]


class ExportSummary(_Output):
    schema_version: Literal[1]
    review_id: Digest
    bundle_id: BundleId
    review_uri: ReviewUri


class VerificationSummary(_Output):
    schema_version: Literal[1]
    bundle_id: BundleId
    verified: bool
    signature: Literal["valid", "invalid", "not_checked"]
    integrity: Literal["match", "mismatch", "not_checked"]
    replay: Literal["match", "mismatch", "not_checked"]
    receipt_id: Digest
    receipt_uri: ReceiptUri


class _DossierTool(Tool):
    """Validate the advertised JSON before FastMCP's legacy argument coercion."""

    async def run(self, arguments, context=None, convert_result=False):
        if not Draft202012Validator(self.parameters).is_valid(arguments):
            raise ToolError(f"Invalid arguments for {self.name}. See the tool input schema.")
        return await super().run(arguments, context=context, convert_result=convert_result)


def _tool(fn, *, read_only: bool) -> Tool:
    annotations = ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=False)
    tool = _DossierTool.from_function(fn, annotations=annotations, structured_output=True)
    tool.parameters["additionalProperties"] = False
    return tool


async def _call(method, *args):
    # Only the service's public errors may expose their message to an MCP client.
    try:
        return await to_thread.run_sync(method, *args)
    except ServiceError:
        raise
    except Exception:
        raise RuntimeError("Could not complete the local dossier operation.") from None
    finally:
        # Let a started filesystem operation finish. If the SDK already answered
        # a cancellation, yield here before returning a second response.
        await checkpoint_if_cancelled()


def create_server(service: DossierService) -> FastMCP:
    async def evidence_dossier(
        example_id: Literal["marker-mx1"] = "marker-mx1",
        without_passages: Annotated[list[str] | None, Field(
            description="Passage IDs to omit from both questions in this review.")] = None,
    ) -> ReviewSummary:
        """Read the configured synthetic example offline and review both questions.

        The result gives each decision, its reason and its eligible study count.
        Read review_uri for the ledger. The server retains the review in memory.
        """
        return ReviewSummary.model_validate(await _call(service.build, example_id, without_passages))

    async def export_dossier(review_id: Digest) -> ExportSummary:
        """Sign a cached review offline with the configured private key and write its
        bundle to the configured output directory. Repeating an identical export
        returns the same bundle ID.
        """
        return ExportSummary.model_validate(await _call(service.export, review_id))

    async def verify_dossier(bundle_id: BundleId) -> VerificationSummary:
        """Read a registered or exported bundle and verify it offline with the configured
        trusted public key. The receipt records signature, file integrity and decision
        replay. Read receipt_uri for details. Verification writes no files.
        """
        return VerificationSummary.model_validate(await _call(service.verify, bundle_id))

    server = FastMCP(
        "MedExtract Evidence Dossier",
        tools=[_tool(evidence_dossier, read_only=True), _tool(export_dossier, read_only=False),
               _tool(verify_dossier, read_only=True)],
    )

    @server.resource("dossier://reviews/{review_id}", mime_type="application/json",
                     description="Full comparison with the question decisions and passage ledgers.")
    async def review(review_id: str) -> str:
        return await _call(service.read_review, review_id)

    @server.resource("dossier://receipts/{receipt_id}", mime_type="application/json",
                     description="Full verification receipt with fingerprints and per-file results.")
    async def receipt(receipt_id: str) -> str:
        return await _call(service.read_receipt, receipt_id)

    return server


def run_server(service: DossierService) -> None:
    create_server(service).run(transport="stdio")
