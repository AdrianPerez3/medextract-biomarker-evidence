# MedExtract Biomarker Evidence

Search **PubMed and Europe PMC** for studies that address a biomarker question, then trace findings to passages in openly licensed articles. MedExtract Biomarker Evidence provides a Python CLI and a Model Context Protocol (MCP) server for this workflow. Reviewers can save an annotated set of studies, sign their review, and share it for independent verification offline.

This is a research preview. The live tools retrieve records and passages. A researcher or the calling AI client must assess whether the study design, population and outcome answer the question. Scientific accuracy on a held-out literature set and clinical use have not been validated.

## Ask a biomarker question from an MCP client

From a copy of the repository on macOS or Linux, install the server with Python 3.11 or later. The Python distribution is named `medextract-evidence-dossier`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[mcp]'
```

Configure the client to start the installed executable over standard input and output. Replace the example path with the absolute path to your environment. The `--literature` option enables the two online tools; it starts without a demonstration casebook.

```json
{
  "mcpServers": {
    "medextract-dossier": {
      "command": "/absolute/path/to/.venv/bin/medextract-dossier-mcp",
      "args": ["--literature"]
    }
  }
}
```

Try this in the client.

> What published studies compared cystatin C and creatinine for estimating kidney function? Search both indexes, identify overlapping citations, and read the methods and results of relevant papers you can access. Give the paper and section for each finding. Tell me which papers you could only identify by citation.

| Tool | What happens |
| --- | --- |
| `research_biomarker` | The server sends the supplied search terms to PubMed and Europe PMC. If terms are omitted, it sends the question. The response includes citations, available identifiers, the search date, query and provider outcomes. The two indexes can describe the same paper. |
| `read_paper` | Given a PubMed Central ID and terms, the server requests article XML from Europe PMC, with NCBI PubMed Central as a fallback for missing text or temporary outages. It searches the text locally, reports which provider supplied it, and returns each match with a source locator. The reader currently accepts explicit CC BY 4.0 or CC0 1.0 article licenses. |

Only send public research terms and paper identifiers. Article text is external content; use it as quoted evidence. The search tools do not need an API key. [PubMed E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25499/) · [Europe PMC API](https://europepmc.org/RestfulWebService)

## Run the same search from the CLI

For screening beyond the first results, start the MCP server with `--discovery`. Use `discover_candidates` to collect papers and `get_discovery_page` to screen them. After a timeout, `recover_discovery` checks the server’s saved searches for a matching provider response. You can enable both `--literature` and `--discovery` together. The Python CLI can also save the full screening table without installing the MCP extra.

```sh
mkdir -p work/saved-searches
python3 -m medextract_dossier discover \
  --question "Which studies examine cystatin C in primary care?" \
  --concept "cystatin C" --concept "primary care" "general practice" \
  --out work/kidney-screen
```

Open `work/kidney-screen/candidates.tsv` to follow the paper links and inspect their original ranks. Check `work/kidney-screen/search.json` for the expressions, publication dates, provider outcomes and truncation. The default searches up to 100 records per provider and returns a ten-row preview. Use `--include-title-search` to add a second search anchored on the first concept. The default profile screens records with PubMed identifiers. The `--profile all_indexed` option also preserves other Europe PMC identifiers and unresolved records. [Screening, paging and export verification](docs/operator-guide.md#screen-a-discovery-snapshot)

If a provider timed out, point the CLI at a directory containing saved discovery exports. This example checks for the same provider query within 60 seconds of the original timeout. It runs offline. The new manifest records the source of each reused response and identifies any unresolved branches. [Automatic recovery options](docs/operator-guide.md#find-a-saved-response-before-retrying)

```sh
python3 -m medextract_dossier recover-discovery work/kidney-screen \
  --cache-dir work/saved-searches --max-gap-seconds 60 --out work/kidney-screen-checked
```

To ask the provider directly, retry from the saved export. The new directory contains the original successful results alongside the outcome of the retry. Its manifest records both capture times and links to the original search. [Recover a timed-out search](docs/operator-guide.md#recover-a-timed-out-search)

```sh
python3 -m medextract_dossier retry-discovery work/kidney-screen --out work/kidney-screen-retry
```

If another saved search completed the same provider query within your chosen time window, reuse its ranked results offline. In this example the successful capture must be within 60 seconds of the timeout. The new export links to both inputs and preserves the provider’s original capture time. [Match and reuse a saved search](docs/operator-guide.md#reuse-a-saved-provider-response)

```sh
python3 -m medextract_dossier reuse-discovery work/kidney-screen \
  --from work/saved-searches/related-screen --max-gap-seconds 60 --out work/kidney-screen-reused
```

The literature commands use Python's standard library. Run this search from the checkout.

```sh
python3 -m medextract_dossier research \
  "Which studies compared cystatin C and creatinine for estimating kidney function?" \
  --terms '"cystatin C" AND creatinine AND "kidney function"' --limit 5
