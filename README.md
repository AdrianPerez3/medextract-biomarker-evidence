# MedExtract Evidence Dossier — offline evidence review for Python and MCP

Trace a research conclusion to the exact passage that supports it. MedExtract Evidence Dossier is an offline **Python CLI and Model Context Protocol (MCP) server** for reviewing pre-annotated synthetic research evidence. It records why each passage was included or excluded. A recipient can verify a signed review and reproduce its decisions from the same source bytes.

The included **MX-1 casebook** demonstrates a common evidence-review problem. A cohort found that people with a higher baseline marker had more task failures. A separate trial found that a program lowered the marker, but its effect on task failures remained uncertain. The tool shows which study answers each question. [Read the cohort](examples/marker-mx1/sources/cohort.txt) · [Read the trial](examples/marker-mx1/sources/trial.txt)

## Quick start for offline evidence review

From the repository directory on macOS or Linux, run the example with Python 3.11 or later. This command reads the bundled files and prints the review. It needs no package installation, API key, model download or network connection.

```sh
python3 -m medextract_dossier build
```

| Question about the fictional studies | Result reported in the source | CLI decision |
| --- | --- | --- |
| Was higher baseline MX-1 associated with a first task failure within 12 months? | 48 of 200 versus 24 of 200 participants. Risk ratio **2.00**, 95% CI **1.28–3.13**. | `association_observed` |
| Did assignment to program K change the risk of the same outcome? | 18 of 150 versus 20 of 150 participants. Risk ratio **0.90**, 95% CI **0.50–1.63**. The interval includes both lower and higher risk. | `effect_uncertain` |

To see which answer depends on the cohort, omit its decisive passage and request JSON.

```sh
python3 -m medextract_dossier build --without-passage cohort-result --json
```

Prognosis changes to `insufficient` with reason `no_direct_result`. The intervention decision stays `effect_uncertain`. To inspect a citation yourself, find `cohort-result` in the **baseline** output. Its quote occupies UTF-8 bytes 385 through 705 of `sources/cohort.txt`; the recorded end offset is 706. [Expected decisions](examples/marker-mx1/expected.json) · [Annotated casebook](examples/marker-mx1/casebook.json)

## Inputs, outputs and evidence provenance

| For this task | Use this file or interface |
| --- | --- |
| Prepare an input | A synthetic `casebook.json` and UTF-8 source files. The casebook supplies the questions, study families, passage annotations, estimates and source digests. [Casebook JSON Schema](schemas/v1/casebook.schema.json) |
| Parse a review | `build --json` returns decisions, reasons, eligible study families, and a ledger of quoted passages with source paths, byte offsets and SHA-256 digests. [Comparison JSON Schema](schemas/v1/comparison.schema.json) · [Decision JSON Schema](schemas/v1/decision.schema.json) |
| Deliver a reproducible review | Freeze the review, export an Ed25519-signed bundle, then verify its signature, file integrity and decision replay using an independently obtained public key. [Signed review walkthrough](docs/operator-guide.md#sign-and-verify-a-review) |
| Audit the example | Three fictional study texts, including a protocol with no observed result, plus the casebook, expected answers and provenance note. The note documents how the numerical examples were constructed. [Example provenance and license](examples/marker-mx1/PROVENANCE.md) |

Version 1 supports prognosis and intervention questions. It checks reviewer-supplied annotations against the question and verifies the quoted source bytes. Reports from a single study family count once. When more than one family supplies direct evidence, the rules return `insufficient` with reason `multiple_families_require_review` and leave synthesis to a reviewer. The example contains no patient records. Accuracy on published studies and suitability for clinical decisions have not been evaluated.

## Install the CLI or connect an MCP client

To use the commands outside the checkout, create a virtual environment in the repository directory and install the features you need.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
medextract-dossier build --json
```

For signing and verification, install `python -m pip install '.[handoff]'` in that environment. For an AI client, install `python -m pip install '.[mcp]'`, then configure the client to launch the absolute path to `.venv/bin/medextract-dossier-mcp` over standard input and output. The MCP extra includes signing and verification. [Client configuration, tool arguments and operator limits](docs/operator-guide.md#connect-an-mcp-client)

| MCP tool | What the client can do |
| --- | --- |
| `evidence_dossier` | Review the configured synthetic example. Read the returned `review_uri` for the full passage ledger. |
| `export_dossier` | Sign the review named by `review_id` when the operator has configured signing. |
| `verify_dossier` | Verify a configured or exported bundle with the trusted public key. Read `receipt_uri` for the per-file results. |

Try this request in your MCP client.

> Run the MX-1 review. Which passage supports the prognosis result? Give its filename and byte range. Omit that passage and show me which decision changes.

## Testing and citation

The test suite covers the decision rules, quote locations, signed bundles, package contents and a client connected to the MCP server. To run it in the active environment, follow the contribution guide. Use the citation file to credit the version you used. [Run the tests](CONTRIBUTING.md) · [Cite the software](CITATION.cff) · [Report a security issue](SECURITY.md)

## License and attribution

The software, schemas, tests and project documentation use the **MIT License**. The six files under `examples/marker-mx1/` use **CC BY 4.0**, including the copy bundled with the Python package. Both credit **MedExtract Evidence Dossier contributors**. When redistributing the example, include the attribution, link the license and indicate any changes. [MIT License](LICENSE) · [CC BY 4.0 license](LICENSES/CC-BY-4.0.txt) · [Example credit](examples/marker-mx1/PROVENANCE.md)
