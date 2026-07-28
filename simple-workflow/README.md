# simple-workflow

Pick one ClinicalTrials.gov NCT id from a fixed list of five studies. One human
step, then the run completes.

- Definition: [`src/simple-workflow.wd.json`](src/simple-workflow.wd.json)
- Namespace: `vedha` on `https://cdisc.mediforce.ai`

## Graph

```text
select-study (human, CM0) ──► done (terminal)
```

| Step | Type | Executor | Notes |
|------|------|----------|-------|
| `select-study` | `creation` | `human` | One required param `nctId`, rendered as a dropdown because the param declares `options`. Restricted to role `operator`. |
| `done` | `terminal` | `human` | End state. No task is created — the engine marks the run `completed` as soon as a terminal step is the routing target. |

A terminal step is not optional: `validateStepGraph` rejects a definition with
no `type: terminal` step.

`selection` is deliberately **not** used. It is only valid on `type: review`
steps, and it presents options taken from an upstream step's output array — this
workflow has no upstream step, and the list of NCT ids is fixed at authoring
time. A human `param` with `options` is the mechanism for a fixed list.

## Trigger

`manual`, with no `triggerInput` — the run starts empty and the operator makes
the choice inside the first step.

## Output contract

`select-study` completes with kind `params`, so its step output is the param
map:

```json
{ "nctId": "NCT02573259" }
```

Readable downstream as `${steps.select-study.nctId}`.

The five selectable ids: `NCT02511184`, `NCT02563548`, `NCT02573259`,
`NCT04672460`, `NCT04822298`.

## Env and secrets

None. The workflow declares no `env`, references no `{{SECRET_NAME}}`, and
needs no namespace or workflow secrets.

## Agents, MCPs, Docker images, skills

None. There is no `agent`, `script`, `cowork`, or `action` step, so there is no
Agent Definition, no Tool Catalog entry, no custom image (not even
`mediforce-golden-image`, which is only pulled for container steps), and no
`externalSkillsRepo`. Nothing in golden-rules §2 (pinning), §3 (Docker), §4
(skills), or §7 (MCP governance) applies — this package has no runtime sources
to pin.

## Manual platform setup

One item: the acting user must hold the `operator` role in namespace `vedha`,
or `select-study` will present no claimable task.

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
`Select Study` task, choose `NCT02573259` from the dropdown, and submit. The run
should reach `completed` with `select-study` output `{"nctId": "NCT02573259"}`.
