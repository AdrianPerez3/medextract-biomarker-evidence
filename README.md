# MedExtract Evidence Dossier — biomarker literature research for MCP and Python

Ask a population-level question about a biomarker and trace the evidence back to published papers. MedExtract Evidence Dossier lets a Model Context Protocol (MCP) client search **PubMed and Europe PMC**, inspect the citations returned by each, and locate passages in openly licensed full text. A reviewer can also freeze an annotated casebook, sign it and let a recipient independently replay its decisions offline.

This is a research preview. The live tools retrieve records and passages. A researcher or the calling AI client must assess whether the study design, population and outcome answer the question. Scientific accuracy on a held-out literature set and clinical use have not been validated.

## Ask a biomarker question from an MCP client

From a copy of the repository on macOS or Linux, install the server with Python 3.11 or later.

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

Run the offline tests, including deterministic provider fixtures, following the contribution guide. Live indexes, article access and network availability can change independently of a software release. [Contribution guide](CONTRIBUTING.md) · [Cite the software](CITATION.cff) · [Security policy](SECURITY.md)

## License and attribution

The software, project annotations, schemas, tests and project documentation use the **MIT License**. The six original MX-1 fixture files use **CC BY 4.0**. Both credit **MedExtract Evidence Dossier contributors**. The published trial excerpt, including the quotation in its casebook, retains the article authors' own **CC BY 4.0** attribution. Other retrieved papers retain their respective terms. [MIT License](LICENSE) · [Published trial credit](examples/crp-saa-trial/PROVENANCE.md) · [MX-1 credit](examples/marker-mx1/PROVENANCE.md) · [CC BY 4.0](LICENSES/CC-BY-4.0.txt)
