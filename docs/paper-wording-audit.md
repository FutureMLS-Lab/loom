# Paper wording audit

- Audit root: `/data/shared/zhizhousha/workspace/loom-project/loom-claude-paper/research-factory/.RUD`
- Audit date: 2026-09-18
- Coverage: 23 paper projects; each `main.tex`, each available `supplement.tex`, and transitively included manuscript `.tex` sources.
- Excluded: LaTeX comments, inactive `\iffalse` blocks, `author.md`, review reports, experiment logs, and files not included by a paper.

## Rules applied

The audit flags manuscript text that:

1. introduces an imaginary reader, reviewer, referee, skeptic, or requester;
2. tells readers how to interpret the evidence instead of stating the evidence boundary;
3. exposes drafting, review, submission, appendix-placement, internal `Round-N`, or revision history;
4. narrates cluster availability, SSH/Slurm choices, retries, OOMs, host contention, or other execution history without a necessary inferential consequence;
5. leaves pending results, placeholders, future-tense tables, or outcome-contingent claims in the paper;
6. uses combative, dramatic, colloquial, or self-adjudicating wording for results;
7. narrates preregistration failures, post-hoc analyses, stopping outcomes, missing runs, or non-convergence as a confession or research diary.

Rule 7 does **not** authorize deleting those facts. They must remain, but should be stated neutrally together with their inferential consequence.

## Executive summary

- All 23 projects contain at least one actionable wording issue under these rules.
- The eight example passages supplied by the user were not found verbatim in the current included sources.
- One project is still an intentional skeleton draft:
  `wacv2027-post-training-quantization-for-visual-autoregressive-generation`.
  It contains **123 active `\ARnum`/`\ARfig` calls** outside the macro-definition file, renders `??` and `FIGURE PLACEHOLDER`, and contains future-tense result scripts. It is not a submission-ready paper.
- The most common issue is not missing science but leaked production history:
  `Round-N`, “earlier/original/retained/corrected/retracted,” reviewer requests,
  paper-placement decisions, failed infrastructure attempts, and cluster-launch details.
- Neutral disclosures of post-hoc status, seed divergence, missing checkpoints, and failed controls were not flagged merely for being negative. They were flagged only where the wording becomes diary-like, defensive, or editorial.

### Priority legend

- **P0 — blocking:** unfinished or asserts unavailable results.
- **P1 — rewrite before submission:** clear reviewer/process leakage, reader direction, research diary, or materially inappropriate rhetoric.
- **P2 — cleanup:** lower-risk editorial or self-adjudicating wording.

## Project overview

| Project | Pipeline stage | Highest priority | Main issue classes |
|---|---:|---:|---|
| `eccv-do-feed-forward-3d-foundation-models-memorize-their-training-scenes-a-memb` | await final review | P1 | infrastructure diary, anonymous-submission meta-talk |
| `eccv-localization-without-leakage-adversarial-anti-reconstruction-training-for` | delivered | P1 | reviewer persona, revision history, dramatic null wording |
| `eccv-how-much-spatial-reasoning-is-one-linear-layer-away-frozen-3d-foundation-f` | delivered | P1 | failed-attempt diary, reviewer persona, directive interpretation |
| `eccv-making-continuous-visual-thought-tokens-load-bearing-grounding-objectives` | loop | P1 | post-hoc control diary, co-tenant/OOM narrative |
| `wacv2027-ugc-quality-score-driven-dpo-for-ugc-video-enhancement` | await final review | P1 | headline/submission strategy, earlier-screen history |
| `wacv2027-post-training-quantization-for-visual-autoregressive-generation` | await draft review | **P0** | 123 active placeholders, pending experiments |
| `wsdm2027-semantic-id-tokenizers-that-peek-test-set-leakage-in-semantic-id-gener` | delivered | P1 | imaginary requester, framing/placement history |
| `wacv2027-open-set-recognition-with-safe-abstention-for-surgical-scene-understan` | delivered | P1 | internal Round, abandoned-claim history |
| `wsdm2027-llm-knowledge-cutoff-contamination-in-llm-relevance-judgments` | delivered | P1 | pilot chronology, protocol-edit and GPU diary |
| `wsdm2027-semantic-id-computational-limits-of-generative-retrieval-over-semantic` | delivered | P2 | pilot/placement vocabulary |
| `wacv2027-retrieval-memory-agents-for-long-form-procedural-video-understanding` | delivered | P1 | reviewer request, rounds, unavailable rerun, missing lanes |
| `wacv2027-differentially-private-personalization-for-gaze-estimation` | delivered | P1 | Round/historical vocabulary, combative “anti-DP” |
| `wacv2027-provable-low-rank-feature-geometry-for-out-of-distribution-detection` | delivered | P1 | review/revision history, self-correction narrative |
| `wacv2027-verifiable-reward-rl-for-causal-video-anomaly-reasoning` | delivered | P1 | pervasive Round/history labels, feasibility diary |
| `wsdm2027-one-step-suffices-diffusion-recommendation-distillation-with-provable` | delivered | P1 | withdrawn-port diary, cluster-launch details |
| `wacv2027-diffusion-score-smoothness-for-open-set-test-time-adaptation` | delivered | P1 | retraction/correction narrative, internal launch rounds |
| `wacv2027-presentation-only-attacks-for-auditing-synthetic-image-forensics-detec` | delivered | P1 | reviewer-requested control, GPU/pilot history |
| `wacv2027-score-statistic-forensics-for-diffusion-generated-video-detection` | delivered | P1 | post-selection diary, reviewer alternative |
| `wsdm2027-how-small-can-you-go-sample-complexity-limits-of-graph-condensation-fo` | delivered | P1 | critique/request framing, Round/GPU/SSH history |
| `wsdm2027-llm-when-the-web-is-llm-written-auditing-click-signals-as-relevance-pr` | delivered | P1 | extensive old/earlier/revised/reviewer framing |
| `wsdm2027-is-scale-necessary-capacity-lower-bounds-for-collaborative-filtering` | delivered | P2 | internal Round census |
| `wsdm2027-risk-sensitive-diversification-without-an-oracle` | delivered | P2 | revision/headline/anti-cherry-picking wording |
| `wacv3-modality-contrastive-decoding-removing-dominant-modality-bias-in-omnimoda` | delivered | P1 | reader direction, dramatic language, extensive process diary |

