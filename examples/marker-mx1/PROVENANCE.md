# MX-1 example provenance

The marker MX-1, program K, participants and aggregate results were invented for this example. The inputs were written from scratch without patient records, publication excerpts or external datasets. The `reviewed` flag records the example author's check of an annotation against the fictional text.

The three study texts in `sources/`, the questions and annotations in `casebook.json`, the expected answers in `expected.json`, and this provenance note are licensed under Creative Commons Attribution 4.0 International. Suggested credit for reuse is **“MX-1 synthetic evidence example” by MedExtract Evidence Dossier contributors, licensed under CC BY 4.0**. Include the license link with the credit and indicate any changes you make. When sharing a signed copy of this example, place the credit alongside the bundle so the signed inventory stays intact. [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

| Source file | Study family | Content and attribution |
| --- | --- | --- |
| `sources/cohort.txt` | `mx1-cohort` | Original synthetic report of a baseline-marker cohort, written for this example. |
| `sources/trial.txt` | `mx1-trial` | Original synthetic report of a separate randomized trial, written for this example. Both trial endpoints share its 300 participants. |
| `sources/protocol.txt` | `mx1-protocol` | Original synthetic plan for a future cohort, written for this example. Recruitment has yet to begin. |

## Reading the estimates

The controlled identifiers for the two baseline MX-1 groups include the threshold of 10 mock units/L. The higher group includes values equal to the threshold.

Both questions concern the first task failure by month 12, an adverse event. The cohort groups people by their initial marker measurement and yields an unadjusted association. The trial groups people by assignment. Its clinical risk ratio spans both lower and higher risks. The separate trial marker result describes the difference in mean change from baseline, with a negative value indicating a greater decrease under program K. A marker decrease alone cannot answer the task-failure question.

All intervals are two-sided 95% normal approximations, rounded to two decimal places. For each risk ratio the log-scale standard error is `sqrt(1/events_A - 1/n_A + 1/events_B - 1/n_B)`. For the marker, the standard error of the difference in mean change is `sqrt(sd_A²/n_A + sd_B²/n_B)`. Both use the multiplier 1.96.

## Replaying the expectations

`casebook.json` identifies each source by its SHA-256 digest over the exact UTF-8 bytes. Each quote starts at its zero-based byte offset and ends just before the supplied end offset. `expected.json` gives the manually assigned status, decision reason, independently eligible study families and per-passage reason for each question. Its two modified cases both ask the prognosis question.

For the removal case, omit only `cohort-result`. For the duplicate case, add a source with a separate identifier and file, attribute it to `mx1-cohort`, and repeat its result as `cohort-repeat-result`. Two direct passages then describe a single cohort. This example tests those decisions on one set of invented studies. Performance on published literature and independent scientific adjudication remain unmeasured.

After checking the passage-level decisions, we recorded SHA-256 digests for the complete baseline and removal JSON outputs in `expected.json`. The test serializes the parsed comparison with Python JSON, sorted keys, UTF-8, unescaped non-ASCII characters, compact separators and no final newline. These regression digests complement the manually specified scientific expectations.
