# Finding and checking biomarker evidence

The fastest useful path depends on what the researcher already knows. Start with PubMed and Europe PMC for a topic. If a paper or trial identifier is already known, resolve it directly where possible. For a numeric question, read the original report and check the population, comparison, endpoint, follow-up, analysis and interval together. A protocol, registry entry and results paper can describe the same patients. This guide records a retrieval investigation on 25 November 2026 and proposes how to evaluate the next changes.

## What the current MCP retrieves

1. `research_biomarker` sends one expression to both indexes. It requests their first ranked pages, defaults to five results, and accepts a limit up to 20. PubMed uses ESearch and then ESummary. The indexes can interpret the same words differently. Inspect each provider's status, total hits and ranks, plus PubMed's query translation and warnings. [PubMed search guide](https://pubmed.ncbi.nlm.nih.gov/help/) · [Europe PMC API](https://europepmc.org/RestfulWebService)
2. The merge uses compatible identifiers, preserves the providers’ ranks and interleaves their records before the final cap. A duplicate citation from a second index supplies another route to one publication. Two publications with distinct DOIs can still report the same experiment. [Search implementation](../medextract_dossier/literature_search.py) · [Cochrane Handbook, chapter 5](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05)
3. `read_paper` checks the requested PMCID, examines reuse terms, and ranks short passages in eligible body prose. Its XML digest, paragraph digest, XPath and offsets identify the returned text. A registered outcome, an observed estimate, a subgroup analysis and a quotation from an earlier paper carry different meanings even if every quoted word is correct. The signed review is a separate, annotated workflow. [Reader](../medextract_dossier/paper_text.py) · [Reviewed casebook workflow](operator-guide.md)

## What happened on real searches

We selected the CRP/SAA trial and its protocol as two known reports and the cystatin C cohort as a third. We ran two questions, each as a full sentence and a concept query, against the baseline with a final cap of ten. Ranks below refer to the returned records. The table measures retrieval of these known items. Other relevant studies were not adjudicated.

| Search | PubMed total | Europe PMC total | Known reports in the first ten |
| --- | ---: | ---: | --- |
| CRP/SAA, question | 0 | Unavailable | Neither report |
| CRP/SAA, concepts | 3 | Unavailable | Results paper first, protocol second |
| Cystatin C, question | 0 | 614 | Cohort absent |
| Cystatin C, concepts | 5 | 165 | Cohort absent |

The four first-pass inputs were recorded verbatim below.

```text
Which trials studied C-reactive protein and serum amyloid A testing for antibiotic prescribing in primary care?
"C-reactive protein" AND "serum amyloid A" AND antibiotic* AND (prescribing OR prescription)

Does using cystatin C instead of creatinine change chronic kidney disease classification in primary care?
"cystatin C" AND "chronic kidney disease" AND "primary care" AND reclassification
```

After observing the kidney miss, we tried `"cystatin C" AND ("primary care" OR "general practice") AND (reclassif* OR "confirm the diagnosis")`. This returned 11 PubMed and 356 Europe PMC hits and still missed the cohort in the merged top ten. A separate broader query, `"cystatin C" AND ("primary care" OR "general practice") AND ("chronic kidney disease" OR CKD)`, also missed the cohort in the first ten, with 78 PubMed hits and Europe PMC unavailable. Supplying the known DOI returned it first from both providers. This follow-up tests identifier recovery. It cannot establish that a new user would discover the paper independently. At 16:55 UTC on the same day, the separate search `"C-reactive protein" AND "antibiotic prescribing" AND ("village clinics" OR "nursing home residents")` returned 86 Europe PMC hits and six PubMed hits, yielding ten distinct records across the two fetched pages before the final cap of six. [CRP/SAA results](https://pubmed.ncbi.nlm.nih.gov/42239972/) · [Protocol](https://pubmed.ncbi.nlm.nih.gov/40920832/) · [Cystatin C cohort](https://doi.org/10.1371/journal.pmed.1002400)

**Practical search strategy.** State whether the question concerns diagnosis, prognosis or an intervention. Start with a biomarker name and a condition or intervention. Add synonyms with `OR`. Reserve timepoints, effect measures and detailed eligibility criteria for screening when including them in the query would exclude relevant reports. For systematic searches, log each provider's exact expression and date. Feed known seed papers into a separately recorded citation search. Preserve inaccessible and unscreened records in the denominator. [PRISMA-S search reporting guideline](https://doi.org/10.1186/s13643-020-01542-z)

**An identifier can also appear in a commentary.** A live MCP search for `10.1016/j.lanwpc.2026.101888` returned two records. The baseline put the accompanying commentary first and the requested trial second. Replaying the same citations with their original provider ranks, the revised search put the exact DOI first and reported one match. It also handles a standalone PMCID or explicitly prefixed PMID. A numeric year such as 2026 remains a topic query. The count covers only retrieved pages. The installed MCP then returned the trial first on a fresh live search with both providers available. A direct resolver would also be able to recover a known record absent from those pages. [Results paper](https://doi.org/10.1016/j.lanwpc.2026.101888) · [Commentary](https://doi.org/10.1016/j.lanwpc.2026.101904)

## Testing which passage a researcher actually sees

In the CRP/SAA cluster trial, the first returned passage for `adjusted RR` previously emphasized secondary comparisons. The primary estimate was already present in the same long paragraph. A later window used the search terms more often and displaced it. The revised reader gives explicit numeric adjusted-risk expressions and relevant endpoint wording extra weight for that query type. Users can specify a secondary outcome or analysis. Continue to check the trial arms: the 0.80 estimate and 0.78–0.82 interval apply to the whole prescribing intervention package. [Primary publication](https://pmc.ncbi.nlm.nih.gov/articles/PMC13227237/)

We fixed the following questions and expected result anchors from the publications before running the comparison. Each success requires the specified contextual and numeric anchors together in one passage under a Results or Findings heading. For example, matching 1.51 alone would accept the wrong endpoint in the emergency-department trial. It also reports 1.51 for a secondary analysis with a different interval. “Outside top three” and an unsupported rights statement have different causes. [Scoring cases and matching rule](../scripts/check_passage_retrieval.py)

| Study and target | Baseline rank | Revised rank |
| --- | --- | --- |
| CRP/SAA prescribing package, adjusted RR 0.80 (0.78–0.82) | Outside top three | 1 |
| Cystatin C cohort, 50 of 653 reclassified to eGFR ≥60 (no CKD, 7.7%) | 1 | 1 |
| ICU antibiotic duration, treatment and control upper quartiles 8.8 and 11.3 days | License check declined | License check declined |
| Emergency-department trial, six-month abstinence RR 1.51 (1.13–2.02) | 1 | 1 |
| Same trial, covariate-adjusted abstinence estimate 1.50 (1.12–2.01) | 1 | 1 |
| Same trial, complete-case sensitivity estimate 1.30 (1.01–1.67) | 1 | 1 |

Across all six requests, four baseline and five revised first passages met the rule. All 14 passages returned by each revision replayed at their reported offsets. Across the five requests the reader could process, the counts were four and five. One case motivated the fix, and three questions concern one behavioral intervention trial chosen to test endpoint discrimination outside the biomarker use case. These are regression checks. They do not estimate clinical accuracy or prove complete evidence retrieval. The ICU publisher assigns CC BY 4.0 to article material with third-party exceptions and separately mentions CC0 for data. The current parser declines this combined statement. Supporting it safely would require distinguishing which terms apply to each content type. [ICU trial](https://pmc.ncbi.nlm.nih.gov/articles/PMC7266125/) · [Emergency-department trial, results](https://doi.org/10.1371/journal.pmed.1004916)

To replay, save these four NCBI EFetch XML files once and use the same directory for both revisions. The scorer uses the normal reader with a local transport and prints the exact SHA-256, access status and rank for each case. No paper text is distributed with this benchmark.

```sh
mkdir -p work/passage-checks
for id in 13227237 5634538 7266125 13568513; do
  curl --fail --silent --show-error \
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=$id&retmode=xml&tool=medextract_evidence_dossier" \
    -o "work/passage-checks/PMC$id.xml" || exit 1
  sleep 0.4
done
python -m scripts.check_passage_retrieval work/passage-checks
```

The baseline predates the scorer. From the current checkout, export that revision to a temporary directory and run the current scorer by filename against it. Confirm that both outputs match the XML digests below before comparing them with the historical results. The scorer also reconstructs every returned passage from its locator and checks both source and paragraph digests.

```sh
baseline=$(mktemp -d)
git archive fead8554 | tar -x -C "$baseline"
PYTHONPATH="$baseline" python scripts/check_passage_retrieval.py work/passage-checks
```

| PMCID | SHA-256 of NCBI XML used in this comparison |
| --- | --- |
| PMC13227237 | `204e0ff253c2618ef940de685f495622de4fa0dfbfac6c530d1a868595007fa0` |
| PMC5634538 | `599a883a7cf8ba139b5eb3e959b2e4f31a570e7518c5152687b13b2a5252d871` |
| PMC7266125 | `13d4603675165375f88fee2711a31b2f17d921599b1cbb97e7638f2963db1103` |
| PMC13568513 | `8a43ed69993733a787adef063290f6a0c5068294531903386b12ac0f300b19a8` |

## Which sources to add

| Priority and trigger | Source and use | Acceptance check |
| --- | --- | --- |
| 1. A researcher supplies a DOI, PMID, PMCID or trial ID | Resolve the identifier directly and preserve which service established each mapping. The existing reader accepts exact PMCIDs. A general identifier resolver is proposed. [NCBI ID Converter coverage](https://pmc.ncbi.nlm.nih.gov/tools/id-converter-api/) | Detect conflicting IDs. A paper outside PMC can still have a valid DOI and PMID. |
| 2. A selected trial has an explicit registration ID | Retrieve its ClinicalTrials.gov record. Preserve the dates of registration, updates and results posting, along with registered endpoints, linked references and their relation types. Review which report contains results. [Registry API](https://clinicaltrials.gov/data-api/api) | NCT06568432 is completed, has no posted registry results and links both the protocol and published outcome as derived references. PMID 42239972 was listed as a journal article in PubMed's summary, so a mandatory trial-type tag would discard it. Preserve both reports and count one confirmed study. [Registration](https://clinicaltrials.gov/study/NCT06568432) |
| 3. A report could support a conclusion | Check its DOI for Crossref updates and read linked notices. Record notice type, direction, source and check time. Deduplicate a single notice supplied by both publisher and Retraction Watch. [Crossref notice coverage](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/) | An original and its retraction notice have reciprocal links. Distinguish a correction, a retraction, no known notice, lookup failure and an unchecked DOI. [Original DOI record](https://api.crossref.org/works/10.1177/1758835920922055) · [Notice DOI record](https://api.crossref.org/works/10.1177/17588359231172420) |
| 4. The user asks for reviews or terminology varies across trials | Add a review-specific search with native provider syntax, then inspect the included studies. Try one generation of cited/citing works from screened seeds, initially through Europe PMC. Test the added yield from OpenAlex under a fixed request budget. [PubMed review filter](https://pubmed.ncbi.nlm.nih.gov/help/#systematic-reviews) · [OpenAlex references](https://help.openalex.org/data/works/references/) | Count additional eligible study families per 100 records screened. Record citation direction and seed. Keep a review and its included participants from being counted twice. |

Publication access and metadata coverage are separate. Keep each provider's attribution and permitted uses alongside its output. ClinicalTrials.gov sets conditions for redistributing its records. A citation-graph service's ability to return a paper does not extend that paper's reuse terms. [ClinicalTrials.gov terms](https://clinicaltrials.gov/terms-conditions) · [PMC open-access subset](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/) · [Semantic Scholar API agreement](https://api.semanticscholar.org/license/)

## What comparable tools contribute

| Project | Practice worth testing in MedExtract |
| --- | --- |
| BioMCP and PaperQA2 | BioMCP distinguishes ranked search from exact article and trial lookup. PaperQA2 separates finding documents, gathering evidence and synthesizing an answer and offers a registry search. Compare their retrieval on the same frozen questions and permitted documents before making a comparative accuracy claim. [BioMCP article guide](https://biomcp.org/user-guide/article/) · [BioMCP trials](https://biomcp.org/user-guide/trial/) · [PaperQA2 tools](https://github.com/Future-House/paper-qa/blob/c692d0dfeac4a0296599f7fe21a253a58b7ac12a/src/paperqa/agents/tools.py) |
| BiomarkerKB and Valsci | Capture the measured entity, specimen and condition before comparing claims. Preserve evidence for disagreement. A trial of lowering a biomarker cannot settle whether a higher baseline biomarker predicts an outcome. [BiomarkerKB schema](https://github.com/clinical-biomarkers/backend-pipeline/blob/e967e6d12034a9df7a71eefdb08979335cc07101/data_model.schema.json) · [Valsci paper](https://doi.org/10.1186/s12859-025-06159-4) |
| Tekmeria, Scientific Claim Verification Engine and Zetesis | Track quotation fidelity, access sufficiency and attestation separately. An abstract-only number may need full-text review. A replayable quote can faithfully reproduce a secondary result that answers a different question. The Zetesis hosted tool list was checked, without running a paid evaluation. [Tekmeria audit](https://github.com/PedroMarkovicz/tekmeria/blob/4b6af0924ce15ce45ca8e101e74688497ba17706/src/tekmeria/contract/audit.py) · [Evidence sufficiency](https://github.com/VivienP/scientific-claim-verification-engine/blob/3ccad960e4f36b0a97f4e00551a81baa19b23c65/src/policy/evidence_sufficiency.py) · [Zetesis client](https://github.com/reutavidan/zetesis/blob/5c0d5982a485da0ac6e97797f6d113be217bd1b3/src/zetesis/client.py) |
| Elicit and factual-research | Keep the search strategy, screening decisions, exclusions and reasons reviewable. Elicit's documented workflow was inspected. A commercial head-to-head was outside this experiment. [Elicit systematic review workflow](https://support.elicit.com/en/articles/14759154-systematic-reviews-in-elicit) · [Example search log](https://github.com/masluny/factual-research/blob/23f71c7c19be720e2b32d2d5dce1fc7052e474e5/examples/vitamin-d/README.md) |

## What made the search faster

Europe PMC can run while PubMed searches and fetches its summaries. The implementation overlaps these independent waits, keeps the PubMed calls ordered and paced, admits two federated searches per process, and joins each worker. The merge still uses provider ranks in a fixed order. Independent client processes sharing an outbound IP share the upstream request budget. [NCBI usage guidance](https://www.ncbi.nlm.nih.gov/books/NBK25497/)

On 20 alternating baseline/candidate pairs, median elapsed time fell from **789.6 to 507.7 ms** and nearest-rank p95 from **796.2 to 512.1 ms**. The controlled transport supplied 20 hits per provider, ten duplicate citations, and 280/80/140 ms Europe PMC, PubMed search and summary delays. The actual 350 ms NCBI start interval remained active. All 40 measured outputs agreed after excluding the retrieval timestamp. When PubMed failed immediately and Europe PMC took 80 ms, both versions took 85.5 ms at the median. A single live CLI smoke returned three PubMed citations during a Europe PMC failure. The timing improvement was measured on the controlled workload. An upstream timeout can still dominate a live request.

Run `python -m scripts.bench_literature_search --runs 20` to time the local version without network access. To measure the baseline, point `PYTHONPATH` at a separate checkout of `fead855` and run the same script by filename. The JSON includes module digests, a normalized result digest, the minimum observed interval between PubMed requests, and latency. A separate 20-run replay per revision with this harness returned the same result digest and medians of 781.9 and 505.2 ms. [Benchmark harness](../scripts/bench_literature_search.py)

There is a local cost for improved ranking. On the 106 KB prescribing trial XML, an offline timing probe including parsing measured approximately 5.8 to 7.7 ms for the bare adjusted-RR query and 5.6 to 8.5 ms for a qualified adjusted-RR query. The ordinary lexical query stayed around 5.5 ms. The network benchmark and these single-paper CPU timings measure different parts of a research session.

## How to establish whether research accuracy improves

1. **Freeze questions and judgments before tuning.** A practical first set is 60 questions, balanced across diagnostic, prognostic and intervention uses. Treat that size as a pilot design choice. Split development and evaluation by study family and biomarker, including newer studies with incomplete indexing. Such a comparison needs a reference set with documented, independent eligibility and passage judgments. The original biomarker pilot ended after packet preparation. Its unlabeled candidate counts measure retrieval coverage only. Give both methods the same source snapshot, screening budget and time cutoff. [BioASQ document and snippet tasks](https://www.bioasq.org/node/1) · [Evidence Inference 2.0](https://aclanthology.org/2020.bionlp-1.13/)
2. **Measure each failure separately.** Report known eligible families retrieved at 10 and 20, eligible families per 100 screened, and first relevant rank. For selected papers, score the correct comparison, outcome, timepoint, estimate and interval together at ranks one and three. Track wrong-endpoint hits, quotation replay failures, false support from abstracts or protocols, unsupported rights, and appropriate abstention. Give numerators and denominators at every stage. Estimate uncertainty by resampling questions or families, keeping repeated questions on one trial together. A pooled set of judged papers only measures recall against that pool.
3. **Include plausible mistakes.** Match serum versus urine and total versus free analyte. Separate sensitivity/specificity from prognosis, baseline association from benefit of changing the marker, and a multicomponent package from its individual tests. Include cohorts that overlap, prespecified versus post-hoc analyses, null findings, differing follow-up, adjusted versus crude effects, table-only estimates, corrections and withdrawn versions. Include a paper with the correct numerical value under the wrong outcome. Verify quotations against the exact source snapshot.
4. **Spend compute where it changes a decision.** Benchmark the current lexical retrieval against BM25 and a reranker on a bounded shortlist. Try reciprocal-rank fusion as an alternative to interleaving. Run each addition separately and count the incremental eligible families and correct passages. BEIR supports BM25 as a serious baseline across diverse corpora and reports a cost for stronger rerankers. It does not establish the best model for these biomarker questions. [BEIR benchmark](https://datasets-benchmarks-proceedings.neurips.cc/paper_files/paper/2021/hash/65b9eea6e1cc6bb9f0cd2a47751a186f-Abstract-round2.html)
5. **Measure service cost with quality.** Repeat cold and warm workloads with fast, slow, rate-limited and unavailable providers. Report median and p95 elapsed time, requests, bytes and partial responses. Consider short-lived metadata caching and reuse of JATS by content hash if measurement shows repeat demand. Keep check dates visible and recheck publication notices at review time. Adopt a new default only after a paired evaluation shows better retrieval at an acceptable latency and screening cost.