## Detailed findings

Paths below are relative to each project directory.

### 1. Geometry membership audits

Project: `eccv-do-feed-forward-3d-foundation-models-memorize-their-training-scenes-a-memb`

- **P1 — execution diary:** `work/manuscript/sections/03_method.tex:310-311`;
  `sections/06_appendix.tex:166-177,248-254`.
  The paper recounts a paused/resumed run, a batch-8 OOM, DataLoader semaphore
  loss, CPU-block changes, a retry, and a paused attempt.
  Replace with the inferentially relevant facts, for example:
  “Resource totals include all accelerator time for retained records and one
  unsuccessful batch-size-8 allocation that produced no checkpoint.”
- **P1 — submission/internal-version meta-talk:**
  `sections/06_appendix.tex:243-244`:
  “exact internal revisions … intentionally omitted from the anonymous manuscript.”
  Rewrite: “The experiment record maps run labels to immutable source and
  raw-shard digests.”
- **P1 — future/defensive framing:** `sections/06_appendix.tex:55`:
  “the planned next experiment rather than a limitation of the design.”
  Rewrite: “Transfer to other scene families remains unevaluated.”
- **P2 — self-adjudicating/colloquial:** `sections/03_method.tex:102`,
  `sections/04_experiments.tex:121,138`, `sections/06_appendix.tex:396`
  (“upper hand,” “manufacture signal,” “settles the pose question,” “rescuing”).
  State the measured comparison directly.

### 2. Adversarial anti-reconstruction training

Project: `eccv-localization-without-leakage-adversarial-anti-reconstruction-training-for`

- **P1 — imaginary reviewer:** `work/manuscript/sections/01_introduction.tex:81-82`:
  “the axes a defense reviewer would probe.”
  Rewrite: “This null persists across head-capacity, inner-search-strength,
  indoor-scene, and adaptive-attack checks.”
- **P1 — internal correction history:** `sections/04_experiments.tex:52-57,118`
  (“after diagnosing the fallback solver,” “repository remained empty,”
  “replace the broken fallback utility axis”).
  Preserve post-hoc status but write:
  “These absolute checks were introduced post hoc after the fallback solver
  proved inadequate; they are exploratory. Official DSAC* measurements
  supersede the fallback-solver utility results.”
- **P2 — infrastructure commentary:** `sections/04_experiments.tex:228`,
  “despite shared-cluster contention.” Remove the cluster explanation from the
  result sentence.
- **P2 — abandoned construction/history:** `sections/06_appendix.tex:92-96,111-115,207`;
  `tables/round3_indoor.tex:4`. Describe the diagnostic scope and final figure
  construction without “earlier,” discarded generations, or old weaknesses.
- **P2 — dramatic null language:** `sections/00_abstract.tex:6,18-23,28-29`;
  `sections/05_conclusion.tex:8-14`; `tables/round7_ksweep.tex:4`
  (“buys anything,” “fare worse,” “fails outright,” “genuine mechanism”).
  Replace with measured advantage/no-advantage statements.

### 3. Frozen-feature spatial reasoning

Project: `eccv-how-much-spatial-reasoning-is-one-linear-layer-away-frozen-3d-foundation-f`

- **P1 — failed-attempt/erratum diary:** `work/manuscript/sections/06_appendix.tex:60-76`.
  It recounts a stopped parser attempt, shared-GPU OOM, rerun, initial
  miscount, assertion failure, and correction.
  Rewrite around impact:
  “The parser excludes numeric rows with null options. A metadata count was
  corrected before feature extraction or scoring; the split rule was unchanged.”
