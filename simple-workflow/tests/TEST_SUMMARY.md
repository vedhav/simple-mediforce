# Test summary — simple-workflow

Run everything:

```bash
python3 tests/run_tests.py
```

No secrets or credentials are needed. `test_fetch_documents.py` needs outbound
network to `clinicaltrials.gov` and `cdn.clinicaltrials.gov`; without it it
reports SKIP rather than FAIL. `test_render_soa.py` needs nothing.

| Script | Status | Asserted |
|--------|--------|----------|
| `scripts/fetch_study_documents.py` | **tested** | Happy path against the live API using `tests/fixtures/fetch-documents.input.json` (`NCT04822298`): exit 0; `result.json` has `nctId`, `documentCount == len(documents)`, `documentCount > 0`, and `summary` naming the study; every document carries `filename`, `sourceFilename`, `sourceUrl`, `typeAbbrev`, `label`, `date`, `hasProtocol`, `hasSap`, `hasIcf`, `sizeBytes`; each file exists on disk, its on-disk size equals the reported `sizeBytes`, it starts with the `%PDF-` magic bytes, and its name is prefixed with the NCT id. Failure path: step input with no `nctId` exits 1 and reports the reason in `result.json`'s `error` field. |
| `scripts/render_soa.py` | **tested** | 13 scenarios, each mutating one part of a complete in-test USDM fixture. See below. |

Last run: 2 passed, 0 skipped, 0 failed.

## What `test_render_soa.py` pins down

The property under test is **a gap must never render as a fact**. Each scenario
removes or corrupts one part of the USDM and asserts that the affected cells come
out `unknown` rather than `not-scheduled`, that the gap is named in
`result.json`, and that the HTML says so in words.

| Scenario | Asserted |
|----------|----------|
| complete schedule | exit 0; `soaComplete: true`; `gapCount: 0`; 5 scheduled / 4 not-scheduled / 0 unknown cells, matching `result.json`; `nctId` from `studyIdentifiers`; phase decoded through the `AliasCode`; timing and window rendered; epoch grouping headers present; rows in `previousId`/`nextId` chain order (not declaration order); `presentation.html` is a fragment with no `<html>`; the standalone file is a full document with a `<title>`; nothing loaded off-host |
| no `scheduleTimelines` | exit 0 (a data gap is not a step failure); all 9 cells `unknown`; **zero** cells claim scheduled or not-scheduled; the missing timeline is named in `gaps` |
| encounter with no instance | that column's 3 cells `unknown`; the visit named in `gaps`; header marked `soa-col-unknown`; **no row is flagged** — a column-wide gap must not mark every row, or the row filter matches everything |
| activity with no instance | that row's 3 cells `unknown`; the activity named in `gaps`; header marked `soa-row-unknown`; exactly 1 row flagged |
| dangling `encounterId` / `activityIds` | both reported in `danglingReferences` with their field names; a broken-reference panel in the HTML; `soaComplete: false` |
| activity with no name | rendered as `unnamed (Activity_2)` — never a bare id passed off as a name; gap raised |
| encounter with no `scheduledAtId` | column header reads `timing not stated`; gap raised |
| broken `previousId`/`nextId` chain | gap raised with severity `ambiguous`; the HTML says the shown order may not be the protocol's |
| instances with no `epochId` | grouping header reads `epoch not stated`; one bulk gap rather than one per column |
| no activities | exit 0; `tableRendered: false`; "No table could be drawn"; **no empty `<table>` rendered at all** |
| upstream `unresolved[]` | `generate-usdm`'s own reason and path surfaced verbatim |
| missing `usdm.json` | exit **1** — a broken upstream contract, unlike a data gap; `error` names the file; a worktree listing is attached for diagnosis; no report written |
| injection in source text | `</script><img src=x onerror=…>` in an activity name never appears as markup, only as escaped text |

## Verified by hand, not by the suite

The layout was checked in a real browser (Playwright, 1000px and 1280px
viewports, light and dark) against a 20-activity × 11-visit fixture:

- All visit columns render at an identical width and the activity column at
  exactly 250px — this is why the table uses `table-layout: fixed` with an
  explicit `colgroup` and a slack-absorbing spacer column. In auto layout the
  widest epoch label sets its column's width and `max-width` on a cell is
  ignored, so a one-column epoch group distorted the grid.
- The activity column stays put under horizontal scroll (sticky), and the two
  header rows stay put under vertical scroll.
- Filter, gaps-only toggle, outline toggle, the visible-count readout, and
  click-a-cell-for-detail all work; no console errors.
- Dark mode resolves through `prefers-color-scheme`.

## How the test reaches the script

`fetch_study_documents.py` resolves its output directory from
`MEDIFORCE_OUTPUT_DIR`, defaulting to `/output`. The test points that at a
scratch dir and drops the fixture in as `input.json`, which mirrors how local
mode redirects `/output` when running outside a container. The real script file
is executed — nothing is copied or rewritten.

## Not covered

These need the platform and are only exercised by a real run:

- The Docker image actually building from `simple-workflow/Dockerfile`.
- `/output` files being collected into `.mediforce/output/fetch-documents/` on
  the run branch and listed as Output Files in the UI.
- A study with zero posted documents (all five selectable studies have both a
  protocol and a SAP, so the `documentCount: 0` branch has no live case; it is
  covered by code reading only).
