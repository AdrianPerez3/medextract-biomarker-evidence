# Do ten backward citations find more studies than ten more PubMed results?

After screening the same 50 PubMed papers, following references found eight known included diagnostic reports in the next 80 screening positions across eight questions. Continuing in PubMed order found four. Two intervention reviews found zero additional known trials in their final ten positions with either strategy.

## Reproduce the results

From a Git checkout with Python 3.11 or later, run:

```sh
python3 benchmarks/citation-supplement/score.py
```

Add `--json` for machine-readable results. The script uses the Python standard library and the included [observations](observations.tsv). It recalculates the scores from saved paper IDs, list positions and historical decisions. A new search of today's PubMed or Crossref index can return different results.

| Unit | Final positions per strategy | PubMed, final ten | Supplement, final ten | PubMed, whole sixty | Supplement, whole sixty |
| --- | ---: | ---: | ---: | ---: | ---: |
| Known diagnostic reports, eight questions | 80 | 4 | 8 | 15 | 19 |
| Additional trial families, two questions | 20 | 0 | 0 | 1 | 1 |

The diagnostic supplement gained seven reports and displaced three. Four gains were absent from their topic's entire captured PubMed page. Three belonged to glaucoma imaging and one to lumbar examination. All four links were also found in the citing articles' bibliographies. The two laboratory assay questions supplied no usable citations. All 20 final intervention records in each strategy had unknown eligibility in the published reference sets. This experiment measures rediscovery of known inclusions within a fixed workload. Overall recall on new biomarker questions remains unknown. [Glaucoma bibliography, references 16, 17 and 23](https://pmc.ncbi.nlm.nih.gov/articles/PMC3721629/) · [Lumbar examination bibliography, reference 21 on page 5](https://nek.istanbul.edu.tr/ekos/MAKALE/M5387.pdf#page=5)

## Protocol

We froze the comparison before choosing eight diagnostic and two intervention reviews. The first PubMed page contained up to 500 records in relevance order for each question. Each question used a historical publication-year cutoff. We screened in native order until reaching the first published inclusion or 100 records. Published review decisions stood in for a researcher's decisions. Both strategies continued from this same position. [Query terms, cutoffs, seeds and provider counts](protocol.json)

1. Screen the next 50 records in PubMed order.
2. For the **native** strategy, screen the next ten PubMed records.
3. For the **supplement** strategy, follow one generation of references from the original included paper. Take up to ten previously unseen PMIDs in provider order and fill unused positions from the native list. Skip duplicates and every paper already screened. A question without a qualifying seed or usable references continues in native order.

The lookup used PubMed's backward citation links first and tried Crossref if none were usable. Crossref citations required an exact DOI match to a PubMed record. Across the ten questions, eight had a qualifying seed and seven yielded usable references. The lookups cost 38 additional provider requests and filled 70 of the 100 final positions. Database coverage, the age of the seed and the year cutoff all limit what backward citations can recover. [PubMed reference coverage](https://pubmed.ncbi.nlm.nih.gov/help/#references) · [Crossref deposited metadata](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)

## Reading the observations

One row identifies a PMID within a review. The rank columns give one-based positions within the initial screening prefix, shared 50, native final ten, or selected citations. An empty rank means the paper was absent from that list. A paper can occur in both final candidate lists. `in_native_capture` records membership anywhere on the original PubMed page. The supplement's remaining positions are reconstructed by refilling in native order.

`judgment` preserves three states: included, explicitly excluded, or unjudged. Eligible trial reports share a topic-local family identifier when they report the same study. `primary` identifies an original results paper under the study mapping. For the final ten, the scorer credits only families first encountered after the shared 50. For the whole sixty, it also credits those first found in the shared block. A family found in the initial screening prefix earns no later credit.

The TSV contains identifiers and derived screening facts. The diagnostic judgments come from CLEF TAR 2017 at revision `dbc13d0`; its MIT copyright notice accompanies the fixture. The intervention family mapping follows the published included-study lists in the 2016 transfusion-threshold and 2018 ovarian-reserve reviews. The table carries no article text or patient records. [CLEF TAR source](https://github.com/CLEF-TAR/tar/tree/dbc13d02bb3e2f8ebc90e62ff47f5eb591e5ca20/2017-TAR) · [CLEF license](CLEF-LICENSE.txt) · [Transfusion review](https://pubmed.ncbi.nlm.nih.gov/27731885/) · [Ovarian reserve review](https://pubmed.ncbi.nlm.nih.gov/29388198/)