- **P1 — imaginary reviewer:** `sections/06_appendix.tex:214`:
  “A reviewer-level concern is whether …”
  Rewrite: “One potential confound is a shift in option-letter marginals.”
- **P1 — internal artifact history:** `sections/06_appendix.tex:91-93`,
  “failed attempts … in `EXPERIMENT_DETAILS.md`.”
  Rewrite: “The reproducibility record contains immutable hashes and software versions.”
- **P2 — directive/rhetorical interpretation:** `sections/00_abstract.tex:13-22`;
  `sections/04_experiments.tex:58,70,117,163,169-170`;
  `sections/05_conclusion.tex:13-16`
  (“decisive,” “remove two rescue stories,” “wrong task,” “bought with,”
  “re-reads as”).
  Replace with effect sizes and “is consistent with” language.
- **Not a deletion target:** the one-of-eight seed divergence and absence of a
  predeclared seed-failure policy are scientifically relevant. The concise,
  neutral version should remain.

### 4. Load-bearing continuous visual tokens

Project: `eccv-making-continuous-visual-thought-tokens-load-bearing-grounding-objectives`

- **P1 — post-hoc controls written as a diary:**
  `work/manuscript/sections/00_abstract.tex:19`;
  `sections/01_introduction.tex:67-69`;
  `sections/03_method.tex:196-199`;
  `sections/04_experiments.tex:29-33`;
  `sections/05_conclusion.tex:5`.
  Consolidate once:
  “The prespecified forced-self control remained at chance. Expert- and
  label-oracle analyses were specified afterward and are treated as
  exploratory diagnostics.”
- **P1 — infrastructure narrative:** `sections/04_experiments.tex:41-47`;
  `sections/06_appendix.tex:27-33`:
  “bare-metal takeovers,” “co-tenant OOMs,” and downstream jobs not launched.
  Rewrite:
  “Infrastructure failures prevented checkpoints for nine assignments.
  Analyses use 13 completed LBG seeds, no imputation, and exact paired-seed
  intersections.”
- **P1 — anonymity/submission meta-talk:** `sections/06_appendix.tex:47,176-180`.
  Replace “outside the anonymous manuscript” and “omitted here to preserve
  anonymity” with a direct description of the reproducibility record.
- **P2 — editorial terms:** `sections/03_method.tex:210`,
  `sections/04_experiments.tex:51`, `sections/06_appendix.tex:9-10`
  (“headline,” “title and conclusions are therefore about”).
  Use “primary” and state scope directly.

### 5. Quality-score preference audit

Project: `wacv2027-ugc-quality-score-driven-dpo-for-ugc-video-enhancement`

- **P1 — anonymous-submission/internal-detail language:**
  `work/manuscript/sections/06_appendix.tex:35,43-44,458-459`.
  Rewrite: “The reproducibility record binds the dataset snapshot, source
  revisions, checkpoint digests, machine identifiers, and environment.”
- **P2 — editorial “headline” strategy:** `sections/00_abstract.tex:16-18`;
  `sections/03_method.tex:160-162`;
  `sections/05_conclusion.tex:39,84`;
  `sections/06_appendix.tex:283`.
  Replace “headline” with “primary analysis/inference.”
- **P2 — earlier-screen and placement history:** `sections/01_introduction.tex:82-85`;
  `sections/03_method.tex:147`; `sections/04_experiments.tex:174-175`;
  `sections/06_appendix.tex:69-70,359-360`.
  State directly that the exploratory abstention procedure failed calibration
  and power checks and supports no selective-reliability inference.
- **P2 — self-adjudicating headings:** `sections/05_conclusion.tex:4,104`
  (“What the identity result means,” “What the audit establishes”).
  Use “Scope of the identity result” and “Observed implications.”

### 6. ScaleProp quantization draft

Project: `wacv2027-post-training-quantization-for-visual-autoregressive-generation`

- **P0 — unfinished evidence:** 123 active `\ARnum`/`\ARfig` calls occur in:
  `work/manuscript/sections/00_abstract.tex`,
  `01_introduction.tex`, `03_method.tex`, `04_experiments.tex`,
  `05_conclusion.tex`, and `06_appendix.tex`.
  `ar_macros.tex` sets `\ARdrafttrue`, so these render as `??` and
  `FIGURE PLACEHOLDER`.
- Representative blocking locations:
  `00_abstract.tex:18-20`;
  `01_introduction.tex:90-93,107-108`;
  `03_method.tex:258-260`;
  `04_experiments.tex:107-111,192-200,255-260,295-305,315-340,347-364`;
  `05_conclusion.tex:7-10`;
  `06_appendix.tex:122-124`.
- **P0 — future execution plan in the manuscript:**
  `04_experiments.tex:94,105,259-260,366-367`;
  `06_appendix.tex:53-74,100-160,171-186`
  (“will answer,” “paper will claim,” “Every run will write,”
  “Extended Experimental Plan,” “supplement will give”).
  No wording-only fix exists: run the experiments and report past-tense
  measurements, or remove unsupported claims and tables.
