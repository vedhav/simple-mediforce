"""Behaviour tests for scripts/render_soa.py.

The property under test is that a gap never renders as a fact. Each scenario
removes or corrupts one part of the USDM and asserts that the affected cells
come out `unknown` (not `not-scheduled`), that the gap is named in
`result.json`, and that the HTML says so in words.

Runs the real script in a scratch directory with `MEDIFORCE_OUTPUT_DIR` and
`MEDIFORCE_WORKSPACE_DIR` redirected — nothing is copied or rewritten.

    python3 tests/test_render_soa.py
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "scripts" / "render_soa.py"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    if condition:
        print(f"   ok   {description}")
    else:
        print(f"   FAIL {description}")
        failures.append(description)


def code(code_id: str, decode: str) -> dict:
    return {
        "id": code_id,
        "code": None,
        "codeSystem": "http://www.cdisc.org",
        "codeSystemVersion": None,
        "decode": decode,
    }


def complete_usdm() -> dict:
    """A USDM whose schedule of activities is fully stated — the no-gap baseline."""
    return {
        "usdmVersion": "3.0.0",
        "systemName": "test",
        "study": {
            "id": "Study_1",
            "name": "TEST-001",
            "versions": [{
                "id": "StudyVersion_1",
                "versionIdentifier": "1.0",
                "titles": [{
                    "id": "StudyTitle_1",
                    "text": "A Phase III Study Of Something",
                    "type": code("Code_1", "Official Study Title"),
                }],
                "studyIdentifiers": [{
                    "id": "StudyIdentifier_1",
                    "studyIdentifier": "NCT99999999",
                }],
                "studyPhase": {"id": "AliasCode_1", "standardCode": code("Code_2", "Phase III Trial")},
                "studyDesigns": [{
                    "id": "StudyDesign_1",
                    "name": "Main Design",
                    "epochs": [
                        {"id": "Epoch_1", "name": "Screening", "previousId": None, "nextId": "Epoch_2"},
                        {"id": "Epoch_2", "name": "Treatment", "previousId": "Epoch_1", "nextId": None},
                    ],
                    "activities": [
                        {"id": "Activity_1", "name": "Informed consent", "previousId": None, "nextId": "Activity_2", "childIds": []},
                        {"id": "Activity_2", "name": "Vital signs", "previousId": "Activity_1", "nextId": "Activity_3", "childIds": []},
                        {"id": "Activity_3", "name": "PK sample", "previousId": "Activity_2", "nextId": None, "childIds": []},
                    ],
                    "encounters": [
                        {"id": "Encounter_1", "name": "Screening", "type": code("Code_3", "Site Visit"),
                         "scheduledAtId": "Timing_1", "previousId": None, "nextId": "Encounter_2"},
                        {"id": "Encounter_2", "name": "Day 1", "type": code("Code_4", "Site Visit"),
                         "scheduledAtId": "Timing_2", "previousId": "Encounter_1", "nextId": "Encounter_3"},
                        {"id": "Encounter_3", "name": "End of Treatment", "type": code("Code_5", "Site Visit"),
                         "scheduledAtId": "Timing_3", "previousId": "Encounter_2", "nextId": None},
                    ],
                    "scheduleTimelines": [{
                        "id": "ScheduleTimeline_1",
                        "name": "Main Timeline",
                        "mainTimeline": True,
                        "instances": [
                            {"id": "SAI_1", "instanceType": "ScheduledActivityInstance", "encounterId": "Encounter_1",
                             "epochId": "Epoch_1", "activityIds": ["Activity_1", "Activity_2"]},
                            {"id": "SAI_2", "instanceType": "ScheduledActivityInstance", "encounterId": "Encounter_2",
                             "epochId": "Epoch_2", "activityIds": ["Activity_2", "Activity_3"]},
                            {"id": "SAI_3", "instanceType": "ScheduledActivityInstance", "encounterId": "Encounter_3",
                             "epochId": "Epoch_2", "activityIds": ["Activity_2"]},
                        ],
                        "timings": [
                            {"id": "Timing_1", "valueLabel": "Day -14 to -1", "windowLabel": None},
                            {"id": "Timing_2", "valueLabel": "Day 1", "windowLabel": "+/- 1 day"},
                            {"id": "Timing_3", "valueLabel": "Week 12", "windowLabel": None},
                        ],
                    }],
                }],
            }],
        },
    }


def design_of(usdm: dict) -> dict:
    return usdm["study"]["versions"][0]["studyDesigns"][0]


def run(usdm: dict | None, upstream: dict | None = None) -> tuple[int, dict, str, str]:
    """Run the script against one USDM. Returns (exit code, result.json, fragment, standalone)."""
    with tempfile.TemporaryDirectory() as scratch:
        output_dir = Path(scratch) / "output"
        workspace_dir = Path(scratch) / "workspace"
        (workspace_dir / "usdm").mkdir(parents=True)
        output_dir.mkdir(parents=True)

        if usdm is not None:
            (workspace_dir / "usdm" / "usdm.json").write_text(json.dumps(usdm))

        step_input = {"steps": {"generate-usdm": upstream or {"nctId": "NCT99999999"}}}
        (output_dir / "input.json").write_text(json.dumps(step_input))

        env = {**os.environ, "MEDIFORCE_OUTPUT_DIR": str(output_dir), "MEDIFORCE_WORKSPACE_DIR": str(workspace_dir)}
        completed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
        if completed.returncode != 0:
            print(f"   (stderr) {completed.stderr.strip()}")

        result_path = output_dir / "result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        fragment_path = output_dir / "presentation.html"
        standalone_path = output_dir / "schedule-of-activities.html"
        return (
            completed.returncode,
            result,
            fragment_path.read_text() if fragment_path.is_file() else "",
            standalone_path.read_text() if standalone_path.is_file() else "",
        )


def cell_states(fragment: str) -> dict[str, int]:
    return {
        state: fragment.count(f'data-state="{state}"')
        for state in ("scheduled", "not-scheduled", "unknown")
    }


# ── scenarios ────────────────────────────────────────────────────────────────


def test_complete_schedule() -> None:
    print("── a fully stated schedule renders with no gaps")
    exit_code, result, fragment, standalone = run(complete_usdm())

    check(exit_code == 0, "exits 0")
    check(result.get("tableRendered") is True, "tableRendered is true")
    check(result.get("soaComplete") is True, f"soaComplete is true (gaps: {result.get('gaps')})")
    check(result.get("gapCount") == 0, f"gapCount is 0, got {result.get('gapCount')}")
    check(result.get("nctId") == "NCT99999999", "nctId comes from studyIdentifiers")
    check(result.get("studyPhase") == "Phase III Trial", "phase decoded from the AliasCode")
    check(result.get("designs", [{}])[0].get("activities") == 3, "3 activity rows")
    check(result.get("designs", [{}])[0].get("encounters") == 3, "3 visit columns")

    # The fixture pairs 5 of the 9 (activity, visit) combinations, so the other
    # 4 are genuine "not scheduled" — the timeline covers every row and column.
    states = cell_states(fragment)
    check(states["scheduled"] == 5, f"5 scheduled cells, got {states['scheduled']}")
    check(states["not-scheduled"] == 4, f"4 not-scheduled cells, got {states['not-scheduled']}")
    check(states["unknown"] == 0, f"no unknown cells, got {states['unknown']}")
    check(result.get("designs", [{}])[0].get("scheduled") == 5, "result.json agrees on the scheduled count")
    check("No gaps detected" in fragment, "the report states that no gaps were detected")

    check("Day -14 to -1" in fragment, "the visit timing is shown")
    check("Day 1 (+/- 1 day)" in fragment, "the timing window is shown alongside the value")
    check("Screening" in fragment and "Treatment" in fragment, "epoch grouping headers are shown")

    # Ordering must follow the previousId/nextId chain, not declaration order.
    check(
        fragment.index("Informed consent") < fragment.index("PK sample"),
        "activity rows follow the chain order",
    )

    check("<!DOCTYPE html>" not in fragment, "presentation.html is a body fragment, not a document")
    check("<html" not in fragment.lower(), "the fragment declares no <html> element")
    check(standalone.startswith("<!DOCTYPE html>"), "the standalone file is a full document")
    check("<title>" in standalone, "the standalone file has a title")
    check("cdn." not in fragment and "http://" not in fragment, "the fragment loads nothing off-host")


def test_no_schedule_timelines() -> None:
    print("── with no scheduleTimelines every cell is unknown, never empty")
    usdm = complete_usdm()
    design_of(usdm)["scheduleTimelines"] = []
    exit_code, result, fragment, _ = run(usdm)

    states = cell_states(fragment)
    check(exit_code == 0, "exits 0 — a data gap is not a step failure")
    check(states["unknown"] == 9, f"all 9 cells are unknown, got {states}")
    check(states["not-scheduled"] == 0, "no cell claims 'not scheduled'")
    check(states["scheduled"] == 0, "no cell claims 'scheduled'")
    check(result.get("soaComplete") is False, "soaComplete is false")
    check(result.get("tableRendered") is True, "the table is still drawn, with unknowns")
    check(
        any("no scheduleTimelines" in gap["message"] for gap in result.get("gaps", [])),
        f"the missing timeline is named in gaps: {result.get('gaps')}",
    )
    check("not stated" in fragment, "the report explains that unknown means not stated")


def test_encounter_with_no_instance() -> None:
    print("── a visit no instance references gets an unknown column, not an empty one")
    usdm = complete_usdm()
    timeline = design_of(usdm)["scheduleTimelines"][0]
    timeline["instances"] = [i for i in timeline["instances"] if i["encounterId"] != "Encounter_3"]
    exit_code, result, fragment, _ = run(usdm)

    states = cell_states(fragment)
    check(exit_code == 0, "exits 0")
    check(states["unknown"] == 3, f"the whole 3-row column is unknown, got {states}")
    check(result.get("soaComplete") is False, "soaComplete is false")
    check(
        any("End of Treatment" in gap["message"] for gap in result.get("gaps", [])),
        f"the unreferenced visit is named in gaps: {result.get('gaps')}",
    )
    check("soa-col-unknown" in fragment, "the column header is marked unknown")
    # A column-wide gap must not mark every row: it would make the row filter
    # match everything and stop discriminating.
    flagged = fragment.count('data-row-gap="true"')
    check(flagged == 0, f"no row is flagged for a gap that belongs to the column, got {flagged}")


def test_activity_with_no_instance() -> None:
    print("── an activity no instance references gets an unknown row, not an empty one")
    usdm = complete_usdm()
    for instance in design_of(usdm)["scheduleTimelines"][0]["instances"]:
        instance["activityIds"] = [a for a in instance["activityIds"] if a != "Activity_3"]
    exit_code, result, fragment, _ = run(usdm)

    states = cell_states(fragment)
    check(exit_code == 0, "exits 0")
    check(states["unknown"] == 3, f"the whole 3-column row is unknown, got {states}")
    check(
        any("PK sample" in gap["message"] for gap in result.get("gaps", [])),
        f"the unreferenced activity is named in gaps: {result.get('gaps')}",
    )
    check("soa-row-unknown" in fragment, "the row header is marked unknown")
    flagged = fragment.count('data-row-gap="true"')
    check(flagged == 1, f"exactly the one affected row is flagged, got {flagged}")


def test_dangling_references() -> None:
    print("── references to undeclared ids are reported, not silently dropped")
    usdm = complete_usdm()
    design_of(usdm)["scheduleTimelines"][0]["instances"].append({
        "id": "SAI_9", "instanceType": "ScheduledActivityInstance",
        "encounterId": "Encounter_404", "activityIds": ["Activity_404"],
    })
    exit_code, result, fragment, _ = run(usdm)

    dangling = result.get("danglingReferences", [])
    check(exit_code == 0, "exits 0")
    check(len(dangling) == 2, f"both broken references are reported, got {dangling}")
    check(
        {reference["field"] for reference in dangling} == {"encounterId", "activityIds"},
        "one per reference field",
    )
    check("broken reference" in fragment, "the report has a broken-reference panel")
    check(result.get("soaComplete") is False, "soaComplete is false")


def test_unplaceable_instance_does_not_license_not_scheduled() -> None:
    print("── an activity known only from an unplaceable instance stays unknown everywhere")
    usdm = complete_usdm()
    timeline = design_of(usdm)["scheduleTimelines"][0]
    # Activity_3 is scheduled only at Encounter_2. Repoint that instance at a
    # visit that does not exist: the activity is still referenced, but nothing
    # says where it happens, so no column may claim it is absent.
    for instance in timeline["instances"]:
        if instance["id"] == "SAI_2":
            instance["encounterId"] = "Encounter_404"
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    rows = fragment.split('data-activity-label="PK sample"')
    check(len(rows) == 2, "the PK sample row is present")
    pk_row = rows[1].split("</tr>")[0]
    claimed_absent = pk_row.count('data-state="not-scheduled"')
    unknown = pk_row.count('data-state="unknown"')
    check(claimed_absent == 0, f"no column claims the activity is absent, got {claimed_absent}")
    check(unknown == 3, f"every cell in its row is unknown, got {unknown}")
    check(len(result.get("danglingReferences", [])) == 1, "the unresolvable encounterId is reported")


def test_unnamed_activity() -> None:
    print("── an activity with no name shows as unnamed, never as its id alone")
    usdm = complete_usdm()
    del design_of(usdm)["activities"][1]["name"]
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    check("unnamed (Activity_2)" in fragment, "the row is labelled unnamed with its id")
    check(
        any("no name or label" in gap["message"] for gap in result.get("gaps", [])),
        f"the missing name is named in gaps: {result.get('gaps')}",
    )


def test_missing_timing() -> None:
    print("── a visit with no resolvable timing says so")
    usdm = complete_usdm()
    del design_of(usdm)["encounters"][0]["scheduledAtId"]
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    check("timing not stated" in fragment, "the column header says the timing is not stated")
    check(
        any("no stated timing" in gap["message"] for gap in result.get("gaps", [])),
        f"the missing timing is named in gaps: {result.get('gaps')}",
    )


def test_broken_encounter_chain() -> None:
    print("── a broken visit order is flagged, not silently reordered")
    usdm = complete_usdm()
    design_of(usdm)["encounters"][1]["nextId"] = None
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    ambiguous = [gap for gap in result.get("gaps", []) if gap["severity"] == "ambiguous"]
    check(
        any("not stated as one consistent" in gap["message"] for gap in ambiguous),
        f"the broken chain is reported as ambiguous: {ambiguous}",
    )
    check("declaration order" in fragment, "the report says the shown order may not be the protocol's")


def test_missing_epoch_grouping() -> None:
    print("── visits with no epoch say 'epoch not stated'")
    usdm = complete_usdm()
    for instance in design_of(usdm)["scheduleTimelines"][0]["instances"]:
        del instance["epochId"]
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    check("epoch not stated" in fragment, "the grouping header says the epoch is not stated")
    check(
        any("could not be grouped by epoch" in gap["message"] for gap in result.get("gaps", [])),
        f"the missing grouping is named in gaps: {result.get('gaps')}",
    )


def test_no_activities() -> None:
    print("── no activities means no table, stated as such")
    usdm = complete_usdm()
    design_of(usdm)["activities"] = []
    exit_code, result, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0 — an empty schedule is a data gap, not a crash")
    check(result.get("tableRendered") is False, "tableRendered is false")
    check("No table could be drawn" in fragment, "the report says no table could be drawn")
    check("<table" not in fragment, "no empty table is rendered")
    check("no activities" in json.dumps(result.get("gaps", [])), "the gap names the empty activities")


def test_upstream_unresolved_is_surfaced() -> None:
    print("── the extraction step's own unresolved list is shown to the reviewer")
    upstream = {
        "nctId": "NCT99999999",
        "unresolved": [{"path": "studyDesigns[0].activities", "reason": "SoA appendix was a scanned image"}],
    }
    exit_code, _, fragment, _ = run(complete_usdm(), upstream)

    check(exit_code == 0, "exits 0")
    check("scanned image" in fragment, "the upstream reason is shown verbatim")
    check("studyDesigns[0].activities" in fragment, "the upstream path is shown")


def test_missing_usdm_is_a_step_failure() -> None:
    print("── a missing usdm.json fails the step with a diagnosable error")
    exit_code, result, fragment, _ = run(None)

    check(exit_code == 1, "exits 1 — a broken upstream contract is not a data gap")
    check("usdm.json" in (result.get("error") or ""), f"the error names the file: {result.get('error')}")
    check("workspaceListing" in result, "the error carries a worktree listing for diagnosis")
    check(fragment == "", "no report is written")


def test_html_is_escaped() -> None:
    print("── source text is escaped, so a report cannot be injected into")
    usdm = complete_usdm()
    design_of(usdm)["activities"][0]["name"] = '</script><img src=x onerror="alert(1)">'
    exit_code, _, fragment, _ = run(usdm)

    check(exit_code == 0, "exits 0")
    check("<img" not in fragment, "the injected tag never appears as markup")
    check("&lt;img src=x" in fragment, "it appears as escaped text instead")
    check("&lt;/script&gt;" in fragment, "the closing script tag is escaped")


def main() -> int:
    if not SCRIPT.is_file():
        print(f"{SCRIPT} not found")
        return 1

    for scenario in (
        test_complete_schedule,
        test_no_schedule_timelines,
        test_encounter_with_no_instance,
        test_activity_with_no_instance,
        test_dangling_references,
        test_unplaceable_instance_does_not_license_not_scheduled,
        test_unnamed_activity,
        test_missing_timing,
        test_broken_encounter_chain,
        test_missing_epoch_grouping,
        test_no_activities,
        test_upstream_unresolved_is_surfaced,
        test_missing_usdm_is_a_step_failure,
        test_html_is_escaped,
    ):
        scenario()

    print(f"\n{len(failures)} assertion(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
