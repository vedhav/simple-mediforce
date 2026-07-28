# simple-workflow

Pick one ClinicalTrials.gov study from a fixed list of five, download every
document that study has posted (protocol, SAP, ICF — whatever is there), have an
agent extract CDISC USDM study-definition metadata from those documents, render
its schedule of activities as an interactive table, and put both in front of a
human reviewer.

- Definition: [`src/simple-workflow.wd.json`](src/simple-workflow.wd.json)
- Namespace: `vedha` on `https://cdisc.mediforce.ai`

## Graph

```text
select-study (human, CM0) ──► fetch-documents (script, CM0) ──► generate-usdm (agent, CM4)
                                                                      │            ▲
                                                                      ▼            │ revise
                                                              render-soa (script, CM0)
                                                                      │            │
                                                                      ▼            │
                                                              review-usdm (human review)
                                                                      │ approve
                                                                      ▼
                                                                 done (terminal)
```

| Step | Type | Executor | Notes |
|------|------|----------|-------|
| `select-study` | `creation` | `human` | One required param `nctId`, rendered as a dropdown because the param declares `options`. Restricted to role `operator`. |
| `fetch-documents` | `creation` | `script` (`script-container`) | Runs `fetch_study_documents.py` in a custom image. 10-minute timeout. Fails the step on any unrecoverable API or download error. |
| `generate-usdm` | `creation` | `agent` (`claude-code-agent`) | `autonomyLevel: L4`. Reads the PDFs out of the run workspace, extracts the study definition, writes USDM v3.0.0 including the schedule of activities. 45-minute timeout, `mediforce-golden-image`. |
| `render-soa` | `creation` | `script` (`script-container`) | Runs `render_soa.py` in the same image. Turns `usdm.json` into an interactive SoA table where every gap is visible as a gap. 10-minute timeout. |
| `review-usdm` | `review` | `human` | Approve/revise gate. `revise` routes back to `generate-usdm` and requires a comment. Restricted to role `operator`. |
| `done` | `terminal` | `human` | End state. No task is created — the engine marks the run `completed` as soon as a terminal step is the routing target. |

### Why `render-soa` sits before the review and not after it

The review task's context panel renders the **previous** step's `presentation`
in a sandboxed iframe (`task-context-panel.tsx` → `SandboxedHtmlIframe` →
`buildSrcdoc`), so putting the renderer immediately upstream of `review-usdm` is
what puts the table in front of the reviewer while they decide. After the
approve verdict it would render for nobody.

Inserting it there costs nothing the old graph had. `review-usdm` is an explicit
`type: review` human step, so its own step output is
`{ verdict, reviewerComment, reviewerCallToAction }` either way
(`buildVerdictStepOutput`, `complete-human-task.ts:155-168`) — it never carried
the upstream step's result. The engine's "cannot approve, agent produced no
output" guard also does not change: that fires only when
`completionData.reviewType === 'agent_output_review'`, which only the built-in
L3 path sets (`agent-step-executor.ts:165`), never an explicit review step.

### Why the review is its own step and not `autonomyLevel: L3`

`agent` + `autonomyLevel: L3` (control mode CM3, "human review") has a built-in
review gate: `agent-step-executor.ts` pauses the run and creates an
`agent_review_l3` human task on the agent step itself, and a `revise` verdict
re-runs the agent. It looks like exactly what this workflow wants, and it is one
step instead of two — but **the reviewer's comment never reaches the agent.**

`complete-human-task.ts` builds a `reviewerCallToAction` string from the
comment, but on the L3 path `isL3Revise` is true, so `advanceStep` is skipped
(`workflow-engine.ts:757`) and the step output — comment included — is never
written to `instance.variables`. The auto-runner then re-executes the agent with
`{ ...previousStepOutput, steps: instance.variables }`
(`run/route.ts:834`), which still holds only the *previous agent output*. The
revise loop re-runs the agent blind.