- **P1 — submission/cherry-picking rhetoric:** `06_appendix.tex:87-89`;
  `04_experiments.tex:352-355`.
  Use “reproducibility record” and “selected by a prespecified random rule.”

### 7. Semantic-ID tokenizer leakage

Project: `wsdm2027-semantic-id-tokenizers-that-peek-test-set-leakage-in-semantic-id-gener`

- **P1 — imaginary requester:** `work/manuscript/sections/04_experiments.tex:133`,
  “The requested intervention therefore confirms …”
  Rewrite: “The intervention confirms protocol exposure and upstream
  overfitting, but not a robust downstream score effect.”
- **P2 — draft/placement history:** `sections/04_experiments.tex:61,186`
  (“not the previous framing,” “move this low-seed result to the appendix”).
  State the absolute paired gap directly and label the three-seed analysis
  descriptive.
- **P2 — “promised metrics”:** `sections/04_experiments.tex:23`;
  `sections/06_appendix.tex:38`; `generated/letter_all_metrics_table.tex:6`.
  Replace “promised” with “prespecified.”

### 8. Surgical-scene open-set recognition

Project: `wacv2027-open-set-recognition-with-safe-abstention-for-surgical-scene-understan`

- **P1 — abandoned-claim history:** `work/manuscript/sections/04_experiments.tex:49-53`
  (“now a diagnostic,” “Earlier … pilot diagnostics”).
  State current scope: “SORCA is evaluated only as a diagnostic fusion.”
- **P1 — internal round/pilot:** `sections/06_appendix.tex:3-4`,
  “final Round-5 estimand,” “earlier CLIP pilot.”
  Rewrite: “This supplement reports the matched ResNet-50 estimand; CLIP
  diagnostics are outside that estimand.”
- **P2 — confessional post-hoc/anti-cherry-picking:** `sections/04_experiments.tex:39,109`.
  Preserve retrospective status, but state that intervals are descriptive;
  replace “rather than selecting favorable rows” with the prespecified grid.
- **P2 — submission packaging:** `sections/06_appendix.tex:7`,
  “The main submission includes …” Rewrite as a self-contained evidence statement.

### 9. LLM knowledge-cutoff contamination

Project: `wsdm2027-llm-knowledge-cutoff-contamination-in-llm-relevance-judgments`

- **P1 — internal protocol-edit history:** `work/manuscript/sections/03_method.tex:178`;
  `sections/06_appendix.tex:148`.
  Rewrite: “The data, design, and inferential registry were frozen before
  outcomes; ranking utility is outside scope and ancillary checks are exploratory.”
- **P1 — pilot/control diary:** `sections/06_appendix.tex:14,38,308-359`;
  plus `sections/01_introduction.tex:48`, `02_related_work.tex:78-80`,
  `03_method.tex:40`, `04_experiments.tex:115`.
  Replace first/earlier/pilot chronology with named analyses and their
  inferential limits.
- **P1 — infrastructure details:** `sections/06_appendix.tex:156,180-183`
  (“re-verified idle,” “no scheduler job,” “failed infrastructure attempts”).
  Rewrite: “Each trajectory uses one H100; GPU-hours exclude environment
  setup, aggregation, and plotting.”
- **P2 — defensive rhetoric:** `sections/01_introduction.tex:54,113`;
  `sections/02_related_work.tex:65`; `sections/04_experiments.tex:193`.
  Report heterogeneity and separate estimands directly.

### 10. Computational limits of generative retrieval

Project: `wsdm2027-semantic-id-computational-limits-of-generative-retrieval-over-semantic`

- **P2 — manuscript placement/history:** `work/manuscript/sections/01_introduction.tex:49`;
  `sections/06_appendix.tex:1325`
  (“retained as an appendix diagnostic,” “retained only to disclose”).
  State that the certificate is diagnostic and that weak-fidelity controls do
  not support the mechanism.
- **P2 — pilot seed chronology:** `sections/06_appendix.tex:835,1080,1460`.
  State final tuning/evaluation seed sets directly; mention an exclusion only
  if its protocol mismatch affects inference.
- **P2 — self-adjudication/internal infrastructure:** `sections/06_appendix.tex:1425,1457,1477`
  (“replicates its strongest result,” “anonymous bundle,” “site-specific
  GPU-node wrapper”). Use measured outcomes and stable replication commands.

### 11. Retrieval-memory agents for long video

Project: `wacv2027-retrieval-memory-agents-for-long-form-procedural-video-understanding`

- **P1 — explicit reviewer request:** `work/manuscript/tables/qa_results.tex:23`,
  “the no-retrieval reference requested by reviewers.”
  Rewrite: “The direct row is the no-retrieval reference.”
- **P1 — internal round labels:** `sections/04_experiments.tex:125`;
  `sections/06_appendix.tex:94,612-642,870,880-884`;
  `tables/round08_overlap.tex:7`;
  `tables/round08_crosstask_task_construction_counts.tex:8`;
  `tables/round08_spacewalk_legacy_matched_grid.tex:7`;
  `tables/round08_spacewalk_legacy_qwen72_grid.tex:7`.
  Replace with descriptive protocol names.
