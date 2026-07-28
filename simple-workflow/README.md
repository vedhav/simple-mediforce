# simple-workflow

Pick one ClinicalTrials.gov study from a fixed list of five, then download every
document that study has posted (protocol, SAP, ICF — whatever is there) as run
output files.

- Definition: [`src/simple-workflow.wd.json`](src/simple-workflow.wd.json)
- Namespace: `vedha` on `https://cdisc.mediforce.ai`

## Graph

```text
select-study (human, CM0) ──► fetch-documents (script, CM0) ──► done (terminal)
```

| Step | Type | Executor | Notes |
|------|------|----------|-------|
| `select-study` | `creation` | `human` | One required param `nctId`, rendered as a dropdown because the param declares `options`. Restricted to role `operator`. |
| `fetch-documents` | `creation` | `script` (`script-container`) | Runs `fetch_study_documents.py` in a custom image. 10-minute timeout. Fails the step on any unrecoverable API or download error. |
| `done` | `terminal` | `human` | End state. No task is created — the engine marks the run `completed` as soon as a terminal step is the routing target. |

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

## Data sources

| Purpose | Endpoint |
|---------|----------|
| Which documents a study posted | `https://clinicaltrials.gov/api/v2/studies/{nctId}?fields=documentSection` → `documentSection.largeDocumentModule.largeDocs[]` |
| Document bytes | `https://cdn.clinicaltrials.gov/large-docs/{last 2 chars of nctId}/{nctId}/{filename}` |

Both are public and unauthenticated. Downloads retry 3 times with linear
backoff before failing the step.

## Env and secrets

No secrets. The workflow declares no `env` and references no `{{SECRET_NAME}}`.

| Name | Secret | Scope | Used by | Meaning | How to set | Example |
|------|--------|-------|---------|---------|------------|---------|
| `MEDIFORCE_OUTPUT_DIR` | no | not set in production | `fetch-documents` | Overrides the script's output directory. Exists so the test suite can run the real script outside a container; **leave unset** on the platform, where it correctly defaults to `/output`. | Not set — test-only | `/tmp/scratch` |

`RUN_ID`, `STEP_ID`, and `MEDIFORCE_RUN_NAMESPACE` are injected into every
script container by the runtime; this script does not read them.

## Docker image

`Dockerfile` builds from `mediforce-golden-image` and copies `scripts/` to
`/opt/simple-workflow/scripts/`. That is its only job — a `command` step can
only execute code already present in the container, so the script has to be
baked in. `fetch_study_documents.py` uses the Python standard library only, so
there is no `apt-get` or `pip install` layer.

Build mode is pinned per golden-rules §2:

```json
"script": {
  "command": "python3 /opt/simple-workflow/scripts/fetch_study_documents.py",
  "dockerfile": "simple-workflow/Dockerfile",
  "repo": "https://github.com/vedhav/simple-mediforce.git",
  "commit": "3b649d0e60624f044f1704f78c139637fac457ea",
  "timeoutMinutes": 10
}
```

**Pinning state: pinned to
`https://github.com/vedhav/simple-mediforce.git`@`3b649d0e60624f044f1704f78c139637fac457ea`.**

That commit is the one whose `simple-workflow/` tree the image builds from. It
stays reachable as an ancestor of `main` as HEAD moves on, so the build SHA is
allowed to lag HEAD — it does not need to be re-pinned for unrelated changes,
only when `Dockerfile` or `scripts/` change.

`dockerfile` is repo-root-relative; the build context is that file's own
directory, so `COPY scripts/` resolves to `simple-workflow/scripts/`. Do not
move the Dockerfile into a subfolder — the context would move with it and the
`COPY` would fail. The repo is public, so the builder clones anonymously over
HTTPS and no `repoAuth` is needed.

`FROM mediforce-golden-image` is untagged (so, `:latest`). That is the base the
platform documents in golden-rules §3 and matches the other workflows on this
deployment.

## Agents, MCPs, skills

None. There is no `agent` or `cowork` step, so there is no Agent Definition, no
Tool Catalog entry, and no `externalSkillsRepo`. Golden-rules §4 and §7 do not
apply.

## Tests

See [`tests/TEST_SUMMARY.md`](tests/TEST_SUMMARY.md).

```bash
python3 tests/run_tests.py
```

No credentials needed; requires outbound network to clinicaltrials.gov.

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
`NCT04822298_Prot_000.pdf` and `NCT04822298_SAP_001.pdf` as Output Files, then
the run should reach `completed`.
