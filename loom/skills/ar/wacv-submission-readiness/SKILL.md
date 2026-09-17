---
name: wacv-submission-readiness
description: Prepare and validate anonymous WACV submissions. Use only when the target venue is WACV, especially for the WACV review template, track selection, the eight-page technical-body boundary, separate supplementary material, paper-ID headers, fully packed page eight, and final PDF checks.
---

# WACV Submission Readiness

## Scope

This is a WACV-only venue skill. Apply `paper-results-reporting` separately for
venue-independent point-estimate-only tables, interval placement, provenance,
and normal submission-PDF naming.

Rules and track names can change between cycles. Check the official call and
author kit before final delivery. For WACV 2027, use the verified project rules
below unless the current official materials disagree.

## Review template and track

Use the WACV review class on US Letter in anonymous two-column form:

```latex
\documentclass[10pt,twocolumn,letterpaper]{article}
\usepackage[review,<track>]{wacv}
```

Replace `<track>` with the option assigned to the paper:

| Track label | `wacv.sty` option |
|---|---|
| Applications | `applications` |
| Algorithms | `algorithms` |
| Evaluations & Datasets | `datasets` |

Do not infer the track from the paper topic during the final build. Read the
frozen submission record or user-approved paper summary, then use the same
option in `main.tex`, `supplement.tex`, and any WACV rebuttal source.

Set and verify:

```latex
\def\wacvPaperID{<OpenReview-ID>}
\def\confName{WACV}
\def\confYear{2027}
```

The rendered header, title block, PDF text, source, handoff notes, and manifest
must all use the current submission ID. A prior-round ID may remain only in an
explicitly labelled historical record, never in an upload artifact.

## Eight-page technical-body boundary

For this project's WACV workflow:

- The main technical body is exactly eight pages and is packed with useful
  content through the bottom of page eight.
- References begin on page nine and do not count toward the eight-page body
  limit.
- Do not include an Appendix heading or appendix section in the main PDF.
- Integrate essential appendix evidence into Method, Experiments, Discussion,
  or Limitations when it strengthens the main argument.
- Move remaining appendix material into a separately compilable
  `supplement.tex` that produces `supplement.pdf`.

Do not pad page eight with repetition, oversized spacing, or decorative
material. Fill real space with definitions, controls, ablations, limitations,
implementation details, or evidence needed to evaluate the claims.

Check the technical-body boundary separately from total PDF pages. A main PDF
with references may have nine or more total pages while still satisfying the
eight-page body limit.

## Result display

The general `paper-results-reporting` rule applies without exception:

- abstract and body prose report point estimates rather than numeric interval
  endpoints;
- every experimental-results table, including supplement and
  aggregator-generated tables, shows one measured point estimate per metric
  cell, with no confidence intervals, standard deviations, standard errors,
  error bars, or other uncertainty/spread in cells or captions;
- figure labels do not print interval endpoints;
- full interval endpoints and additional uncertainty analysis belong in the
  supplement's experiment-details prose or a separate experiment-details
  artifact, not result-table cells or captions.

Update generators and exporters as well as generated TeX, figures, and PDFs so
a rebuild cannot restore forbidden interval text.

## Supplement

Build the supplement independently with the same current paper ID, year, track,
anonymity, title, notation, and result values as the main paper. It may contain:

- appendix experiment details and full uncertainty endpoints;
- derivations, extended proofs, additional ablations, and per-seed records;
- implementation details, extended qualitative results, and audit tables.

The main paper must not depend on the supplement for a central claim, and the
supplement must not expose author identity, private paths, hosts, or searchable
internal hashes.

## Anonymity and artifact hygiene

- Keep review mode enabled and use anonymous author metadata.
- Remove acknowledgements, affiliations, emails, personal links, local paths,
  hostnames, and identifying PDF metadata.
- Keep commit hashes and machine provenance outside rendered submission PDFs.
- Remove stale venue names, old paper IDs, placeholder text, and template
  instructions.
- Build the normal main artifact as `main-paper-<OpenReview-ID>.pdf`; leave no
  legacy `main.pdf`, and refresh recorded size and SHA-256 values.

For a Round-1 Revise-and-Resubmit package, also apply `paper-rebuttal` and
`paper-rebuttal-delivery/WACV.md`. Its frozen delivery policy may require the
upload names `revised-paper.pdf`, `rebuttal.pdf`, and `supplement.pdf`; that
explicit package policy overrides the normal main-artifact filename.

## Build and acceptance loop

1. Force-build the main PDF and `supplement.pdf` from their current sources.
2. Reject LaTeX errors, undefined citations or references, overfull content,
   clipped floats, overlaps, unreadable tables, and stale generated artifacts.
3. Confirm the rendered header has the current ID and approved track.
4. Confirm pages 1--8 are technical content, page eight is substantively full,
   and References begins on page nine.
5. Confirm the main PDF has no appendix section and the supplement builds
   independently.
6. Confirm all result tables show point estimates only, then scan source,
   rendered text, and figure pixels for numeric interval endpoints, identity
   leaks, hashes, private paths, old IDs, and stale track labels.
7. Visually inspect every main-paper page and every supplementary page.
8. Recompute manifest sizes and checksums only after the final verified build.

Report the eight-page body count, total main-PDF page count, reference start
page, supplement page count, track, paper ID, filenames, sizes, and checksums.