- **P1 — planned/unavailable run diary:** `sections/01_introduction.tex:48`;
  `sections/06_appendix.tex:9-13`;
  `tables/round08_video_human_qa.tex:35`.
  Rewrite: “Only the archived one-frame-per-minute CLIP observer was
  available; the three-frame Qwen-VL condition was not evaluated.”
- **P1 — missing optional lanes:** `sections/03_method.tex:427`;
  `sections/06_appendix.tex:65`;
  `tables/round08_local_control.tex:13`;
  `tables/round08_video_human_qa.tex:36`.
  State that no estimates are available and restrict checkpoint scope.
- **P1 — self-certifying result:** `sections/04_experiments.tex:115-117`,
  “The honest result is therefore a failed visual pipeline.”
  Rewrite: “Observer quality was insufficient, leaving competent-observer
  effects unresolved.”
- **P2 — pervasive earlier/legacy/history and appendix-only wording:**
  `sections/01_introduction.tex:14,27,79,86,94`;
  `sections/03_method.tex:18,29,191,232,391,408,415`;
  `sections/04_experiments.tex:24,91,120,130`;
  `sections/05_conclusion.tex:18,20`;
  `sections/06_appendix.tex:13,30,46-70,95,630,880-884`.
  Name the protocol and inferential status directly.

### 12. Differentially private gaze personalization

Project: `wacv2027-differentially-private-personalization-for-gaze-estimation`

- **P1 — combative internal comparison:** `work/manuscript/sections/01_introduction.tex:84`;
  `sections/04_experiments.tex:136`, “anti-DP comparison.”
  Rewrite with the exact rate mismatch and its effect on the DP/NP ratio.
- **P1 — internal Round:** `sections/04_experiments.tex:26`;
  `sections/06_appendix.tex:235,365`.
  Replace `Round~5`/`Round~4` with “independently tuned cell,”
  “three-seed candidate sweep,” and “fixed control file.”
- **P1 — historical vocabulary throughout:** `sections/00_abstract.tex:20`;
  `sections/01_introduction.tex:97`;
  `sections/04_experiments.tex:50,173-191`;
  `sections/06_appendix.tex:188-317,387-398`.
  Use protocol names such as “shared-private-rate block-8 comparison.”
- **P2 — workspace/submission language:** `sections/06_appendix.tex:30`;
  `sections/04_experiments.tex:45,189`.
  State that external domains were not evaluated because source access was
  unavailable; reference the appendix directly.

### 13. Provable low-rank OOD geometry

Project: `wacv2027-provable-low-rank-feature-geometry-for-out-of-distribution-detection`

- **P1 — review/revision strategy:** `work/manuscript/sections/04_experiments.tex:24`,
  “in response to prior reviews; … within this revision.”
  Rewrite: “These analyses were specified after prior benchmark access but
  before their reported outcomes; they were not independently preregistered.”
- **P1 — correction/confession history:** `sections/04_experiments.tex:123,141`;
  `sections/01_introduction.tex:53,78,81`;
  `sections/05_conclusion.tex:16-18`.
  State the current supported interpretation and post-selection status
  without “original/earlier/corrected.”
- **P1 — imaginary objection:** `sections/01_introduction.tex:84`,
  “answers the sample-allocation objection.”
  Rewrite: “assesses whether covariance-sample size explains the spectral result.”
- **P2 — manuscript/self-adjudication:** `sections/01_introduction.tex:57`;
  `sections/02_related_work.tex:63`;
  `sections/03_method.tex:125,140`;
  `sections/04_experiments.tex:106,195`.
  Replace “paper’s disposition,” “retain,” “now,” and “survive/fire” with
  current estimands and measured decisions.

### 14. Verifiable-reward RL for video anomaly reasoning

Project: `wacv2027-verifiable-reward-rl-for-causal-video-anomaly-reasoning`

- **P1 — pervasive internal rounds/history:** `work/manuscript/sections/03_method.tex:188`;
  `sections/04_experiments.tex:18-23,64,92-104,218-241`;
  `sections/05_conclusion.tex:17-30`;
  `sections/06_appendix.tex:7-25,97-121,203-343,405-442`;
  several generated captions.
  Replace Round numbers, historical/new/earlier/supersedes wording with
  “3B exploratory cohort,” “full-coverage cohort,” and “matched-training family.”
- **P1 — feasibility/resource diary:** `sections/04_experiments.tex:32`;
  `sections/03_method.tex:197`;
  `sections/06_appendix.tex:298-301,425,438-442`.
  State the fixed cost-bounded sample and completed-run scope; do not narrate
  failed logs as a story.
- **P1 — review/submission/empty-packet meta-talk:** `sections/04_experiments.tex:280,283`;
  `sections/06_appendix.tex:462,468-470`.
  Rewrite: “No human annotations were collected; no human-validation evidence
  is reported. The released artifact is identified by …”
