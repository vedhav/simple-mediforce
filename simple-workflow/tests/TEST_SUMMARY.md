# Test summary — simple-workflow

Run everything:

```bash
python3 tests/run_tests.py
```

No secrets or credentials are needed. The only prerequisite is outbound network
to `clinicaltrials.gov` and `cdn.clinicaltrials.gov`; without it the suite
reports SKIP rather than FAIL.

| Script | Status | Asserted |
|--------|--------|----------|
| `scripts/fetch_study_documents.py` | **tested** | Happy path against the live API using `tests/fixtures/fetch-documents.input.json` (`NCT04822298`): exit 0; `result.json` has `nctId`, `documentCount == len(documents)`, `documentCount > 0`, and `summary` naming the study; every document carries `filename`, `sourceFilename`, `sourceUrl`, `typeAbbrev`, `label`, `date`, `hasProtocol`, `hasSap`, `hasIcf`, `sizeBytes`; each file exists on disk, its on-disk size equals the reported `sizeBytes`, it starts with the `%PDF-` magic bytes, and its name is prefixed with the NCT id. Failure path: step input with no `nctId` exits 1 and reports the reason in `result.json`'s `error` field. |

Last run: 1 passed, 0 skipped, 0 failed — downloaded `Prot` and `SAP` for
`NCT04822298`.

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