```

To read a specific paper, use the returned PMCID. For example, `PMC5634538` is a published primary-care cohort study on cystatin C measurement. This command searches its text for a passage about **reclassification**. Answers about diagnostic classification need different evidence from answers about whether a treatment improves outcomes. [Original paper](https://doi.org/10.1371/journal.pmed.1002400)

```sh
python3 -m medextract_dossier read-paper PMC5634538 --terms "reclassification" --limit 2
```

Both commands emit JSON. Save it by redirecting output to a file. Record which search terms you used. Check the provider statuses before interpreting an empty result, and inspect the paper before citing a claim. Repeating a live search later can return different records.

For discovery, group alternative biomarker names with `OR` and join the main concepts with `AND`. When reading a selected paper, include the outcome and follow-up in your terms. A request for “adjusted RR” alone can match several estimates in the same trial. The research guide shows which queries found the known studies, how passage ranking was tested and where coverage still falls short. [Retrieval quality and source selection](docs/retrieval-quality.md)

If you already have a DOI, PMCID or `PMID: 42239972`, supply it as the whole search expression. The response reports how many fetched citations match that identifier and places them first. A count of zero can occur when an index is unavailable or its returned page omits the paper.

## Review a published document and sign the result

For a formal review, prepare a local casebook with `evidence_origin` set to `published`. Include the actual source text that you are entitled to use, a bibliographic citation, source URL, reuse terms, and the corresponding annotated passages. The validator checks the source SHA-256 and each quotation's UTF-8 byte range. Eligible estimates must match the reviewed question's population, comparison, outcome and follow-up. Retrieved citations and lexical matches enter this workflow only after someone has assessed them.

```sh
medextract-dossier build /path/to/casebook --json
medextract-dossier build /path/to/casebook --review-store work/reviews --json
```

Use `--casebook /path/to/casebook` in the MCP startup arguments to expose the same review to the client. The three offline tools are `evidence_dossier` (review), `export_dossier` (sign), and `verify_dossier` (check signature, contents and decision replay). The signer must have permission to redistribute each included source. A trusted key authenticates the review bytes; the scientific assessment remains the reviewer's responsibility. [Casebook JSON Schema](schemas/v1/casebook.schema.json) · [Comparison JSON Schema](schemas/v1/comparison.schema.json) · [Signed review walkthrough](docs/operator-guide.md#sign-and-verify-a-review)

## Reproduce a published trial result offline

The default `build` uses an excerpt from a published cluster-randomized trial in Chinese village clinics. The study compared a package that included C-reactive protein (CRP) and serum amyloid A (SAA) testing with usual care. The reviewed outcome is antibiotic prescribing at the index consultation. The authors reported an adjusted relative risk of **0.80** (95% CI **0.78–0.82**). This estimate applies to the intervention package studied; the trial does not isolate the contribution of either biomarker. [Primary publication](https://pmc.ncbi.nlm.nih.gov/articles/PMC13227237/) · [Source attribution and review scope](examples/crp-saa-trial/PROVENANCE.md)

```sh
python3 -m medextract_dossier build
python3 -m medextract_dossier build --without-passage index-antibiotic-prescribing --json
```

The first command reports `effect_estimate_reported`. Omitting the decisive passage leaves `insufficient` direct evidence in this casebook. This reproduces a single published comparison, not a meta-analysis. The older fictional MX-1 casebook is available explicitly as `python3 -m medextract_dossier build examples/marker-mx1` for regression tests. [Published casebook](examples/crp-saa-trial/casebook.json) · [MX-1 test expectations](examples/marker-mx1/expected.json)

## Testing and citation

Does following references from an included paper find more studies than continuing a PubMed search? Run the ten-question benchmark offline from a Git checkout to compare the two strategies at the same screening workload. [Citation-supplement benchmark](https://github.com/AdrianPerez3/medextract-biomarker-evidence/tree/main/benchmarks/citation-supplement)

Run the offline tests, including deterministic provider fixtures, following the contribution guide. Live indexes, article access and network availability can change independently of a software release. [Contribution guide](CONTRIBUTING.md) · [Cite the software](CITATION.cff) · [Security policy](SECURITY.md)

## License and attribution

The software, project annotations, schemas, tests and project documentation use the **MIT License**. The six original MX-1 fixture files use **CC BY 4.0**. Both credit **MedExtract Evidence Dossier contributors**. The published trial excerpt, including the quotation in its casebook, retains the article authors' own **CC BY 4.0** attribution. Other retrieved papers retain their respective terms. [MIT License](LICENSE) · [Published trial credit](examples/crp-saa-trial/PROVENANCE.md) · [MX-1 credit](examples/marker-mx1/PROVENANCE.md) · [CC BY 4.0](LICENSES/CC-BY-4.0.txt)