- **P2 — dramatic negative language:** `sections/00_abstract.tex:23`;
  `sections/01_introduction.tex:91`;
  `sections/04_experiments.tex:213,255-259`;
  `sections/05_conclusion.tex:58-62`
  (“rescue,” “failed diagnostic”).
  Use “does not provide evidence of improvement.”

### 15. One-step diffusion recommendation distillation

Project: `wsdm2027-one-step-suffices-diffusion-recommendation-distillation-with-provable`

- **P1 — abandoned implementation diary:** `work/manuscript/sections/01_introduction.tex:63-65`;
  `sections/04_experiments.tex:87-94`
  (“failed port,” “removed,” “withdraw it,” “retain its old numbers”).
  Rewrite: “The adapted TA-Rec port did not establish native-protocol
  conformance and is excluded from method-level inference.”
- **P1 — cluster-launch narrative:** `sections/06_appendix.tex:204-216`
  (“refreshed cluster inventory,” node-GPU pairs, device rechecks, launch
  waves, detached SSH, cleanup).
  Retain hardware and one-process-per-GPU facts; move operational commands to
  the artifact README.
- **P1 — submission/promotion strategy:** `sections/04_experiments.tex:109,166`;
  `generated/round6_primary.tex:4,46,88,130`.
  Rewrite: “All rows belong to the same prespecified 96-test family.”
- **P2 — combative/dramatic wording:** `sections/02_related_work.tex:48`;
  `sections/05_conclusion.tex:12`; `sections/06_appendix.tex:7`
  (“silently redefining,” “harder to dismiss,” “anomaly”).
  State protocol preservation and controls directly.

### 16. Diffusion-score smoothness for OSTTA

Project: `wacv2027-diffusion-score-smoothness-for-open-set-test-time-adaptation`

- **P1 — confessional correction/retraction narrative:**
  `work/manuscript/sections/00_abstract.tex:17`;
  `sections/01_introduction.tex:37-60,78`;
  `sections/04_experiments_round7.tex:127-140`;
  `sections/05_conclusion.tex:12-18`;
  `sections/06_appendix.tex:22,41-46`.
  Rewrite in current-study terms:
  “The generator mismatch inflated cross-dataset signals, and exact-array
  calibration repairs much of the HOS gap. The evidence supports a
  calibration-pipeline artifact rather than intrinsic threshold-transfer failure.”
- **P1 — internal round/scripts:** `sections/06_appendix.tex:152-190`.
  Replace “Round-7” and iteration-specific commands with stable reproduction
  entry points, or move them to the artifact README.
- **P1 — editorial primary/headline framing:** `sections/01_introduction.tex:60`;
  `sections/04_experiments_round7.tex:40,57`.
  Preserve selection bias directly:
  “Because the configuration was selected on SVHN, SVHN estimates are
  exploratory and excluded from primary inference.”
- **P2 — defensive failed-rule wording:** `sections/06_appendix.tex:376-380`.
  State that the prespecified sign reverses on SVHN and is reported without
  target-conditioned correction.

### 17. Presentation-only forensic attacks

Project: `wacv2027-presentation-only-attacks-for-auditing-synthetic-image-forensics-detec`

- **P1 — explicit review strategy:** `work/manuscript/sections/03_method.tex:148`,
  “this review-requested control was added.”
  Rewrite: “This control was specified after the measured-release analyses and
  is an exploratory sensitivity analysis.”
- **P1 — GPU/pilot/reserve diary:** `sections/03_method.tex:49-55`;
  `sections/06_appendix.tex:37`.
  Rewrite: “A score-independent 12-megapixel eligibility cap was fixed before
  evaluation; the prespecified ordering yielded a balanced cohort.”
- **P1 — internal conclusion/correction history:** `sections/01_introduction.tex:45`;
  `sections/04_experiments.tex:115`;
  `sections/06_appendix.tex:251-253,298`.
  State the current path-specific transfer result and post-hoc threshold status.
- **P2 — internal script rounds:** `sections/06_appendix.tex:154-173`.
  Prefer a stable top-level reproduction command.

### 18. Score-statistic diffusion-video forensics

Project: `wacv2027-score-statistic-forensics-for-diffusion-generated-video-detection`

- **P1 — post-selection diary:** `work/manuscript/sections/01_introduction.tex:56-59,84-90`;
  `sections/03_method.tex:94-96`;
  `sections/04_experiments.tex:75-77,197-200`;
  `sections/06_appendix.tex:12-14`.
  Consolidate:
  “Trajectory curvature was selected after inspection of TSC results, so
  Kinetics estimates are exploratory. UCF101 and SD1.5 protocols were fixed
  before evaluation.”
- **P1 — imaginary reviewer:** `sections/06_appendix.tex:65`,
  “resolves the reviewer’s alternative explanation.”
  Rewrite: “The low-pass pair tests whether the deployed VAE/U-Net resolves a
  known trajectory-curvature intervention.”
