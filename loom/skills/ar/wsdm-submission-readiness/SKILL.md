---
name: wsdm-submission-readiness
description: Prepare and validate WSDM submissions in anonymous ACM proceedings format. Use only when the target venue is WSDM, especially for ACM template migration, the nine-page technical-content boundary, mean-only narrative results, Ethical Considerations placement, appendix triage, and final PDF checks.
---

# WSDM Submission Readiness

## Scope

This is a WSDM-only venue skill. Do not apply its template or page-boundary
rules to another conference. Use `paper-results-reporting` separately for
point-estimate-only tables and provenance.

Rules can change between WSDM cycles. Check the official call for papers before
final delivery. For WSDM 2027, use the verified rules below unless the official
site has been updated.

## WSDM 2027 submission boundary

- Use the ACM proceedings class:

  ```latex
  \documentclass[sigconf,anonymous,review]{acmart}
  ```

- Use `ACM-Reference-Format` for the bibliography.
- The technical paper is at most nine pages, including figures, tables, and
  any appendix included in the submission PDF.
- Ethical Considerations and References do not count toward those nine pages.
- Place the paper-specific Ethical Considerations section immediately after
  the technical body and before References.
- Keep the review PDF anonymous and include the assigned CMT submission ID.

Do not infer compliance from total PDF pages alone. Inspect where the technical
body ends, where Ethical Considerations begins, and where References begins.

## WSDM result-display policy

Apply the general `paper-results-reporting` interval-placement rule to the
abstract, body, tables, captions, and figure labels. Every experimental-results
table, including appendix and aggregator-generated tables, must show one
measured point estimate per metric cell. Do not print confidence intervals,
standard deviations, standard errors, error bars, or other uncertainty/spread
in table cells or captions. A caption may state the replicate unit and count
without printing the omitted uncertainty statistic.

In WSDM narrative prose, report the point estimate (mean) only. This applies to
every numerical experimental result in the abstract, introduction, method,
experiments, discussion, limitations, conclusion, captions, and numerical
callouts embedded in figures.

- Do not write a point estimate followed by bracketed or parenthesized interval
  endpoints in prose.
- Do not print `mean ± SD`, `mean ± SE`, or another numeric uncertainty pair
  as an inline prose result.
- A caption may define what graphical error bars or shaded bands represent,
  and a figure may retain those visual uncertainty marks, but its text labels
  must not print the interval endpoints.
- Preserve the underlying CI/SD fields in machine-readable results and, when
  scientifically useful, report full endpoints in the experiment-details
  portion of `appendix-backup.tex`; do not put them in the main WSDM PDF.
- Update figure generators and LaTeX exporters, not only generated PDF/PNG
  files, so a rebuild cannot restore forbidden interval labels.

Before delivery, scan both main source and rendered main-PDF text for
estimate-plus-interval patterns, including tables. Inspect figure pixels when
labels are paths or otherwise not extractable as text. Full interval endpoints
may appear only in appendix experiment details kept outside the main WSDM PDF.

## Laboratory packaging policy

For the full-nine-page workflow used by this project:

1. Integrate scientifically necessary appendix material into the relevant
   Method, Experiments, Discussion, or Limitations section.
2. Do not leave an `Appendix` heading in the main submission PDF.
3. Preserve overflow material in an independently compilable
   `appendix-backup.tex`.
4. Do not input or include that backup from `main.tex`.
5. Treat the backup as an archive, not as a conference supplement unless the
   current WSDM rules explicitly permit one.

Do not pad weak content merely to fill page nine. Use the space for evidence,
definitions, ablations, limitations, or reproducibility details that improve
the paper.

## Ethical Considerations

Write a paper-specific section rather than boilerplate. Cover the relevant
subset of:

- data licensing, consent, privacy, and redistribution;
- ranking, recommendation, exposure, or allocation harms;
- model, judge, benchmark, or contamination misinterpretation;
- misuse and high-stakes deployment boundaries;
- compute and environmental costs;
- limitations of the evidence and concrete mitigations.

The section must not introduce unsupported claims or pretend that unavailable
demographic, causal, privacy, or safety evidence was measured.

## ACM and anonymity checks

- Keep `anonymous,review` enabled.
- Use anonymous author metadata and remove acknowledgements for review.
- Remove author names, affiliations, emails, personal paths, hostnames, and
  identifying PDF metadata.
- Keep raw hashes and internal artifact identifiers outside the rendered paper;
  `paper-results-reporting` defines the provenance record.
- Remove ICLR/NeurIPS/WACV style files, commands, and bibliography styles from
  the active build.
- Ensure every figure and table is legible at the ACM two-column print size.

## Build and acceptance loop

1. Force-build `main-paper-<CMT-ID>.pdf` from `main.tex` rather than trusting a
   stale artifact. Follow the general naming skill and leave no legacy
   `main.pdf`.
2. Build `appendix-backup.pdf` independently when a backup exists.
3. Reject LaTeX errors, undefined citations or references, overfull content,
   clipped floats, overlapping text, and visibly unbalanced reference columns.
4. Confirm the main PDF has no Appendix heading or appendix section.
5. Confirm technical content ends on page nine or earlier.
6. Confirm Ethical Considerations begins only after the technical boundary and
   References follows it.
7. Confirm the abstract, body prose, main tables, captions, and figure labels
   contain no numeric interval endpoints, and every experimental-results table
   contains point estimates only with no uncertainty/spread in cells or
   captions.
8. Scan the rendered PDF for identity leaks, local paths, internal hashes,
   placeholders, stale venue names, and old template text.
9. Visually inspect all nine technical pages plus the ethics/reference pages.

Report the technical-body page count separately from the total PDF page count.
An eleven-page PDF is common when pages 1--9 are technical content, page 10 is
Ethical Considerations, and page 11 is References, but eleven total pages is
not itself a rule.
