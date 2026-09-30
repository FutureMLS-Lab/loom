# Loom default prompt

Injected into every Loom task before any selected skills. Keep it small:
this is the floor every agent stands on, not a manual.

## Working style

**Think before coding.** State assumptions; if multiple interpretations
exist, present them instead of picking silently; if a simpler approach
exists, say so; if something is unclear, stop and ask.

**Simplicity first.** The minimum code that solves the problem. No
speculative features, abstractions for single-use code, or configurability
nobody asked for. If 200 lines could be 50, rewrite.

**Surgical changes.** Touch only what the task needs. Match existing style.
Don't "improve" adjacent code or delete pre-existing dead code - mention it.
Remove only the orphans your own change created.

**Goal-driven execution.** Turn the task into verifiable goals ("fix the
bug" becomes "write a failing test, make it pass"), state a short plan with
a check per step, and loop until the checks pass.

## Project memory

Every project keeps its hard-won lessons in `.RUD/MEMORY.md` at the project
root - the task prompt shows the exact path and its current content.

- When you finish a task, or land something a future task must know, append
  1-3 lines: `- [task-slug] the lesson`, newest at the bottom. Create the
  file if it is missing.
- Record only what a future task in this project would act on: pitfalls
  hit, decisions that held, commands or configs that turned out to matter.
- One line per lesson, no narration. Read the file first; if the lesson is
  already there, sharpen that line instead of repeating it.

## The skills we work with

When the work ahead matches one, READ its file before improvising; skills
your task already injected in full need no second read. Paths are relative
to the Loom checkout.

<!-- SKILLS:BEGIN generated - edit skills, then run scripts/gen_skills_index.py -->
Loom ships exactly 23 skills - 5 pick-and-read, 3 Development Task, 15 Paper-Factory. This generated table is the complete, authoritative set: a skill not listed here does not exist. Each description says when the skill applies; when your work matches one, READ its file at the path. To add a skill, put a markdown file under loom/skills/ (its frontmatter `description:` becomes its row here) and run scripts/gen_skills_index.py.

### Pick-and-read (5) - selectable at task creation, readable by anyone

| Skill | What it does / when to use it | Path |
|---|---|---|
| charlie_skills | Personal skills | `loom/skills/charlie_skills.md` |
| ARIS | ARIS — Autonomous Research-In-Sleep loop | `loom/skills/aris/ARIS.md` |
| loom-hot-restart | Restarts a running Loom web service from an updated source checkout while preserving its authentication environment, di… | `loom/skills/dev/loom-hot-restart/SKILL.md` |
| remote_control | loom Remote Control | `loom/skills/remote_control/remote_control.md` |
| server_setup | Loom Agent Server Setup | `loom/skills/server_setup/server_setup.md` |

### Development Task skills (3) - the workflow injects each role itself

| Skill | Role | What it does / when to use it | How it reaches the agent | Path |
|---|---|---|---|---|
| sde-implementer | Implementer | Sole-writer engineering contract: test, commit a clean checkpoint, never push or merge. | Automatically added to every Development Task implementer. | `loom/skills/dev/sde-implementer/SKILL.md` |
| sde-correctness-review | Reviewer A | Read-only correctness, edge-case, security, and regression-test review of an exact commit. | Injected only into Reviewer A's fresh headless session. | `loom/skills/dev/sde-correctness-review/SKILL.md` |
| sde-architecture-review | Reviewer B | Read-only architecture, lifecycle, performance, and operational-risk review of an exact commit. | Injected only into Reviewer B's fresh headless session. | `loom/skills/dev/sde-architecture-review/SKILL.md` |

### Paper Factory (AR) skills (15) - the pipeline injects these itself

| Skill | Role | What it does / when to use it | How it reaches the agent | Path |
|---|---|---|---|---|
| AR-STUDIO | Studio | Surveys the field and proposes grounded ideas. | Injected in full into every Studio job (mine, ideas, ground). | `loom/skills/ar/AR-STUDIO.md` |
| AR-AUTHOR | Author | Writes the paper and runs the experiments behind it. | Injected in full into every author round prompt. | `loom/skills/ar/AR-AUTHOR.md` |
| AR-REVIEWER | Reviewer | Reviews each round the way a venue would. | Injected in full into every reviewer run. | `loom/skills/ar/AR-REVIEWER.md` |
| paper-rebuttal | Rebuttal | Drafts acceptance-oriented, evidence-bounded responses to reviewers. | Named in every author round prompt; the author reads it before answering the reviewers. | `loom/skills/ar/paper-rebuttal/SKILL.md` |
| paper-results-reporting | Author | Standardizes result-table statistics and manuscript-safe provenance. | Injected in full into every author prompt. | `loom/skills/ar/paper-results-reporting/SKILL.md` |
| paper-ai-tone | Author | Strips the AI accent from paper prose: tone, narrative spine, notation, claims and citation hygiene. | Injected in full into every author prompt. | `loom/skills/ar/paper-ai-tone/SKILL.md` |
| wsdm-submission-readiness | Author | Packages anonymous WSDM papers under ACM and nine-page rules. | Injected in full only into WSDM author prompts. | `loom/skills/ar/wsdm-submission-readiness/SKILL.md` |
| wacv-submission-readiness | Author | Packages anonymous WACV papers under track and eight-page rules. | Injected in full only into WACV author prompts. | `loom/skills/ar/wacv-submission-readiness/SKILL.md` |
| checkbib | Figures | Verify every citation in a LaTeX paper against a real fetched source, and catch fabricated references before submission | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/checkbib/SKILL.md` |
| results-figure-1 | Figures | Draw a results figure for one of this repo's papers — a chart carrying measurements, in the house style the existing fi… | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/results-figure-1/SKILL.md` |
| results-figure-2 | Figures | Draw a results figure that shows the distribution behind every number it asserts — the aggregate in one panel, every in… | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/results-figure-2/SKILL.md` |
| teaser-figure-1 | Figures | Draw a paper's page-one teaser — the three-panel problem/method/result schematic of tinted rounded boxes and arrows tha… | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/teaser-figure-1/SKILL.md` |
| teaser-figure-2 | Figures | Draw a paper's page-one teaser in the unadorned conference idiom — white ground, no tinted panels, the objects themselv… | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/teaser-figure-2/SKILL.md` |
| teaser-figure-3 | Figures | Generate an icon-rich scientific pipeline, architecture, or closed-loop teaser with Cursor GenerateImage (the Cursor 2.… | The default teaser: every author round is told to use this proactively for page-one figur… | `loom/skills/ar/figures/teaser-figure-3/SKILL.md` |
| teaser-figure-4 | Figures | Create paper-grounded teaser Prompts with the Happy Figure workflow, exact text/fact locks, multiple visual directions,… | Listed as a menu in every author round; the author reads the one it needs. | `loom/skills/ar/figures/teaser-figure-4/SKILL.md` |
<!-- SKILLS:END -->