- **P1 — correction/retention rhetoric:** `sections/00_abstract.tex:7`;
  `sections/01_introduction.tex:51,69,84-106`;
  `sections/05_conclusion.tex:16-18`.
  State the controlled-clock result and evidence boundary directly.
- **P2 — dramatic/combative labels:** `sections/04_experiments.tex:173,182`;
  `sections/06_appendix.tex:18`
  (“Rejected Hypothesis,” “silently,” “do not invent”).
  Use neutral result and availability language.

### 19. Graph-condensation sample complexity

Project: `wsdm2027-how-small-can-you-go-sample-complexity-limits-of-graph-condensation-fo`

- **P1 — critique/request persona:** `work/manuscript/sections/01_introduction.tex:100`;
  `sections/04_experiments.tex:58-59`
  (“requested identifiable-projector witness,” “answer requested by the
  identifiability critique”).
  Rewrite as the technical question and measured witness.
- **P1 — internal Round/GPU/SSH/review process:**
  `sections/04_experiments.tex:77`;
  `sections/07_round6_appendix.tex:42-44,221-244`
  (“Round-8,” “no Slurm,” “concern matrix,” “direct SSH”).
  Rewrite with measured GPU-hours and artifact-validation facts only.
- **P2 — history/self-adjudication:** `sections/01_introduction.tex:90`;
  `sections/04_experiments.tex:111`;
  `sections/07_round6_appendix.tex:10,26`
  (“Earlier,” “defensible result,” “retained”).
  State the necessary floor, measured upper witness, and exploratory scope directly.

### 20. Auditing click signals when the Web is LLM-written

Project: `wsdm2027-llm-when-the-web-is-llm-written-auditing-click-signals-as-relevance-pr`

- **P1 — explicit reviewer language:** `work/manuscript/sections/03_method.tex:167`;
  `sections/04_experiments.tex:208`;
  `sections/06_appendix.tex:29,324`
  (“reviewer-requested,” “reviewer questions,” “requested by one reviewer”).
  Replace with the analysis name and evidence boundary.
- **P1 — pervasive old/earlier/revised/retained history:**
  `sections/00_abstract.tex:12`;
  `sections/01_introduction.tex:44,73-101`;
  `sections/02_related_work.tex:32`;
  `sections/03_method.tex:103,156-170`;
  `sections/04_experiments.tex:119-230`;
  `sections/05_conclusion.tex:3-26`;
  `sections/06_appendix.tex:67-465`.
  Rewrite around named estimands, not manuscript evolution.
- **P1 — evidence-removal concern:** `sections/06_appendix.tex:370`,
  “We remove the earlier downstream LTR tables because all intervals were too wide.”
  Do not hide imprecise evidence. Report the intervals or state that the
  estimates are too imprecise to inform the claim.
- **P1 — infrastructure/round narrative:** `sections/04_experiments.tex:230`;
  `sections/06_appendix.tex:443-490`
  (“resource-collision attempts,” Round-3/5, node terminations, direct SSH, no Slurm).
  Keep completed-run scope and compute accounting; remove operational storytelling.

### 21. Capacity lower bounds for collaborative filtering

Project: `wsdm2027-is-scale-necessary-capacity-lower-bounds-for-collaborative-filtering`

- **P2 — internal Round census:** `work/manuscript/sections/06_appendix.tex:552`;
  `figures/round6_census_table.tex:5`
  (“complete Round-6 census,” “Round-6 registered census”).
  Rewrite: “complete experiment census” and “registered experiment census.”
- **P2 — internal source identity wording:** `sections/06_appendix.tex:478`.
  Rewrite: “Per-run executed-source hashes define code identity and are
  verified across runs.”

### 22. Risk-sensitive diversification

Project: `wsdm2027-risk-sensitive-diversification-without-an-oracle`

- **P2 — revision language:** `work/manuscript/sections/04_experiments.tex:59`,
  “resolves the previously confusable rates.”
  Rewrite: “distinguishes the primary and factorial estimands.”
- **P2 — headline/placement language:** `sections/06_appendix.tex:798`,
  “not the empirical headline.”
  Rewrite: “These are implementation checks and do not support empirical-effect claims.”
- **P2 — anti-cherry-picking defense:** `sections/06_appendix.tex:806`,
  “rather than selecting favorable radii.”
  Rewrite: “Table … reports every cell in the prespecified matched-control family.”

### 23. Modality-contrastive decoding

Project: `wacv3-modality-contrastive-decoding-removing-dominant-modality-bias-in-omnimoda`

This manuscript has the densest concentration of the targeted style.
Technical uses of “reader” as the name of a routing component are not findings.

- **P1 — reader direction / imaginary skeptic:**
  `work/manuscript/sec/1_intro.tex:98`;
  `sec/6_appendix.tex:40,101-103`.
  Examples: “controls a skeptic needs,” “The correct reading is conditional,”
  “should not be read as.”
  Rewrite the conditions directly.