An explicit `type: review` human step does not have this problem: its completion
is not `isL3Revise`, so `advanceStep` runs, `variables['review-usdm']` gets
`{ verdict, reviewerComment, reviewerCallToAction }`, and the next agent
iteration reads it from `steps['review-usdm']`. That is why `generate-usdm` is
`L4` (autonomous — the explicit step is the gate; `L3` would double-gate) and
`review-usdm` carries the verdicts. It is also the pattern `cdisc-case-3` uses
for all three of its review gates.

One consequence worth knowing: `review.maxIterations` is only enforced in
`submitReviewVerdict`, which is the *agent*-reviewer path. Human review tasks go
through `completeHumanTask`, so the revise loop here is bounded by the reviewer's
judgement and by the engine's per-step attempt cap, not by `maxIterations`. There
is no point setting that field on `review-usdm`.

A terminal step is not optional: `validateStepGraph` rejects a definition with
no `type: terminal` step.

`selection` is deliberately **not** used on `select-study`. It is only valid on
`type: review` steps, and it presents options taken from an upstream step's
output array — this workflow has no upstream step, and the list of NCT ids is
fixed at authoring time. A human `param` with `options` is the mechanism for a
fixed list.

## Trigger

`manual`, with no `triggerInput` — the run starts empty and the operator makes
the choice inside the first step.

## Output contracts

### `select-study`

Completes with kind `params`, so its step output is the param map:

```json
{ "nctId": "NCT02573259" }
```

The five selectable ids: `NCT02511184`, `NCT02563548`, `NCT02573259`,
`NCT04672460`, `NCT04822298`. All five currently have both a protocol and a SAP
posted.

### `fetch-documents`

Reads `steps['select-study'].nctId` from `/output/input.json` and writes
`/output/result.json`:

```json
{
  "nctId": "NCT02573259",
  "documentCount": 2,
  "documents": [
    {
      "filename": "NCT02573259_Prot_000.pdf",
      "sourceFilename": "Prot_000.pdf",
      "sourceUrl": "https://cdn.clinicaltrials.gov/large-docs/59/NCT02573259/Prot_000.pdf",
      "typeAbbrev": "Prot",
      "label": "Study Protocol",
      "date": "2018-08-07",
      "hasProtocol": true,
      "hasSap": false,
      "hasIcf": false,
      "sizeBytes": 6537808
    }
  ],
  "summary": "Downloaded 2 document(s) for NCT02573259"
}
```

Every document the study lists is downloaded — there is no type filter, so ICF
and combined `Prot_SAP` documents are included alongside standalone protocols
and SAPs.

A study with no posted documents is **not** an error: the step succeeds with
`documentCount: 0` and an empty `documents` array.

### The PDFs themselves

The downloaded files are written into the step's output directory as
`<nctId>_<originalFilename>.pdf`. Anything left in `/output` that is not an
engine control file is copied into `.mediforce/output/fetch-documents/` on the
run's git branch and committed, which makes it a durable **Output File** listed
under the step in the run detail view and fetchable through
`/api/agent-output-file`.

Two limits worth knowing:

- Files above `MEDIFORCE_OUTPUT_FILE_MAX_BYTES` (default 100 MiB) are skipped
  with a warning. The largest document across the five studies is ~16 MB, so
  this does not bite today.
- Run branches are never pushed — the bytes live on the deployment host and are
  reached through the UI, not from a remote repo.

This copy is also how `generate-usdm` gets the PDFs. Output Files land at
`.mediforce/output/<stepId>/` in the run's git worktree, every step of a run
mounts that same worktree at `/workspace`, so the agent reads them from
`/workspace/.mediforce/output/fetch-documents/`. The prompt does not trust that
path blindly — it runs `find /workspace /data -name '*.pdf'` first and fails with
a directory listing if nothing turns up, so a wrong assumption shows up as a
readable error instead of invented study data.

### `generate-usdm`

