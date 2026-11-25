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

## Interpreting the passage ledger

Quote ranges use zero-based UTF-8 byte offsets with an exclusive end. The bundled sources use LF line endings so offsets and digests survive checkout. Each v1 source belongs to one study family. Byte-identical copies must share a family, even under different filenames or citation IDs. Detecting overlap between different reports and checking annotations against their source requires a reviewer.

A protocol objective receives `no_result`. A result whose estimate was never captured receives `estimate_missing`. The verifier can also replay early v1 handoffs that recorded `no_result` for both. The JSON Schemas ship inside the wheel under `medextract_dossier/schemas/v1`. [Repository schemas](../schemas/v1/) · [Example expectations](../examples/marker-mx1/expected.json)