- **P1 — dramatic/pejorative language:** `sec/0_abstract.tex:9,16,27,38`;
  `sec/4_experiments.tex:19,43,149,161,178,215,219,240,245,279,356,412,431`;
  `sec/5_conclusion.tex:7-9`;
  `sec/6_appendix.tex:5,16,69-121,425,593,601,615,842-844,923-931`.
  Repeated terms include “deployable finding,” “promiscuous,” “catastrophic,”
  “survives,” “cheapest competitor,” “closes it,” “nothing left to expose,”
  “verdict,” and “wins only by destroying less.”
  Replace each with effect sizes and neutral support/non-support statements.
- **P1 — extensive internal phases/rounds/git/host/GPU diary:**
  `sec/1_intro.tex:39`;
  `sec/3_method.tex:67`;
  `sec/4_experiments.tex:7-16,37,159,190,284-286`;
  `sec/6_appendix.tex:16,32,69-101,158,177-227,257-312,370-395,500-538,649-740,801,935-958,1067-1093`.
  Examples include “before each round’s inference,” frozen phases, git commits,
  host admission, H100/H200 availability, host contention, restarts, smoke
  indices, deviation logs, “submission-time archive,” and Round-2 through
  Round-9 artifact names.
  Replace with development/evaluation split definitions, stable protocol names,
  concise deviations and their inferential effects, and artifact links.
- **P1 — missing/unevaluated work and self-adjudication:**
  `sec/2_related.tex:46-51`;
  `sec/4_experiments.tex:381,431`;
  `sec/6_appendix.tex:121,395,601`.
  State unavailable evaluations and validation limits directly; do not say a
  proxy was “withdrawn,” a comparison is “not a method win,” or a negative
  “must match the nuisance.”
- **P1 — negative evidence to preserve but neutralize:**
  `sec/4_experiments.tex:356`;
  `sec/6_appendix.tex:16,593,935-958`.
  Report the prespecified target, estimate, interval, and whether it was
  supported. Do not use “refuted at scale,” “survive,” or “verdict.”

## Cross-paper replacement patterns

Use these transformations consistently:

| Avoid | Prefer |
|---|---|
| “a reviewer/referee/skeptic would ask” | state the technical confound or robustness question directly |
| “reviewer-requested” | name the analysis or control |
| “the correct reading is” | state the condition and evidence boundary |
| “Round-7/8,” “earlier/original/legacy/historical” | stable descriptive protocol names |
| “we added after it failed” | “specified post hoc; treated as exploratory” |
| “we retained/withdrew/moved to appendix” | state the analysis scope and inferential status |
| “headline,” “appendix-only,” “main submission” | “primary analysis,” or omit placement language |
| “OOM, idle GPU, SSH, no Slurm, host contention” | hardware, completed-run scope, missingness, and compute totals only |
| “rescue/survive/fire/catastrophic/promiscuous” | measured effect and supported/not-supported conclusion |
| “honest/defensible result” | direct result plus uncertainty |
| “not cherry-picked / no favorable row selected” | “prespecified” or “all cells are reported” |

## Acceptable disclosures that should remain

The following are not defects when written neutrally:

- a preregistered criterion was not met;
- an analysis was specified after observing an earlier result and is exploratory;
- one or more seeds failed to converge;
- assignments produced no checkpoint and were not imputed;
- a stopping criterion was triggered;
- a negative or null result does not support the proposed mechanism;
- hardware model, measured GPU-hours, and latency are reported as scientific cost metrics.

Recommended template:

> The prespecified control did not satisfy criterion \(C\). Analyses specified
> after this result are labeled exploratory. One of \(n\) seeds did not
> converge and remains in the prespecified aggregate; consequently, the
> evidence does not establish claim \(P\).

## Suggested automatic lint checks

These searches are suitable for a first-pass manuscript lint, followed by
contextual review:

```text
\b(reviewer|referee|skeptic)\b
reviewer-requested|requested by (a|one|the) reviewer
\bRound[-~ ]?[0-9]+\b
correct reading|should not be read|reader should
headline|appendix-only|main submission|anonymous manuscript
idle H100|no Slurm|direct SSH|host contention|co-tenant|bare-metal
\b(earlier|original|historical|legacy|retained|withdrawn|retracted|superseded)\b
\b(rescue|survive|catastrophic|promiscuous|verdict|defensible|honest result)\b
\\AR(num|fig|TODO)
```

Matches in comments, labels, paths, mandatory template headers, and technical
component names must be excluded before reporting.

## Recommended remediation order

1. Finish or withhold the ScaleProp draft; no style pass can repair unavailable results.
2. Remove reviewer/referee/reader personas and explicit review-request language.
3. Replace rendered `Round-N` and revision chronology with stable experiment names.
4. Compress infrastructure stories to completed-run scope, missingness policy,
   hardware, and measured cost.
5. Consolidate post-hoc disclosures once per analysis and preserve their
   inferential status.
6. Replace dramatic/self-adjudicating language with estimates, uncertainty,
   and support/non-support statements.