Writes three deliverables to **both** `/workspace/usdm/` (committed to the run
branch) and `/output/` (surfaces as this step's Output Files):

| File | Purpose |
|------|---------|
| `usdm.json` | The USDM v3.0.0 envelope — `{ usdmVersion, systemName, study }`. Real USDM class and attribute names throughout. |
| `usdm-provenance.json` | `{ nctId, entries: [{ path, sourceFile, page, quote }] }`. Required for every title, identifier, studyType, studyPhase, interventionModel, blindingSchema, arm, epoch, objective, endpoint and eligibility criterion. |
| `usdm-summary.md` | What the reviewer reads to decide approve vs revise. |

`result.json` — the step's output value — deliberately carries no USDM payload:

```json
{
  "nctId": "NCT04822298",
  "usdmVersion": "3.0.0",
  "studyTitle": "…",
  "studyPhase": "Phase III Trial",
  "sourceDocuments": [{ "filename": "…", "typeAbbrev": "Prot", "pages": 214, "usedAs": "primary" }],
  "counts": { "titles": 2, "studyIdentifiers": 1, "arms": 2, "epochs": 3, "elements": 3,
              "studyCells": 6, "objectives": 4, "endpoints": 9, "inclusionCriteria": 12,
              "exclusionCriteria": 18, "studyInterventions": 2, "activities": 0 },
  "integrityCheck": { "passed": true, "danglingReferences": [] },
  "unresolved": [{ "path": "…", "reason": "…" }],
  "outputFiles": ["usdm.json", "usdm-provenance.json", "usdm-summary.md"],
  "confidence": 0.0,
  "confidence_rationale": "…",
  "summary": "…"
}
```

`confidence` and `confidence_rationale` are not optional decoration — the agent
runtime appends a "Confidence Self-Assessment" section to every agent prompt and
requires both fields. `confidenceThreshold: 0.6` with
`fallbackBehavior: continue_with_flag` means a low-confidence extraction still
flows to `review-usdm` flagged, rather than escalating separately; the review
gate is already mandatory, so escalation would be redundant.

**USDM scope is a documented subset, not the full model.** Populated: study,
studyVersion (versionIdentifier, titles, studyIdentifiers, studyType,
studyPhase), studyDesign (interventionModel, blindingSchema, arms, epochs,
elements, studyCells, objectives with nested endpoints, population,
eligibilityCriteria, studyInterventions), the schedule of activities
(`activities`, `encounters`, `scheduleTimelines` with their `instances` and
`timings`), and documentedBy. Estimands, amendments, and biomedical concepts are
out of scope.

The schedule of activities is **required**, not best-effort, because
`render-soa` draws it. The prompt's rule is that an unreadable part is left out
*whole* rather than partly guessed: no `ScheduledActivityInstance` for an
illegible visit column, no mention in any `activityIds` for an illegible
activity row, and an `unresolved` entry either way. That makes the omission
render as UNKNOWN instead of as a confident blank — see
[`render-soa`](#render-soa) for how the renderer reads it.

The prompt forbids inventing content: anything the protocol does not state
becomes `null` plus an `unresolved` entry, and a CDISC CT code the agent is not
confident of becomes `"code": null` with the verbatim wording kept in `decode`.
A sparse-but-true USDM is the intended output; catching the opposite is what
`review-usdm` is for.

### `render-soa`

Reads `usdm.json` out of the run worktree and writes three files.

| File | Purpose |
|------|---------|
| `presentation.html` | A **body fragment**, not a document. `script-container-plugin.ts:426-440` reads it back as the step's `presentation`, and `buildSrcdoc` injects it into a document it builds itself — so a full `<!DOCTYPE html>` here would nest. `presentation.md` is preferred over HTML when both exist, so this step must not write one. |
| `schedule-of-activities.html` | The same report as a standalone document, for download. |
| `result.json` | The step output value: counts, `gaps[]`, `danglingReferences[]`, `soaComplete`. Carries no HTML. |

Both HTML files are self-contained — inline CSS and JS, no external requests.
That is a hard requirement, not tidiness: `buildSrcdoc` sets a CSP of
`default-src 'none'` with `connect-src 'none'`, so anything fetched at runtime
is blocked. Inline `<script>` and `<style>` are allowed (`script-src
'unsafe-inline'`), which is what makes the filtering and cell-detail
interactions work inside the review task. The stylesheet handles light and dark
through both `prefers-color-scheme` and the `html.dark` class the host toggles
via its `postMessage` theme sync.

#### Three cell states, and why `unknown` is the default

USDM cannot say "this cell is unknown" — an activity either is or is not listed
by a `ScheduledActivityInstance`. A renderer that maps that straight to a
two-state ✕/blank grid therefore turns **every extraction failure into a
confident "not scheduled"**, which is exactly the error a reviewer cannot catch.
So each cell resolves to one of three states, and `not scheduled` is claimed only
when the evidence for it exists:

| State | Shown as | Meaning |
|-------|----------|---------|
| scheduled | green `✕` | An instance at that encounter lists that activity. |
| not scheduled | grey `·` | The timeline covers **both** that activity and that encounter, and does not pair them. |
| unknown | amber `?` | There is no timeline at all, **or** no instance references that encounter, **or** no instance references that activity. |

An unreferenced encounter makes its whole column unknown; an unreferenced
activity makes its whole row unknown. Both are reported in `gaps`.

Everything else the renderer could not resolve lands in `gaps[]` and in a panel
above the table: missing names, visits with no resolvable `scheduledAtId`, a
`previousId`/`nextId` chain that is not one consistent chain (the table then
falls back to declaration order and says so), visits with no epoch,
`activityIds`/`encounterId` values pointing at ids the design never declares, and
activity `childIds` hierarchies that the flat table does not show. The
`unresolved[]` list from `generate-usdm`'s own output is surfaced verbatim in its
own panel.

```json
{
  "nctId": "NCT04822298",
  "studyTitle": "…",
  "usdmSource": "/workspace/usdm/usdm.json",
  "tableRendered": true,
  "soaComplete": false,
  "designs": [{ "id": "StudyDesign_1", "label": "…", "activities": 20, "encounters": 11,
                "scheduled": 80, "notScheduled": 90, "unknown": 50, "danglingReferences": 0 }],
  "gapCount": 5,
  "gaps": [{ "severity": "missing", "scope": "studyDesigns[0].activities[14]",
             "message": "no scheduled activity instance references activity 'PK blood sample', so its whole row is unknown rather than empty" }],
  "danglingReferences": [],
  "outputFiles": ["presentation.html", "schedule-of-activities.html"],
  "summary": "Rendered 20 activity(ies) x 11 visit(s), 80 scheduled, 50 unknown cell(s) and 5 gap(s) flagged for review"
}
```

`severity` is `missing` (the source said nothing) or `ambiguous` (it said
something that could not be reconciled). `soaComplete` is true only when there
are no gaps, no unknown cells, and a table was actually drawn.

**A data gap is not a step failure.** Empty `activities`, no
`scheduleTimelines`, an unreadable SoA — all of these exit 0 and render a report
that says so, because the point is for the reviewer to see the hole and hit
revise. The step fails (exit 1, `error` in `result.json` plus a bounded
worktree listing) only when `usdm.json` is absent or unparseable, which is a
broken upstream contract rather than missing study data.

The interactive controls are a filter box over activity names, a
gaps-only toggle, an outline-the-unknowns toggle, and click-a-cell-for-detail.
The gaps-only toggle deliberately matches an activity's **own** gaps — never
scheduled anywhere, or unknown at a visit that does have scheduling data — and
excludes cells that are unknown only because their whole visit column is
unknown. Without that distinction one unknown column marks every row and the
filter stops discriminating.

`usdm.json` is looked up at `/workspace/usdm/usdm.json`, then
`/workspace/.mediforce/output/generate-usdm/usdm.json`, then by walking the
worktree. Both preferred paths exist because every step of a run mounts the same
git worktree at `/workspace`: the agent writes the first directly, and the engine
commits its `/output` copy to the second (`copyOutputFilesIntoWorkspace`). The
search is the fallback for an agent that followed only part of its output
contract.

### `review-usdm`

Completes with kind `verdict`. `revise` requires a comment
(`requiresComment: true` on the verdict, `requiredForVerdicts: ["revise"]` on the
param). Step output is the agent's `result.json` plus `verdict`,
`reviewerComment`, and `reviewerCallToAction` — see the L3 discussion above for
why that matters.

Approving an agent step whose result is empty is blocked by the engine
(`Cannot approve step '…': agent produced no output`), so a failed extraction
cannot be rubber-stamped.

## Data sources

| Purpose | Endpoint |
|---------|----------|
| Which documents a study posted | `https://clinicaltrials.gov/api/v2/studies/{nctId}?fields=documentSection` → `documentSection.largeDocumentModule.largeDocs[]` |
| Document bytes | `https://cdn.clinicaltrials.gov/large-docs/{last 2 chars of nctId}/{nctId}/{filename}` |

Both are public and unauthenticated. Downloads retry 3 times with linear
backoff before failing the step.

## Env and secrets

| Name | Secret | Scope | Used by | Meaning | How to set | Example |
|------|--------|-------|---------|---------|------------|---------|
| `GITHUB_TOKEN` | yes | workflow (or namespace) | `fetch-documents` | Clone token for the Docker build context. Required **even though this repo is public** — see below. | `mediforce secret set --key GITHUB_TOKEN` | zero-scope fine-grained PAT |
| `OPENROUTER_API_KEY` | yes | workflow (or namespace) | `generate-usdm` | OpenRouter key, injected as `ANTHROPIC_AUTH_TOKEN` so the Claude Code CLI in the container routes through OpenRouter. | `mediforce secret set --key OPENROUTER_API_KEY` | `sk-or-v1-…` |
| `ANTHROPIC_BASE_URL` | no | literal in the step | `generate-usdm` | `https://openrouter.ai/api`. Hardcoded rather than a secret — it is not sensitive, and `resolveStepEnv` passes non-`{{…}}` values through verbatim. | Literal in `.wd.json` | `https://openrouter.ai/api` |
| `ANTHROPIC_API_KEY` | no | literal in the step | `generate-usdm` | Deliberately empty string. The Claude Code CLI prefers it over `ANTHROPIC_AUTH_TOKEN` when set, so leaving it unset-but-inherited would bypass OpenRouter. | Literal `""` in `.wd.json` | `""` |
| `MEDIFORCE_OUTPUT_DIR` | no | not set in production | `fetch-documents`, `render-soa` | Overrides the script's output directory. Exists so the test suite can run the real script outside a container; **leave unset** on the platform, where it correctly defaults to `/output`. | Not set — test-only | `/tmp/scratch` |
| `MEDIFORCE_WORKSPACE_DIR` | no | not set in production | `render-soa` | Same idea for the run worktree the script reads `usdm.json` out of. **Leave unset** on the platform, where it correctly defaults to `/workspace`. | Not set — test-only | `/tmp/scratch/workspace` |

Set the OpenRouter key once:

```bash
printf '%s' "<sk-or-v1-…>" | MEDIFORCE_API_KEY="$(cat ~/.config/mediforce/cdisc-key)" \
  pnpm exec mediforce secret set --key OPENROUTER_API_KEY --stdin \
  --namespace vedha --base-url https://cdisc.mediforce.ai
```

Omitting `--workflow` makes it namespace-wide, so every workflow in `vedha`
inherits it — preferable to the workflow-scoped copies that
`master-workflow` and `protocol-to-tlf` each carry today.

A missing secret is caught before any container starts: the run **pauses** at its
first step with `pauseReason` set and the missing-secret list as the error, e.g.
`[{"secretName":"GITHUB_TOKEN","template":"{{GITHUB_TOKEN}}","steps":[…]}]`.
Registration does not check secrets, so a definition referencing an unset secret
registers cleanly and only fails when run.

### Why a public repo still needs a token

`resolveImageBuild` runs every `repo` value through `normalizeRepoUrls`
(`container-plugin.ts:167`), which rewrites `https://github.com/…` to
`git@github.com:…`. **There is no anonymous-HTTPS build path.** Without a token
the builder clones over SSH using the deployment's deploy key; on
`cdisc.mediforce.ai` that key is mode 0755, which SSH rejects, so the build fails
with `Permission denied (publickey)` — an error that points at infrastructure
rather than at the missing token.

A token flips the transport back to HTTPS via `toHttpsWithToken`. Two things are
needed, and omitting **either** produces the identical SSH error:

```jsonc
"env": { "GITHUB_TOKEN": "{{GITHUB_TOKEN}}" },   // resolveStepEnv only exposes declared keys
"script": { "repoAuth": "GITHUB_TOKEN", … }
```

`repoAuth` alone silently resolves to `undefined` (`resolveRepoToken` reads
`resolvedEnv[authKey]`) and falls back to SSH without complaining.

Because the repo is public the token needs **no scopes at all** — a
zero-permission fine-grained PAT is sufficient, and preferable given the
credential lives on a shared deployment.

Set it once:

```bash
printf '%s' "<token>" | MEDIFORCE_API_KEY="$(cat ~/.config/mediforce/cdisc-key)" \
  pnpm exec mediforce secret set --key GITHUB_TOKEN --stdin \
  --namespace vedha --workflow simple-workflow --base-url https://cdisc.mediforce.ai
```

Omit `--workflow` to set it namespace-wide so every workflow in `vedha` inherits
it.

`RUN_ID`, `STEP_ID`, and `MEDIFORCE_RUN_NAMESPACE` are injected into every
script container by the runtime; this script does not read them.

## Docker image

`Dockerfile` builds from `mediforce-golden-image` and copies `scripts/` to
`/opt/simple-workflow/scripts/`. That is its only job — a `command` step can
only execute code already present in the container, so the scripts have to be
baked in. Both `fetch_study_documents.py` and `render_soa.py` use the Python
standard library only, so there is no `apt-get` or `pip install` layer.

One image serves both script steps. `fetch-documents` and `render-soa` share the
same `dockerfile` + `repo` + `commit`, and the build tag is
`sha256(repo + commit + dockerfile)` (`deriveBuildTag`, `container-plugin.ts`),
so they resolve to one tag and one build. Pinning them to different commits would
build the same image twice for no reason — keep the two SHAs equal.

Build mode is pinned per golden-rules §2:

```json
"script": {
  "command": "python3 /opt/simple-workflow/scripts/fetch_study_documents.py",
  "dockerfile": "simple-workflow/Dockerfile",
  "repo": "https://github.com/vedhav/simple-mediforce.git",
  "commit": "<the pinned SHA below>",
  "timeoutMinutes": 10
}
```

**Pinning state: both script steps pinned to
`https://github.com/vedhav/simple-mediforce.git`@`003c8cec48c59209adfad7c9826bc97bce554715`.**

That commit is the one whose `simple-workflow/` tree the image builds from — the
one that added `render_soa.py`. It
stays reachable as an ancestor of `main` as HEAD moves on, so the build SHA is
allowed to lag HEAD — it does not need to be re-pinned for unrelated changes,
only when `Dockerfile` or `scripts/` change.

`dockerfile` is repo-root-relative; the build context is that file's own
directory, so `COPY scripts/` resolves to `simple-workflow/scripts/`. Do not
move the Dockerfile into a subfolder — the context would move with it and the
`COPY` would fail. `repoAuth` **is** required despite the repo being public — see
"Why a public repo still needs a token" above.

`FROM mediforce-golden-image` is untagged (so, `:latest`). That is the base the
platform documents in golden-rules §3 and matches the other workflows on this
deployment.

`generate-usdm` needs no Dockerfile of its own: it runs `mediforce-golden-image`
directly as a prebuilt `image`, which already carries the Claude Code CLI and
`poppler-utils` (so `pdftotext`). Because that step pins no `repo`/`commit`,
nothing about it has to be re-pinned when this repo moves. The tag must exist on
the deployment's Docker host — it does, since `fetch-documents` builds `FROM` it.

## Agents, MCPs, skills

`generate-usdm` is a `claude-code-agent` step with an **inline `agent.prompt`** —
no `skill` / `skillsDir`, no Agent Definition, no MCP servers, no
`externalSkillsRepo`. The prompt is self-contained, so the step needs no custom
image and no commit pin. Move it to a `SKILL.md` when it starts being shared
across steps or grows past comfortable inline size; until then a skill would add
a plugin directory, a `skillsDir` bind-mount, and a custom image for no gain.

No governable MCP is in play, so golden-rules §7 (Tool Catalog + Agent
Definitions) does not apply. §4 does: the step declares an output contract and an
explicit `timeoutMinutes`.

The step uses the default Claude Code tool set (Bash, Read, Write, Edit, Glob,
Grep) — no `allowedTools` override. It needs no internet access: every source
document is already on disk from `fetch-documents`, and granting `WebFetch` would
let the agent pull study data from the web instead of the protocol it was told to
read.

## Tests

See [`tests/TEST_SUMMARY.md`](tests/TEST_SUMMARY.md).

```bash
python3 tests/run_tests.py
```

No credentials needed. `test_fetch_documents.py` needs outbound network to
clinicaltrials.gov; `test_render_soa.py` needs nothing and runs offline.

## Manual platform setup

Two items:

1. The acting user must hold the `operator` role in namespace `vedha`, or
   `select-study` will present no claimable task.
2. `mediforce-golden-image` must be present on the deployment's Docker host —
   this image builds `FROM` it.

## Register

From a checkout of the `mediforce` repo (reads the working tree — no commit
needed):

```bash
pnpm exec mediforce workflow register \
  --file /Users/vedha/Repo/simple-mediforce/simple-workflow/src/simple-workflow.wd.json \
  --namespace vedha
```

Or import from git (paths are repo-root-relative):

```bash
pnpm exec mediforce workflow import \
  --repo https://github.com/vedhav/simple-mediforce.git \
  --path simple-workflow/src/simple-workflow.wd.json \
  --ref main \
  --namespace vedha
```

`version` and `namespace` are assigned server-side; re-registering the same
`name` creates a new version.

## Known-good input

There is no start form. Start a run from the manual trigger, open the
`Select Study` task, choose `NCT04822298` (smallest documents, ~3 MB total) and
submit. `fetch-documents` should complete with `documentCount: 2` and list
`NCT04822298_Prot_000.pdf` and `NCT04822298_SAP_001.pdf` as Output Files.

`generate-usdm` then runs unattended for up to 45 minutes and should complete
with `integrityCheck.passed: true` and three Output Files. `render-soa` follows
in seconds and should complete with `tableRendered: true` and two Output Files.
Finally `review-usdm` creates a task for role `operator` with the SoA table
rendered in its context panel; approve it and the run reaches `completed`.

Expect the **first** run after a re-pinned `commit` to be slower than the
timeouts suggest — it pays the image build before the script starts.

```bash
# start
pnpm exec mediforce run start --workflow simple-workflow --namespace vedha --json

# pick the study
pnpm exec mediforce task list --namespace vedha
pnpm exec mediforce task complete <taskId> \
  --payload '{"kind":"params","paramValues":{"nctId":"NCT04822298"}}'

# read what the agent produced
pnpm exec mediforce run get <runId> --json
pnpm exec mediforce run files <runId>
pnpm exec mediforce run download <runId> --step generate-usdm

# read the rendered schedule of activities
pnpm exec mediforce run download <runId> --step render-soa
open schedule-of-activities.html

# approve (or send it back)
pnpm exec mediforce task complete <taskId> --payload '{"kind":"verdict","verdict":"approve"}'
pnpm exec mediforce task complete <taskId> \
  --payload '{"kind":"verdict","verdict":"revise","comment":"arms are wrong — protocol has 3, see p.24"}'
```

The completion payload key is `paramValues` / `verdict`, never `params` — the
union in `task-completion.ts` is `.strict()`, so a wrong key is rejected rather
than silently ignored.

## Verification status

| Step | Status |
|------|--------|
| `select-study` | Verified by a real run (v1–v3, `completed`). |
| `fetch-documents` | Verified by a real run (v3, run `81f01e42`, `completed`). |
| `generate-usdm` | Schema + preflight only — **no run has executed it yet.** |
| `review-usdm` | Schema + preflight only — **no run has executed it yet.** |
