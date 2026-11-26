# Operating MedExtract Evidence Dossier

Use the literature tools for discovery and passage retrieval. Use the offline review tools for a casebook whose sources and annotations a reviewer has checked. The two operations are distinct: rerunning a live query can retrieve different publications; replaying a signed review checks the frozen source bytes. [Biomarker research quick start](../README.md#ask-a-biomarker-question-from-an-mcp-client)

## From published paper to reviewed casebook

1. Save the intended question, search expression and provider results from `research`. Check truncation, missing providers, study design and publication notices. One study may have more than one DOI or report.
2. Use `read-paper` to inspect supported openly licensed text, or review a local UTF-8 text source that you have permission to use. The paper reader's locations are character offsets in a whitespace-normalized JATS paragraph. The casebook's locations are byte offsets into the exact local source text. Compute the byte range from the saved local bytes.
3. Declare `evidence_origin: published`. Give each source its bibliographic `citation`, absolute HTTPS `url`, declared `license`, relative text file, SHA-256 and study-family ID. In the passage annotation, encode the population, comparison, follow-up, outcome and reported estimate. Mark it reviewed only after checking these against the publication. The rules currently interpret eligible risk ratios with intervals. Hazard ratios and odds ratios must retain their meaning; they cannot be entered as risk ratios.
4. Build with the explicit casebook path. The source metadata then appears in the passage ledger and signed handoff. Reading a local casebook accepts declared source terms. Signed export currently supports `CC0-1.0`, `CC-BY-3.0` and `CC-BY-4.0`; retain the attribution and verify the terms cover each included passage. [Worked published casebook](../examples/crp-saa-trial/casebook.json) · [Input schema](../schemas/v1/casebook.schema.json)

## Sign and verify a review

From a checkout with the virtual environment active, install `python -m pip install '.[handoff]'`. The following example uses the packaged casebook. After installation you can run these commands in a separate working directory.

**1. Review and freeze the source bytes.** This uses the included published trial. To freeze your own annotated study, add its casebook directory after `build`.

```sh
medextract-dossier build --review-store work/reviews --json > work-reviewed.json
review_id=$(python -c "import json; print(json.load(open('work-reviewed.json'))['review_id'])")
```

Inspect the `comparison` in `work-reviewed.json` before signing. The review ID binds the validated casebook, source bytes, decisions and omissions. Stored export checks that ID and works even if the original input changes or moves. Keep the review store private.

**2. Create the producer key and export the frozen review.**

```sh
key_dir="$HOME/.local/state/medextract-dossier"
medextract-dossier keygen --private-key "$key_dir/producer.pem" --public-key "$key_dir/producer.pub.pem"
medextract-dossier export --review-store work/reviews --review-id "$review_id" \
  --out handoff/reviewed --private-key "$key_dir/producer.pem"
```

Keep the private key outside the inputs and exports. Repeating the same export to the same destination succeeds. Select a fresh destination for different content. For an immediate export of the current files, supply the example directory explicitly, e.g. `medextract-dossier export examples/marker-mx1 --out handoff/baseline --private-key "$key_dir/producer.pem"` from the repository root.

**3. Verify as the recipient.** Transfer the complete signed directory, preserving file bytes. Supply the public key separately or verify its fingerprint over a trusted channel. Include the required source attribution. [Published trial credit](../examples/crp-saa-trial/PROVENANCE.md) · [MX-1 fixture credit](../examples/marker-mx1/PROVENANCE.md)

```sh
medextract-dossier verify received/reviewed --public-key trusted/producer.pub.pem --json
```

Replace the two paths with the received directory and the independently trusted public key. Verification works offline using those files alone after installation. A successful receipt reports `signature: valid`, `integrity: match` and `replay: match`. After verification, open `report.html` in the signed directory to read the decision and its cited passages. Omit `--json` for a text receipt.

| Verification outcome | CLI exit | How to interpret it |
| --- | --- | --- |
| Signature, contents and replay verified | 0 | The supplied public key authenticates the manifest, the listed files match and the rules reproduce the recorded decisions. |
| Completed negative check | 1 | Inspect the receipt for a different key, changed or unexpected member, or a replay disagreement. |
| Setup or usage error | 2 | Check the command, bundle directory and key. For verification setup errors, the JSON receipt gives `error_kind: configuration` and `signature: not_checked`. |

The verifier authenticates the manifest independently of checking individual files. A changed source can therefore appear alongside a valid manifest signature. Replay waits for a complete matching inventory. If the supplied key differs from the claimed signer, the receipt includes both fingerprints. The fingerprint is the SHA-256 of the raw Ed25519 public key. Establishing who controls that key depends on the independent exchange.

## Signed bundle contents

| Member | Purpose |
| --- | --- |
| `question.json` | Question scope, contrast, outcome and time horizon. |
| `evidence.json` and `sources/*.txt` | Annotations, study families, original source bytes, quotations and UTF-8 byte ranges. |
| `decision.json` | The recorded comparison, exclusion reasons and intentionally omitted passage IDs. The verifier reruns the v1 rules on the structured inputs. |
| `report.html` | The human-readable report, verified byte for byte. |
| `manifest.dsse.json` | One Ed25519 signature over a typed DSSE payload. The manifest lists every other member once with its relative path, length in bytes and SHA-256 digest. |

The v1 media type is `application/vnd.medextract.evidence-dossier.manifest.v1+json`. [DSSE protocol](https://github.com/dsse-lf/dsse/blob/master/protocol.md)

## Connect an MCP client

From the repository directory with the virtual environment active, install `python -m pip install '.[mcp]'`. For a client that accepts `mcpServers`, use this configuration and replace the command with the absolute path to your installed executable.

```json
{
  "mcpServers": {
    "medextract-dossier": {
      "command": "/absolute/path/to/.venv/bin/medextract-dossier-mcp",
      "args": []
    }
  }
}
```

| Tool | Example arguments | Response |
| --- | --- | --- |
| `evidence_dossier` | `{}` for baseline or `{"without_passages":["index-antibiotic-prescribing"]}` for the published example | A decision, reason and eligible study-family count per question, with a shared `review_id` and `review_uri`. |
| `export_dossier` | `{"review_id":"<returned review_id>"}` | A signed directory under the configured output directory and its `bundle_id`. Export uses the bytes captured during review. |
| `verify_dossier` | `{"bundle_id":"received"}` for the recipient below, or use a returned export ID | Signature, integrity and replay results, plus a `receipt_uri` for file checks and signer fingerprints. |

Each tool returns a structured summary and a JSON text copy. Use MCP `resources/read` on the returned URI to fetch the full JSON. An `insufficient` decision is a successful review. A changed file or a different valid key produces a completed verification with `verified: false`. An unknown ID, unavailable directory or missing key configuration produces a tool error. A detailed receipt includes the manifest digest when the signature verifies and the reproduced `review_id` when replay succeeds. [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [MCP resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)

For a version-specific citation or bug report, run `python -m pip show medextract-evidence-dossier` in the server's installed environment. In the tested MCP SDKs the handshake reports the SDK version.

**Producer configuration.** Set `--output-dir /absolute/path/to/handoffs` and `--private-key /absolute/path/to/operator/producer.pem` in the server's startup arguments to enable signing. Add `--public-key /absolute/path/to/trusted/producer.pub.pem` to verify local exports. A retry with the same review and signing key reuses the identical bundle. An export that has already started writing can finish if the client cancels.

**Recipient configuration.** Use these startup arguments to review a signed bundle received from another operator. Obtain the public key independently. The client selects the bundle by its configured name.

```json
["--no-example", "--bundle", "received=/absolute/path/to/received/reviewed",
 "--public-key", "/absolute/path/to/trusted/producer.pub.pem"]
```

**Retention and limits.** The server keeps the 16 most recently used reviews and 16 receipts in memory. After eviction or restart, rebuild a review or verify a bundle again to recover the corresponding record. Signed directories persist. The adapter accepts up to 1 MiB of casebook and source bytes and a 4 MiB serialized comparison. MCP export and verification share an 8 MiB bundle limit and a 256-entry inventory limit. Use portable ASCII source filenames for signing. Local reviews also accept Unicode filenames. Run `medextract-dossier-mcp --help` to see the startup arguments. [Security and key handling](../SECURITY.md)

## Screen a discovery snapshot

Start the local server with `medextract-dossier-mcp --discovery`. For an example input to `discover_candidates`, use `{"question":"Which published studies examine cystatin C in primary care?","concept_groups":[["cystatin C"],["primary care","general practice"]]}`. Only the concept groups go into the search expression. Put synonyms in the same group. An optional `until_year` limits PubMed by publication date and Europe PMC by first publication date.

1. Inspect `status`, `candidate_count` and `lanes`. A completed search can return zero records or reach its depth cap. A partial search retains the available records. An unavailable search has a manifest describing the outage. Individual totals overlap across providers.
2. Inspect the ten-row preview. Pass its `search_id` and `next_offset` to `get_discovery_page`. Continue until `next_offset` is null. For the original PubMed order, start at offset zero with `view="pubmed_broad"`. The other native views are `epmc_broad`, `pubmed_anchored` and `epmc_anchored`. Offsets refer to the selected view. Reading another page makes zero provider requests.
3. Save the resources named by `candidates_uri` and `manifest_uri` before restarting the server. The local server retains the 16 most recently used discovery snapshots. The CLI writes the same two files to a new directory specified by `--out`. Exit 0 means all requested lanes completed, 3 indicates a partial or unavailable capture, and 2 indicates an input or local output error. Use a fresh directory for a new live capture.
4. Open the paper link, check its population, design, comparator and outcome, and record your eligibility decision. Native ranks indicate where an index returned a record. Multiple records can describe the same study.

The default `pmid_indexed` profile interleaves PubMed and Europe PMC MED records by native rank, PubMed first on ties. The optional title-focused branch appends newly found identifiers after the broad results. The `all_indexed` profile includes unrestricted Europe PMC sources, adds identifier lookups for PubMed hits, and exports a versioned table with snapshot-local row IDs. Inspect `identity_status` and `native_positions` when identifiers conflict.

To check an exported TSV offline, run this inside its directory. A missing file, invalid manifest or mismatched digest produces a nonzero exit status.

```sh
python3 - <<'PY'
from hashlib import sha256
import json
from pathlib import Path
manifest = json.loads(Path("search.json").read_bytes())
actual = sha256(Path("candidates.tsv").read_bytes()).hexdigest()
if actual != manifest["candidates_sha256"]:
    raise SystemExit("The TSV differs from the search manifest")
print("TSV digest verified")
PY
```

## Recover a timed-out search

Open `search.json` and look for a provider and branch whose status is `unavailable` and whose sole warning is `timeout`. If PubMed completed and Europe PMC timed out, run the following command on the directory containing the original `search.json` and `candidates.tsv`.

```sh
medextract-dossier retry-discovery work/kidney-screen --out work/kidney-screen-retry
```

Recovery checks the source files, repeats each timed-out search branch once with the original concepts, profile, date and depth, and writes a new snapshot. A completed empty response is a valid search outcome. The original export remains available for comparison. In an MCP client, call `retry_discovery` with the cached parent’s `search_id` and open the returned manifest. After a server restart, use the CLI with the saved files.

Read the new manifest’s `recovery` entry for its parent search ID, the branches retried and the candidate counts. `request_count` counts provider search calls and any PubMed metadata batches for this attempt. `elapsed_seconds` measures the time spent waiting for a concurrency slot and collecting those responses. For `all_indexed`, `candidate_count_added` counts rows supplied entirely by the retried branches. A new identifier can also join or split existing rows, so the change in table size can differ from this count.

Check each branch’s `captured_at` when reporting results because the combined table may contain observations from two dates. A repeated timeout produces its own receipt and the CLI exits with status 3. Exit status 2 indicates an invalid request, an ineligible source or a local export error.

If a provider responds with an HTTP error, check the affected search or metadata receipt for `http_status`. For example, a 429 is recorded with the warning `rate_limited`. Older receipts may record only `http_error`. Recovery eligibility applies to search timeouts only.

Older `pmid_indexed` exports can be retried from their saved files. Recovery of an `all_indexed` export requires the individual identifier claims saved by a recovery-capable version. For older wide exports that lack these claims, start a new discovery capture in a fresh directory.

## Reuse a saved provider response

Suppose Europe PMC timed out for one question, but another search used the same Europe PMC query and completed. Give the CLI the first export as the parent and the second as the donor. Choose the largest time gap your review protocol allows.

```sh
medextract-dossier reuse-discovery work/kidney-screen \
  --from work/saved-searches/related-screen --max-gap-seconds 60 --out work/kidney-screen-reused
```

The search profile, provider, branch and effective request parameters must match, including the search expression, date filter and page depth. The questions may differ. Each eligible parent branch must have a recorded timeout. Its donor branch must have a successful response with a capture time and response hash. A completed zero-hit search qualifies.

The window is inclusive in either time direction, measured between the two recorded request start times. A value of zero requires identical times. If your protocol forbids later evidence, select a donor whose matching captures precede the parent timeouts. The command processes only the two exports you name and makes zero network requests. It saves a child when at least one branch matches. Any remaining timeouts retain their original receipts. The CLI exits with 0 for a complete child, 3 for a partial child and 2 if no branch matches or the input or output is invalid.

In MCP, call `reuse_discovery` with the cached `search_id`, `donor_search_id` and integer `max_gap_seconds`. With the same inputs and window, the CLI and MCP produce the same child search ID. Save the parent, donor and child together. The child’s `reuse` entry identifies both source snapshots, the branches borrowed and the number of added candidates. The candidate table preserves the donor’s original ranks, and the lane receipt preserves its capture time and response digest. Screen the candidates against the parent question.

For `all_indexed`, both exports must include `native_hits`, the individual identifiers supplied at each provider rank. Older wide exports may lack them. A wide capture with no timed-out branches can also omit them to fit the 2 MiB export limit. For a borrowed PubMed branch, the donor manifest holds its original identifier-lookup receipts. A single lookup can cover several branches.

## Find a saved response before retrying

Put each completed discovery export in its own immediate subdirectory of an operator-controlled cache directory. Each export consists of `search.json` and `candidates.tsv`. Supply the original timed-out search separately. Use a new output path outside the cache.

```sh
medextract-dossier recover-discovery work/kidney-screen \
  --cache-dir work/saved-searches --max-gap-seconds 60 --out work/kidney-screen-checked
```

By default the lookup runs offline. It checks each branch that timed out for a successful saved response with the same profile, provider, branch and effective request parameters. The 60-second window in this example extends in both directions from the original branch’s request start time. Choose the window for your use case. The CLI checks up to 256 exports and 1,024 immediate directory entries. An incomplete export, a failed integrity check, a symlink in the cache or an exceeded limit produces an input error.

To resume a search later, measure donor age from the start of the new lookup. This example checks the preceding 60 seconds and asks the provider once for each timed-out branch that still needs a response. Wide-profile PubMed results can require additional identifier lookups. Inspect the per-branch outcome to see whether a retry succeeded.

```sh
medextract-dossier recover-discovery work/kidney-screen \
  --cache-dir work/saved-searches --max-gap-seconds 60 --lookup-at now \
  --fetch-missing --out work/kidney-screen-resumed
```

For offline replay, replace `now` with a timezone-aware timestamp and omit `--fetch-missing`. A fixed time constrains the donor capture and applicable identifier lookups. For a historical comparison, supply the parent and only those donor exports that were already complete at the decision time. A recorded request start time alone cannot establish when its response became available.

Eligible donors must agree on the total and the ordered native results, including applicable identifiers. If they agree, the closest capture wins, with search ID breaking a tie. If they disagree, the result is marked `ambiguous`. With no eligible donor it is marked `missing`. Both outcomes preserve the timeout during an offline run. A completed search with zero hits can supply a valid response.

Open `search.json` and read `automatic_recovery` for the time window, lookup time, selected donor ID per branch, candidate counts and number of fresh requests. Save each selected donor alongside the parent and child to audit its timestamps, response hash and any identifier-lookup receipts. A complete result exits with 0, a partial result or one still unavailable with 3, and an input or output error with 2.

In MCP, use `recover_discovery(search_id, max_gap_seconds, fetch_missing=False, lookup_at=None)` with discovery mode enabled. It searches other snapshots in the server’s 16-entry in-memory cache. Save CLI exports for recovery across server restarts.

## Interpreting the passage ledger

Quote ranges use zero-based UTF-8 byte offsets with an exclusive end. The bundled sources use LF line endings so offsets and digests survive checkout. Each v1 source belongs to one study family. Byte-identical copies must share a family, even under different filenames or citation IDs. Detecting overlap between different reports and checking annotations against their source requires a reviewer.

A protocol objective receives `no_result`. A result whose estimate was never captured receives `estimate_missing`. The verifier can also replay early v1 handoffs that recorded `no_result` for both. The JSON Schemas ship inside the wheel under `medextract_dossier/schemas/v1`. [Repository schemas](../schemas/v1/) · [Example expectations](../examples/marker-mx1/expected.json)
