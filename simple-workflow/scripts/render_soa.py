"""Render the Schedule of Activities from USDM metadata as an interactive HTML table.

Reads the `usdm.json` that `generate-usdm` committed into the run worktree,
derives the activity x encounter matrix from `studyDesign.scheduleTimelines`,
and writes two HTML renderings plus a machine-readable gap report.

The governing rule is that a gap must never look like a fact. USDM has no way to
say "this cell is unknown" — an activity simply is or is not referenced by a
ScheduledActivityInstance — so a naive renderer turns every extraction failure
into a confident "not scheduled". Every cell here therefore resolves to one of
three states, and `unknown` is the default whenever the evidence for `no` is
itself missing:

    scheduled      an instance at that encounter lists that activity
    not scheduled  the timeline covers both that activity and that encounter,
                   and does not pair them
    unknown        there is no timeline, or no instance references that
                   encounter, or no instance references that activity

Everything the renderer could not resolve is collected into `gaps` and shown at
the top of the report, so the reviewer reads the holes before the data.

Outputs, all in the step's output directory:

    presentation.html            body fragment, rendered inline in the run UI
    schedule-of-activities.html  standalone document, downloadable
    result.json                  the step output value: counts and gaps
"""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

OUTPUT_DIR = Path(os.environ.get("MEDIFORCE_OUTPUT_DIR", "/output"))
WORKSPACE_DIR = Path(os.environ.get("MEDIFORCE_WORKSPACE_DIR", "/workspace"))
USDM_FILENAME = "usdm.json"
UPSTREAM_STEP_ID = "generate-usdm"

# Directories that cannot hold a deliverable and are expensive to walk.
SEARCH_PRUNE = {".git", "node_modules", "__pycache__", ".venv"}

# Cell states.
SCHEDULED = "scheduled"
NOT_SCHEDULED = "not-scheduled"
UNKNOWN = "unknown"

# Gap severities. `missing` means the source said nothing; `ambiguous` means it
# said something the renderer could not reconcile.
MISSING = "missing"
AMBIGUOUS = "ambiguous"


def fail(message: str, extra: dict | None = None) -> NoReturn:
    """Report a step failure the way the script-container plugin reads it.

    The plugin surfaces `result.json`'s `error` field when a script exits
    non-zero, so write the file before exiting.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"error": message}
    if extra:
        payload.update(extra)
    (OUTPUT_DIR / "result.json").write_text(json.dumps(payload, indent=2))
    print(message, file=sys.stderr)
    sys.exit(1)


# ── input ────────────────────────────────────────────────────────────────────


def read_step_input() -> dict:
    input_path = OUTPUT_DIR / "input.json"
    try:
        return json.loads(input_path.read_text())
    except FileNotFoundError:
        fail(f"{input_path} not found — the engine did not provide step input")
    except json.JSONDecodeError as error:
        fail(f"{input_path} is not valid JSON: {error}")


def upstream_output(step_input: dict) -> dict:
    """The `generate-usdm` result, by step id with a flattened-predecessor fallback.

    The engine builds step input as `{...previousStepOutput, steps: variables}`,
    so reading by step id survives a step being inserted in front of this one.
    """
    by_step = (step_input.get("steps") or {}).get(UPSTREAM_STEP_ID)
    if isinstance(by_step, dict):
        return by_step
    return step_input


def locate_usdm() -> Path:
    """Find `usdm.json` in the run worktree.

    `generate-usdm` writes it to `/workspace/usdm/` and copies it to `/output`,
    from where the engine commits it to `.mediforce/output/generate-usdm/`. Both
    land in the worktree that every step of the run mounts at `/workspace`, but
    which one exists depends on the agent following the whole output contract,
    so search rather than trust a path.
    """
    preferred = [
        WORKSPACE_DIR / "usdm" / USDM_FILENAME,
        WORKSPACE_DIR / ".mediforce" / "output" / UPSTREAM_STEP_ID / USDM_FILENAME,
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate

    found = sorted(
        path
        for path in WORKSPACE_DIR.rglob(USDM_FILENAME)
        if path.is_file() and SEARCH_PRUNE.isdisjoint(path.parts)
    )
    if found:
        return found[0]

    fail(
        f"no {USDM_FILENAME} found under {WORKSPACE_DIR} — "
        f"{UPSTREAM_STEP_ID} did not deliver its output contract",
        {"searched": [str(path) for path in preferred], "workspaceListing": workspace_listing()},
    )


def workspace_listing(limit: int = 200) -> list[str]:
    """A bounded listing of the worktree, so a missing file is diagnosable."""
    try:
        entries = []
        for path in sorted(WORKSPACE_DIR.rglob("*")):
            if not SEARCH_PRUNE.isdisjoint(path.parts):
                continue
            entries.append(str(path.relative_to(WORKSPACE_DIR)))
            if len(entries) >= limit:
                break
        return entries
    except OSError as error:
        return [f"<could not list {WORKSPACE_DIR}: {error}>"]


def load_usdm(path: Path) -> dict:
    try:
        usdm = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        fail(f"{path} is not valid JSON: {error}")
    if not isinstance(usdm, dict):
        fail(f"{path} is not a USDM object (got {type(usdm).__name__})")
    return usdm


# ── USDM traversal ───────────────────────────────────────────────────────────


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def code_decode(value: Any) -> str | None:
    """The human-readable text of a USDM Code or AliasCode, if it carries one."""
    node = as_dict(value)
    if not node:
        return None
    standard = as_dict(node.get("standardCode"))
    if standard:
        node = standard
    decode = node.get("decode")
    if isinstance(decode, str) and decode.strip() != "":
        return decode.strip()
    code = node.get("code")
    if isinstance(code, str) and code.strip() != "":
        return code.strip()
    return None


def display_name(entity: dict, fallback_id: str | None) -> tuple[str, bool]:
    """Best label for an entity, and whether it had to be synthesised.

    A synthesised label is a gap: the reviewer must not read an id as a name.
    """
    for key in ("label", "name"):
        value = entity.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip(), False
    if fallback_id:
        return f"unnamed ({fallback_id})", True
    return "unnamed", True


class Gaps:
    """Collector for everything the renderer could not resolve."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, severity: str, scope: str, message: str) -> None:
        self.entries.append({"severity": severity, "scope": scope, "message": message})

    def missing(self, scope: str, message: str) -> None:
        self.add(MISSING, scope, message)

    def ambiguous(self, scope: str, message: str) -> None:
        self.add(AMBIGUOUS, scope, message)

    def count(self) -> int:
        return len(self.entries)


def order_by_chain(
    entities: list[dict],
    gaps: Gaps,
    scope: str,
    kind: str,
) -> tuple[list[dict], bool]:
    """Order entities by their `previousId`/`nextId` chains.

    USDM orders arrays through an explicit linked list rather than position.
    Several chains are normal, not a defect: a protocol with one schedule per
    cycle or regimen chains each schedule's visits separately, so requiring a
    single chain across all of them would flag every multi-schedule protocol.
    Chains are therefore walked from every start point and concatenated in the
    order their heads are declared.

    A start point is an entity **nothing points to** — not one whose own
    `previousId` is empty. The distinction matters: three alternative Cycle 1
    regimens all follow screening, and each of their first visits says
    `previousId: Screening`, but a linked list can only express one branch, so
    `Screening.nextId` names just one of the three. Reading `previousId` to find
    heads therefore misses two entire schedules. `nextId` is treated as
    authoritative for order, and a `previousId` that disagrees with it is
    ignored rather than reported — with branches it disagrees routinely.

    Only an entity reachable from no start point — that is, one caught in a
    cycle — is a genuine defect. Then the declaration order is used and said to
    be unreliable, because silently sorting on a broken chain would present an
    invented visit order as the protocol's.
    """
    identified = [entity for entity in entities if isinstance(entity.get("id"), str)]
    if len(identified) != len(entities):
        gaps.missing(scope, f"{len(entities) - len(identified)} {kind}(s) have no id and cannot be ordered")
    if len(identified) <= 1:
        return list(entities), True

    by_id = {entity["id"]: entity for entity in identified}
    successors = {
        entity["id"]: entity.get("nextId")
        for entity in identified
        if isinstance(entity.get("nextId"), str)
    }
    pointed_at = {target for target in successors.values() if target in by_id}
    heads = [entity for entity in identified if entity["id"] not in pointed_at]

    ordered: list[dict] = []
    seen: set[str] = set()
    for head in heads:
        cursor: str | None = head["id"]
        while isinstance(cursor, str) and cursor in by_id and cursor not in seen:
            seen.add(cursor)
            ordered.append(by_id[cursor])
            cursor = successors.get(cursor)

    unreached = [entity for entity in identified if entity["id"] not in seen]
    if unreached:
        gaps.ambiguous(
            scope,
            f"{len(unreached)} of {len(identified)} {kind}s sit in a circular nextId chain and so "
            f"have no stated position ({len(heads)} start point(s), {len(successors)} link(s)) — "
            f"everything is shown in declaration order, which may not be the protocol's order",
        )
        return list(entities), False

    return ordered, True


def timeline_instances(timeline: dict) -> list[dict]:
    """The ScheduledActivityInstances of one timeline.

    Decision instances carry no activities, so only activity instances
    contribute cells. An instance with no `instanceType` is included — being
    lenient here is right, since the alternative is silently dropping schedule
    data over a missing discriminator.
    """
    instances = []
    for instance in as_list(timeline.get("instances")):
        instance_dict = as_dict(instance)
        instance_type = instance_dict.get("instanceType")
        if isinstance(instance_type, str) and instance_type != "ScheduledActivityInstance":
            continue
        instances.append(instance_dict)
    return instances


def collect_timings(design: dict) -> dict[str, dict]:
    timings: dict[str, dict] = {}
    for timeline in as_list(design.get("scheduleTimelines")):
        for timing in as_list(as_dict(timeline).get("timings")):
            timing_dict = as_dict(timing)
            timing_id = timing_dict.get("id")
            if isinstance(timing_id, str):
                timings[timing_id] = timing_dict
    return timings


def encounter_timing(encounter: dict, timings: dict[str, dict]) -> str | None:
    """The scheduled time of a visit, as the protocol stated it."""
    scheduled_at = encounter.get("scheduledAtId")
    timing = timings.get(scheduled_at) if isinstance(scheduled_at, str) else None
    if not timing:
        return None

    label = timing.get("valueLabel") or timing.get("value") or code_decode(timing.get("type"))
    if not isinstance(label, str) or label.strip() == "":
        return None
    window = timing.get("windowLabel")
    if isinstance(window, str) and window.strip() != "":
        return f"{label.strip()} ({window.strip()})"
    return label.strip()


def build_design(design: dict, index: int, gaps: Gaps) -> dict:
    """Turn one StudyDesign into the row/column/cell model the renderer draws."""
    design_id = design.get("id") if isinstance(design.get("id"), str) else f"StudyDesign_{index + 1}"
    design_label, _ = display_name(design, design_id)
    scope = f"studyDesigns[{index}]"

    activities = [as_dict(entity) for entity in as_list(design.get("activities"))]
    encounters = [as_dict(entity) for entity in as_list(design.get("encounters"))]
    if not activities:
        gaps.missing(f"{scope}.activities", "no activities — the schedule has no rows to show")
    if not encounters:
        gaps.missing(f"{scope}.encounters", "no encounters — the schedule has no visit columns to show")

    activities, _ = order_by_chain(activities, gaps, f"{scope}.activities", "activity")
    encounters, _ = order_by_chain(encounters, gaps, f"{scope}.encounters", "encounter")

    timings = collect_timings(design)
    activity_ids = {entity["id"] for entity in activities if isinstance(entity.get("id"), str)}
    encounter_ids = {entity["id"] for entity in encounters if isinstance(entity.get("id"), str)}

    epochs_by_id = {
        as_dict(epoch)["id"]: as_dict(epoch)
        for epoch in as_list(design.get("epochs"))
        if isinstance(as_dict(epoch).get("id"), str)
    }

    # Name and describe every activity once, so the gaps that belong to the
    # activity itself are raised once rather than once per table.
    rows_meta = []
    for position, activity in enumerate(activities):
        activity_id = activity.get("id") if isinstance(activity.get("id"), str) else None
        label, synthesised = display_name(activity, activity_id)
        if synthesised:
            gaps.missing(f"{scope}.activities[{position}]", "the activity has no name or label")
        children = [child for child in as_list(activity.get("childIds")) if isinstance(child, str)]
        if children:
            gaps.ambiguous(
                f"{scope}.activities[{position}]",
                f"activity '{label}' declares {len(children)} child activity(ies); the table is "
                "flat, so the grouping is not shown",
            )
        rows_meta.append({
            "id": activity_id,
            "label": label,
            "description": activity.get("description") if isinstance(activity.get("description"), str) else None,
        })

    # One table per schedule timeline. A protocol with a Cycle 1 schedule and a
    # Cycle 2-and-beyond schedule — or with alternative schedules per regimen —
    # models each as its own timeline, and merging them into a single grid would
    # place visits from mutually exclusive schedules side by side as though they
    # were one. Each timeline therefore gets its own table.
    timelines = [as_dict(entity) for entity in as_list(design.get("scheduleTimelines"))]
    dangling: list[dict] = []
    scheduling = [
        read_timeline(timeline, index, activity_ids, encounter_ids, scope, gaps, dangling)
        for index, timeline in enumerate(timelines)
    ]

    for reference in dangling:
        gaps.ambiguous(
            reference["from"],
            f"{reference['field']} points at '{reference['value']}', which is not declared in this design",
        )

    if not timelines:
        gaps.missing(
            f"{scope}.scheduleTimelines",
            "no scheduleTimelines — USDM carries the activity-to-visit pairings here, so "
            "every cell below is unknown rather than empty",
        )
    elif not any(entry["instances"] for entry in scheduling):
        gaps.missing(
            f"{scope}.scheduleTimelines",
            f"{len(timelines)} scheduleTimeline(s) declared but none contains a "
            "ScheduledActivityInstance — every cell below is unknown rather than empty",
        )

    # A visit no timeline schedules anything at is a gap on the design, not on
    # any one table, so it is reported once and shown as an unknown column in
    # every table (a visit that belongs to no schedule belongs to all of them
    # equally as far as the reader can tell).
    encounters_anywhere = set().union(*(entry["encounters"] for entry in scheduling)) if scheduling else set()
    orphan_encounter_ids = set()
    for position, encounter in enumerate(encounters):
        encounter_id = encounter.get("id") if isinstance(encounter.get("id"), str) else None
        label, synthesised = display_name(encounter, encounter_id)
        if synthesised:
            gaps.missing(f"{scope}.encounters[{position}]", "the visit has no name or label")
        if encounter_timing(encounter, timings) is None:
            gaps.missing(
                f"{scope}.encounters[{position}]",
                f"visit '{label}' has no stated timing (no resolvable scheduledAtId)",
            )
        if encounter_id and encounter_id not in encounters_anywhere:
            orphan_encounter_ids.add(encounter_id)
            gaps.missing(
                f"{scope}.encounters[{position}]",
                f"no scheduled activity instance in any timeline references visit '{label}', so "
                "its whole column is unknown rather than empty",
            )

    if not scheduling:
        scheduling = [EMPTY_SCHEDULING]
    tables = [
        build_table(entry, rows_meta, encounters, timings, epochs_by_id, orphan_encounter_ids,
                    len(scheduling), scope, gaps)
        for entry in scheduling
    ]

    return {
        "id": design_id,
        "label": design_label,
        "tables": tables,
        "dangling": dangling,
        "timelineCount": len(timelines),
        "counts": {
            "activities": len(rows_meta),
            "encounters": len(encounters),
            "scheduled": sum(table["counts"]["scheduled"] for table in tables),
            "notScheduled": sum(table["counts"]["notScheduled"] for table in tables),
            "unknown": sum(table["counts"]["unknown"] for table in tables),
        },
    }


# The stand-in when a design declares no timeline at all: one table, every cell
# unknown. `hasTimeline` is what stops any cell claiming "not scheduled".
EMPTY_SCHEDULING: dict = {
    "id": None, "label": "Schedule not stated", "hasTimeline": False,
    "instances": [], "encounters": set(), "activities": set(),
    "scheduled": set(), "epochOfEncounter": {},
}


def read_timeline(
    timeline: dict,
    index: int,
    activity_ids: set[str],
    encounter_ids: set[str],
    design_scope: str,
    gaps: Gaps,
    dangling: list[dict],
) -> dict:
    """Resolve one timeline's instances into the sets the cell states are read from."""
    timeline_id = timeline.get("id") if isinstance(timeline.get("id"), str) else None
    label, _ = display_name(timeline, timeline_id or f"ScheduleTimeline_{index + 1}")
    scope = f"{design_scope}.scheduleTimelines[{index}]"
    instances = timeline_instances(timeline)

    scheduled: set[tuple[str, str]] = set()
    encounters_covered: set[str] = set()
    activities_covered: set[str] = set()
    epoch_of_encounter: dict[str, str] = {}
    without_encounter = 0

    for instance in instances:
        instance_id = instance.get("id") if isinstance(instance.get("id"), str) else "<no id>"
        encounter_id = instance.get("encounterId")

        if not isinstance(encounter_id, str) or encounter_id == "":
            without_encounter += 1
            encounter_id = None
        elif encounter_id not in encounter_ids:
            dangling.append({
                "from": f"{scope} instance {instance_id}",
                "field": "encounterId",
                "value": encounter_id,
            })
            encounter_id = None
        else:
            encounters_covered.add(encounter_id)
            epoch_id = instance.get("epochId")
            if isinstance(epoch_id, str) and epoch_id != "":
                epoch_of_encounter.setdefault(encounter_id, epoch_id)

        for activity_id in as_list(instance.get("activityIds")):
            if not isinstance(activity_id, str) or activity_id == "":
                continue
            if activity_id not in activity_ids:
                dangling.append({
                    "from": f"{scope} instance {instance_id}",
                    "field": "activityIds",
                    "value": activity_id,
                })
                continue
            # Only an instance that landed on a declared encounter counts as
            # scheduling evidence. An instance with a missing or dangling
            # encounterId could belong to any visit, so treating its activities
            # as "covered" would license a "not scheduled" verdict in every
            # other column on the strength of a reference we could not resolve.
            if isinstance(encounter_id, str):
                activities_covered.add(activity_id)
                scheduled.add((activity_id, encounter_id))

    if without_encounter > 0:
        gaps.missing(
            scope,
            f"{without_encounter} scheduled activity instance(s) in '{label}' name no encounter, "
            "so their activities could not be placed in any visit column",
        )

    return {
        "id": timeline_id,
        "label": label,
        "hasTimeline": True,
        "instances": instances,
        "encounters": encounters_covered,
        "activities": activities_covered,
        "scheduled": scheduled,
        "epochOfEncounter": epoch_of_encounter,
    }


def build_table(
    scheduling: dict,
    rows_meta: list[dict],
    encounters: list[dict],
    timings: dict[str, dict],
    epochs_by_id: dict[str, dict],
    orphan_encounter_ids: set[str],
    table_count: int,
    design_scope: str,
    gaps: Gaps,
) -> dict:
    """Build one rendered table from one timeline's scheduling data."""
    scope = f"{design_scope} schedule '{scheduling['label']}'"

    columns: list[dict] = []
    for encounter in encounters:
        encounter_id = encounter.get("id") if isinstance(encounter.get("id"), str) else None
        # Visits belonging to a *different* schedule are not this schedule's
        # columns. Visits belonging to no schedule are shown in every table,
        # since nothing says which one they belong to.
        if encounter_id is not None and encounter_id not in scheduling["encounters"]:
            if encounter_id not in orphan_encounter_ids:
                continue
        label, _ = display_name(encounter, encounter_id)
        has_data = encounter_id in scheduling["encounters"] if encounter_id else False
        epoch_id = scheduling["epochOfEncounter"].get(encounter_id) if encounter_id else None
        epoch = epochs_by_id.get(epoch_id) if epoch_id else None
        columns.append({
            "id": encounter_id,
            "label": label,
            "timing": encounter_timing(encounter, timings),
            "type": code_decode(encounter.get("type")),
            "hasData": has_data,
            "epochLabel": display_name(epoch, epoch_id)[0] if epoch else None,
        })

    if scheduling["hasTimeline"] and epochs_by_id and not scheduling["epochOfEncounter"]:
        gaps.missing(
            scope,
            f"{len(epochs_by_id)} epoch(s) are declared but no scheduled activity instance in "
            "this schedule names one, so its visits could not be grouped by epoch",
        )
    for column in columns:
        if column["epochLabel"] is None and epochs_by_id and scheduling["epochOfEncounter"] and column["hasData"]:
            gaps.missing(
                scope,
                f"visit '{column['label']}' is not assigned to an epoch by any scheduled activity "
                "instance, so it is grouped under 'epoch not stated'",
            )

    rows: list[dict] = []
    outside = []
    for meta in rows_meta:
        activity_id = meta["id"]
        has_data = activity_id in scheduling["activities"] if activity_id else False
        if scheduling["hasTimeline"] and activity_id and not has_data:
            outside.append(meta["label"])
        rows.append({
            **meta,
            "hasData": has_data,
            "cells": [
                {
                    "state": cell_state(activity_id, column, has_data, scheduling["scheduled"],
                                        scheduling["hasTimeline"]),
                    "column": column,
                }
                for column in columns
            ],
        })

    if outside:
        # Phrased differently depending on whether there is another schedule that
        # could account for the activity: with one schedule this is a plain hole,
        # with several it is usually an activity belonging to a different one.
        elsewhere = (
            "they may belong to one of the other schedules below, or may have been missed"
            if table_count > 1
            else "so their rows are unknown rather than empty"
        )
        gaps.missing(
            scope,
            f"{len(outside)} activity(ies) are not scheduled anywhere in this schedule "
            f"({', '.join(outside[:6])}{', …' if len(outside) > 6 else ''}) — {elsewhere}; "
            "their cells here are unknown, not blank",
        )

    return {
        "id": scheduling["id"],
        "label": scheduling["label"],
        "rows": rows,
        "columns": columns,
        "counts": {
            "activities": len(rows),
            "encounters": len(columns),
            "scheduled": sum(1 for row in rows for cell in row["cells"] if cell["state"] == SCHEDULED),
            "notScheduled": sum(1 for row in rows for cell in row["cells"] if cell["state"] == NOT_SCHEDULED),
            "unknown": sum(1 for row in rows for cell in row["cells"] if cell["state"] == UNKNOWN),
        },
    }


def cell_state(
    activity_id: str | None,
    column: dict,
    activity_has_data: bool,
    scheduled: set[tuple[str, str]],
    has_timeline: bool,
) -> str:
    """Resolve one cell to scheduled / not scheduled / unknown.

    `not scheduled` is only claimed when the timeline demonstrably covers both
    the activity and the visit and still does not pair them. Anything weaker is
    unknown — an absent pairing is not evidence of an absent procedure.
    """
    encounter_id = column["id"]
    if activity_id is None or encounter_id is None:
        return UNKNOWN
    if (activity_id, encounter_id) in scheduled:
        return SCHEDULED
    if not has_timeline:
        return UNKNOWN
    if not column["hasData"] or not activity_has_data:
        return UNKNOWN
    return NOT_SCHEDULED


def build_model(usdm: dict, usdm_path: Path, upstream: dict, gaps: Gaps) -> dict:
    study = as_dict(usdm.get("study"))
    if not study:
        fail(f"{usdm_path} has no `study` object — it is not a USDM envelope")

    versions = [as_dict(version) for version in as_list(study.get("versions"))]
    if not versions:
        fail(f"{usdm_path} has no `study.versions` — there is no study design to render")
    if len(versions) > 1:
        gaps.ambiguous(
            "study.versions",
            f"{len(versions)} study versions are present; the schedule below is version 1 only",
        )
    version = versions[0]

    titles = [as_dict(title) for title in as_list(version.get("titles"))]
    title_text = next(
        (title["text"] for title in titles if isinstance(title.get("text"), str) and title["text"].strip() != ""),
        None,
    )
    if title_text is None:
        gaps.missing("study.versions[0].titles", "the study has no title text")

    phase = code_decode(version.get("studyPhase"))
    if phase is None:
        gaps.missing("study.versions[0].studyPhase", "the study phase is not stated")

    designs = [as_dict(design) for design in as_list(version.get("studyDesigns"))]
    if not designs:
        gaps.missing(
            "study.versions[0].studyDesigns",
            "no study designs — there is no schedule of activities to render",
        )

    nct_id = next(
        (
            identifier["studyIdentifier"]
            for identifier in (as_dict(entry) for entry in as_list(version.get("studyIdentifiers")))
            if isinstance(identifier.get("studyIdentifier"), str)
            and identifier["studyIdentifier"].startswith("NCT")
        ),
        None,
    )
    if nct_id is None:
        candidate = upstream.get("nctId")
        nct_id = candidate if isinstance(candidate, str) else None

    unresolved = [
        entry for entry in as_list(upstream.get("unresolved")) if isinstance(entry, dict)
    ]

    designs_built = [build_design(design, index, gaps) for index, design in enumerate(designs)]

    # A whole SoA table missing from the USDM is invisible to this renderer:
    # nothing in the model records that the protocol had another one. So
    # `generate-usdm` reports how many SoA tables it FOUND, and the count is
    # checked against how many timelines it actually modelled. Without this, a
    # protocol with four SoA tables of which one was extracted renders a
    # confident, complete-looking table and a "no gaps" verdict.
    found = upstream.get("soaTablesFound")
    modelled = sum(design["timelineCount"] for design in designs_built)
    # The per-table breakdown, when the extraction step supplied one. It turns
    # "2 tables are absent" into a statement of *which* two and why, which is
    # the difference between a warning the reviewer can act on and one they have
    # to go digging for.
    source_tables = [
        entry for entry in as_list(upstream.get("soaTables")) if isinstance(entry, dict)
    ]
    unmodelled = [
        entry for entry in source_tables if entry.get("modelled") is not True
    ]

    if isinstance(found, int) and found > modelled:
        named = "; ".join(
            f"{entry.get('sourceLabel') or 'unnamed table'}"
            + (f" — {entry['note']}" if isinstance(entry.get("note"), str) and entry["note"] else "")
            for entry in unmodelled
        )
        detail = (
            f" The table(s) left out: {named}."
            if named
            else " The extraction step did not say which ones."
        )
        gaps.missing(
            "study.versions[0].studyDesigns[].scheduleTimelines",
            f"the extraction step found {found} schedule-of-activities table(s) in the source "
            f"documents but modelled only {modelled} — {found - modelled} whole table(s) are absent "
            f"from this report, so what is drawn below is incomplete however complete it looks."
            f"{detail}",
        )
    elif found is None:
        gaps.missing(
            "generate-usdm.soaTablesFound",
            "the extraction step did not report how many schedule-of-activities tables the source "
            "documents contain, so this report cannot tell whether a whole table is missing. "
            "Protocols routinely carry several (one per cycle, or one per regimen)",
        )

    return {
        "nctId": nct_id,
        "title": title_text,
        "phase": phase,
        "usdmVersion": usdm.get("usdmVersion") if isinstance(usdm.get("usdmVersion"), str) else None,
        "sourcePath": str(usdm_path),
        "designs": designs_built,
        "soaTablesFound": found if isinstance(found, int) else None,
        "soaTablesModelled": modelled,
        "sourceTables": source_tables,
        "upstreamUnresolved": unresolved,
        "upstreamConfidence": upstream.get("confidence"),
    }


# ── rendering ────────────────────────────────────────────────────────────────

CSS = """
#soa-report {
  --soa-bg: #ffffff;
  --soa-panel: #f7f8fa;
  --soa-ink: #17181d;
  --soa-muted: #6b7280;
  --soa-line: #dfe2e8;
  --soa-line-strong: #b9bec9;
  --soa-accent: #1f5fd0;
  --soa-yes-bg: #e7f2ea;
  --soa-yes-ink: #1c6b3d;
  --soa-unknown-bg: #fdf3d8;
  --soa-unknown-ink: #8a5a06;
  --soa-unknown-edge: #e0b64b;
  --soa-gap-bg: #fff8ec;
  --soa-gap-edge: #e0b64b;
  --soa-amb-bg: #fdeeee;
  --soa-amb-edge: #d97b7b;
  --soa-amb-ink: #9a2f2f;
  font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  color: var(--soa-ink);
  background: var(--soa-bg);
  box-sizing: border-box;
}
#soa-report *, #soa-report *::before, #soa-report *::after { box-sizing: inherit; }

@media (prefers-color-scheme: dark) {
  #soa-report:not(.soa-force-light) {
    --soa-bg: #0f1117; --soa-panel: #171a22; --soa-ink: #e4e6ec; --soa-muted: #9aa1ae;
    --soa-line: #2b2f3a; --soa-line-strong: #454b5a; --soa-accent: #6f9dee;
    --soa-yes-bg: #16321f; --soa-yes-ink: #7fd6a0;
    --soa-unknown-bg: #3a2f12; --soa-unknown-ink: #e9c46a; --soa-unknown-edge: #7a6120;
    --soa-gap-bg: #241d0d; --soa-gap-edge: #7a6120;
    --soa-amb-bg: #2c1618; --soa-amb-edge: #7d3436; --soa-amb-ink: #f0a3a3;
  }
}
html.dark #soa-report {
  --soa-bg: #0f1117; --soa-panel: #171a22; --soa-ink: #e4e6ec; --soa-muted: #9aa1ae;
  --soa-line: #2b2f3a; --soa-line-strong: #454b5a; --soa-accent: #6f9dee;
  --soa-yes-bg: #16321f; --soa-yes-ink: #7fd6a0;
  --soa-unknown-bg: #3a2f12; --soa-unknown-ink: #e9c46a; --soa-unknown-edge: #7a6120;
  --soa-gap-bg: #241d0d; --soa-gap-edge: #7a6120;
  --soa-amb-bg: #2c1618; --soa-amb-edge: #7d3436; --soa-amb-ink: #f0a3a3;
}

#soa-report h1 { font-size: 18px; margin: 0 0 2px; font-weight: 650; letter-spacing: -0.01em; }
#soa-report h2 { font-size: 14px; margin: 22px 0 8px; font-weight: 620; }
#soa-report h4 { font-size: 13px; margin: 0 0 6px; font-weight: 620; }
.soa-table-title {
  font-size: 12.5px !important; margin: 20px 0 8px !important; font-weight: 650 !important;
  padding-left: 8px; border-left: 3px solid var(--soa-accent);
}
#soa-report h3 { font-size: 13px; margin: 0 0 6px; font-weight: 620; }
#soa-report p { margin: 0 0 8px; }
#soa-report code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
  background: var(--soa-panel); border: 1px solid var(--soa-line);
  border-radius: 3px; padding: 0 3px;
}

.soa-head { border-bottom: 1px solid var(--soa-line); padding-bottom: 12px; margin-bottom: 14px; }
.soa-sub { color: var(--soa-muted); font-size: 12px; }
.soa-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.soa-chip {
  display: inline-flex; align-items: baseline; gap: 5px;
  background: var(--soa-panel); border: 1px solid var(--soa-line);
  border-radius: 999px; padding: 2px 10px; font-size: 11.5px;
}
.soa-chip b { font-weight: 620; }
.soa-chip.soa-chip-gap { background: var(--soa-gap-bg); border-color: var(--soa-gap-edge); }

.soa-panel {
  background: var(--soa-panel); border: 1px solid var(--soa-line);
  border-radius: 8px; padding: 12px 14px; margin: 0 0 14px;
}
.soa-panel.soa-warn { background: var(--soa-gap-bg); border-color: var(--soa-gap-edge); }
.soa-panel.soa-bad { background: var(--soa-amb-bg); border-color: var(--soa-amb-edge); }
.soa-panel.soa-ok { border-left: 3px solid var(--soa-yes-ink); }

.soa-gaps { list-style: none; margin: 8px 0 0; padding: 0; }
.soa-gaps li {
  display: grid; grid-template-columns: 84px 1fr; gap: 10px;
  padding: 5px 0; border-top: 1px solid var(--soa-line); font-size: 12px;
}
.soa-gaps li:first-child { border-top: none; }
.soa-tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700;
  border-radius: 3px; padding: 1px 5px; text-align: center; height: fit-content;
}
.soa-tag-missing { background: var(--soa-unknown-bg); color: var(--soa-unknown-ink); }
.soa-tag-ambiguous { background: var(--soa-amb-bg); color: var(--soa-amb-ink); }
.soa-tag-drawn { background: var(--soa-yes-bg); color: var(--soa-yes-ink); }
.soa-gap-scope { display: block; color: var(--soa-muted); font-size: 11px; margin-top: 1px; }

.soa-toolbar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin: 0 0 10px; padding: 8px 10px;
  background: var(--soa-panel); border: 1px solid var(--soa-line); border-radius: 8px;
  position: relative; z-index: 6;
}
.soa-toolbar input[type="search"] {
  flex: 1 1 190px; min-width: 150px; padding: 5px 9px; font: inherit; font-size: 12px;
  color: var(--soa-ink); background: var(--soa-bg);
  border: 1px solid var(--soa-line-strong); border-radius: 6px;
}
.soa-toolbar input[type="search"]:focus-visible { outline: 2px solid var(--soa-accent); outline-offset: 1px; }
.soa-toggle { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer; user-select: none; }
.soa-btn {
  font: inherit; font-size: 12px; cursor: pointer; color: var(--soa-ink);
  background: var(--soa-bg); border: 1px solid var(--soa-line-strong);
  border-radius: 6px; padding: 4px 10px;
}
.soa-btn:hover { border-color: var(--soa-accent); color: var(--soa-accent); }
.soa-count { margin-left: auto; color: var(--soa-muted); font-size: 11.5px; white-space: nowrap; }

.soa-legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 10px; font-size: 11.5px; color: var(--soa-muted); }
.soa-legend span { display: inline-flex; align-items: center; gap: 5px; }
.soa-swatch {
  width: 17px; height: 17px; border-radius: 4px; display: inline-flex;
  align-items: center; justify-content: center; font-size: 11px; font-weight: 700;
  border: 1px solid var(--soa-line);
}
.soa-swatch.yes { background: var(--soa-yes-bg); color: var(--soa-yes-ink); }
.soa-swatch.no { background: var(--soa-bg); color: var(--soa-muted); }
.soa-swatch.unknown { background: var(--soa-unknown-bg); color: var(--soa-unknown-ink); border-color: var(--soa-unknown-edge); }

.soa-scroll {
  max-height: 620px; overflow: auto;
  border: 1px solid var(--soa-line); border-radius: 8px; background: var(--soa-bg);
}
/* Fixed layout with an explicit colgroup: in auto layout the widest epoch
   label or activity name sets its column's width, so the grid comes out
   irregular and `max-width` on a cell is ignored. The trailing spacer column
   has no declared width and absorbs whatever slack is left over, which keeps
   the data columns at exactly their declared size. */
.soa-table { border-collapse: separate; border-spacing: 0; table-layout: fixed; min-width: 100%; }
.soa-table col.soa-spacer-col { width: auto; }
.soa-table th.soa-spacer, .soa-table td.soa-spacer { border-right: none; background: var(--soa-bg); }
.soa-table th, .soa-table td {
  border-right: 1px solid var(--soa-line); border-bottom: 1px solid var(--soa-line);
  padding: 0; margin: 0; background: var(--soa-bg); text-align: center;
}
.soa-table thead th { background: var(--soa-panel); font-weight: 620; font-size: 11.5px; }

/* Two sticky header rows. Heights are fixed so the second row's offset is
   deterministic — a wrapped label must not shift the sticky boundary. */
.soa-epoch-row th { position: sticky; top: 0; z-index: 4; height: 30px; border-bottom: 1px solid var(--soa-line-strong); }
.soa-visit-row th { position: sticky; top: 30px; z-index: 4; height: 152px; vertical-align: bottom; }
.soa-corner {
  position: sticky; left: 0; top: 0; z-index: 6; text-align: left !important;
  padding: 0 10px !important; border-right: 1px solid var(--soa-line-strong) !important;
}
/* An epoch spanning a single narrow column must ellipsise rather than widen it.
   The width comes from the markup — see the comment where it is emitted. */
.soa-epoch-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin: 0 auto; }
.soa-visit-label {
  writing-mode: vertical-rl; transform: rotate(180deg);
  max-height: 122px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  margin: 6px auto 4px; font-weight: 620;
}
.soa-visit-timing {
  writing-mode: vertical-rl; transform: rotate(180deg);
  max-height: 122px; overflow: hidden; white-space: nowrap;
  color: var(--soa-muted); font-weight: 400; font-size: 10.5px; margin: 0 auto 6px;
}
.soa-visit-row th.soa-col-unknown { background: var(--soa-unknown-bg); }
.soa-visit-row th.soa-col-unknown .soa-visit-timing { color: var(--soa-unknown-ink); }
.soa-epoch-row th.soa-epoch-missing { color: var(--soa-unknown-ink); background: var(--soa-unknown-bg); }

.soa-table tbody th.soa-row-head {
  position: sticky; left: 0; z-index: 3;
  text-align: left; font-weight: 450;
  padding: 5px 10px; border-right: 1px solid var(--soa-line-strong);
}
.soa-table tbody tr:hover th.soa-row-head, .soa-table tbody tr:hover td { background: var(--soa-panel); }
.soa-table tbody tr.soa-row-unknown th.soa-row-head { color: var(--soa-unknown-ink); }
.soa-row-flag { color: var(--soa-unknown-ink); font-weight: 700; margin-right: 4px; }

.soa-cell { height: 26px; font-size: 12px; }
.soa-cell button {
  all: unset; display: block; width: 100%; height: 100%; cursor: pointer;
  font: inherit; font-size: 12px; line-height: 26px; text-align: center;
}
.soa-cell button:focus-visible { outline: 2px solid var(--soa-accent); outline-offset: -2px; }
.soa-cell.scheduled { background: var(--soa-yes-bg); color: var(--soa-yes-ink); font-weight: 700; }
.soa-cell.unknown { background: var(--soa-unknown-bg); color: var(--soa-unknown-ink); font-weight: 700; }
.soa-cell.not-scheduled { color: var(--soa-line-strong); }
#soa-report.soa-highlight .soa-cell.unknown { box-shadow: inset 0 0 0 2px var(--soa-unknown-edge); }
.soa-table tbody tr.soa-hidden { display: none; }

.soa-detail {
  margin: 10px 0 0; padding: 10px 12px; font-size: 12px;
  background: var(--soa-panel); border: 1px solid var(--soa-line); border-radius: 8px;
}
.soa-detail dl { display: grid; grid-template-columns: 120px 1fr; gap: 3px 10px; margin: 6px 0 0; }
.soa-detail dt { color: var(--soa-muted); }
.soa-detail dd { margin: 0; }
.soa-detail-empty { color: var(--soa-muted); }
.soa-foot { margin-top: 18px; padding-top: 10px; border-top: 1px solid var(--soa-line); color: var(--soa-muted); font-size: 11px; }
"""

JS = """
(function () {
  var report = document.getElementById('soa-report');
  if (!report) return;

  var search = report.querySelector('[data-soa="search"]');
  var onlyGapRows = report.querySelector('[data-soa="only-gap-rows"]');
  var highlight = report.querySelector('[data-soa="highlight"]');
  var reset = report.querySelector('[data-soa="reset"]');
  var rows = Array.prototype.slice.call(report.querySelectorAll('tbody tr[data-activity-label]'));

  function applyFilter() {
    var needle = search && search.value ? search.value.trim().toLowerCase() : '';
    var gapsOnly = onlyGapRows ? onlyGapRows.checked : false;
    var counts = {};

    rows.forEach(function (row) {
      var label = row.getAttribute('data-activity-label') || '';
      var hasGap = row.getAttribute('data-row-gap') === 'true';
      var matches = (needle === '' || label.toLowerCase().indexOf(needle) !== -1)
        && (gapsOnly === false || hasGap);
      row.classList.toggle('soa-hidden', matches === false);
      var design = row.getAttribute('data-design') || '';
      if (matches) counts[design] = (counts[design] || 0) + 1;
    });

    report.querySelectorAll('[data-soa="visible-count"]').forEach(function (node) {
      var design = node.getAttribute('data-design') || '';
      var total = node.getAttribute('data-total') || '0';
      var shown = counts[design] || 0;
      node.textContent = shown === Number(total)
        ? total + ' activities'
        : shown + ' of ' + total + ' activities';
    });
  }

  if (search) search.addEventListener('input', applyFilter);
  if (onlyGapRows) onlyGapRows.addEventListener('change', applyFilter);
  if (highlight) {
    highlight.addEventListener('change', function () {
      report.classList.toggle('soa-highlight', highlight.checked);
    });
  }
  if (reset) {
    reset.addEventListener('click', function () {
      if (search) search.value = '';
      if (onlyGapRows) onlyGapRows.checked = false;
      if (highlight) { highlight.checked = false; report.classList.remove('soa-highlight'); }
      applyFilter();
      clearDetail();
    });
  }

  var STATE_TEXT = {
    'scheduled': 'Scheduled — an instance at this visit lists this activity.',
    'not-scheduled': 'Not scheduled — the timeline covers both this activity and this visit, and does not pair them.',
    'unknown': 'UNKNOWN — the source did not say. This is a gap in the extracted data, not a statement that the activity does not happen.'
  };

  function clearDetail() {
    report.querySelectorAll('[data-soa="detail"]').forEach(function (panel) {
      panel.innerHTML = '<span class="soa-detail-empty">Select any cell to see what the source did and did not say about it.</span>';
    });
  }

  function escapeText(value) {
    var node = document.createElement('span');
    node.textContent = value;
    return node.innerHTML;
  }

  report.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.soa-cell button') : null;
    if (!button) return;
    var cell = button.parentNode;
    var design = cell.getAttribute('data-design') || '';
    var panel = report.querySelector('[data-soa="detail"][data-design="' + design + '"]');
    if (!panel) return;

    var state = cell.getAttribute('data-state') || 'unknown';
    var fields = [
      ['Activity', cell.getAttribute('data-activity') || '—'],
      ['Visit', cell.getAttribute('data-encounter') || '—'],
      ['Visit timing', cell.getAttribute('data-timing') || 'not stated'],
      ['Epoch', cell.getAttribute('data-epoch') || 'not stated'],
      ['Activity id', cell.getAttribute('data-activity-id') || 'not stated'],
      ['Visit id', cell.getAttribute('data-encounter-id') || 'not stated'],
      ['State', STATE_TEXT[state] || state]
    ];
    panel.innerHTML = '<dl>' + fields.map(function (pair) {
      return '<dt>' + escapeText(pair[0]) + '</dt><dd>' + escapeText(pair[1]) + '</dd>';
    }).join('') + '</dl>';
  });

  clearDetail();
  applyFilter();
})();
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_gap_panel(gaps: Gaps) -> str:
    if gaps.count() == 0:
        return (
            '<div class="soa-panel soa-ok"><h3>No gaps detected</h3>'
            "<p>Every activity and visit below carries a name, the visit order is a consistent "
            "chain, and the schedule timeline accounts for every row and column. Nothing in this "
            "table is a placeholder.</p></div>"
        )

    missing = sum(1 for entry in gaps.entries if entry["severity"] == MISSING)
    ambiguous = gaps.count() - missing
    items = "".join(
        f'<li><span class="soa-tag soa-tag-{esc(entry["severity"])}">{esc(entry["severity"])}</span>'
        f'<span>{esc(entry["message"])}<code class="soa-gap-scope">{esc(entry["scope"])}</code></span></li>'
        for entry in gaps.entries
    )
    panel_class = "soa-bad" if ambiguous > 0 else "soa-warn"
    return (
        f'<div class="soa-panel {panel_class}">'
        f"<h3>{gaps.count()} gap(s) in this schedule — {missing} missing, {ambiguous} ambiguous</h3>"
        "<p>These are things the source documents did not state, or stated in a way that could not "
        "be reconciled. They are listed here rather than filled in: an "
        f'<span class="soa-swatch unknown">?</span> cell below means &ldquo;not stated&rdquo;, never '
        "&ldquo;does not happen&rdquo;.</p>"
        f'<ul class="soa-gaps">{items}</ul></div>'
    )


def render_source_tables_panel(model: dict) -> str:
    """Every SoA table the source documents contain, and whether it is drawn here."""
    tables = model["sourceTables"]
    if not tables:
        return ""

    rows = []
    for entry in tables:
        drawn = entry.get("modelled") is True
        label = entry.get("sourceLabel") or "unnamed table"
        page = entry.get("page")
        note = entry.get("note") if isinstance(entry.get("note"), str) else None
        where = entry.get("timelineId") if drawn else None
        detail = " · ".join(
            part for part in (
                f"page {page}" if isinstance(page, int) else None,
                f"drawn as {where}" if where else None,
                note,
            ) if part
        )
        rows.append(
            f'<li><span class="soa-tag soa-tag-{"drawn" if drawn else "missing"}">'
            f'{"drawn" if drawn else "not drawn"}</span>'
            f"<span>{esc(label)}"
            + (f'<code class="soa-gap-scope">{esc(detail)}</code>' if detail else "")
            + "</span></li>"
        )

    drawn_count = sum(1 for entry in tables if entry.get("modelled") is True)
    tone = "soa-panel" if drawn_count == len(tables) else "soa-panel soa-warn"
    return (
        f'<div class="{tone}">'
        f"<h3>Source schedule tables: {drawn_count} of {len(tables)} drawn below</h3>"
        "<p>Every schedule-of-activities table the extraction step found in the source documents. "
        "A table marked <b>not drawn</b> is not represented anywhere in this report.</p>"
        f'<ul class="soa-gaps">{"".join(rows)}</ul></div>'
    )


def render_unresolved_panel(model: dict) -> str:
    unresolved = model["upstreamUnresolved"]
    if not unresolved:
        return ""
    items = "".join(
        f'<li><span class="soa-tag soa-tag-missing">upstream</span>'
        f'<span>{esc(entry.get("reason") or "no reason given")}'
        f'<code class="soa-gap-scope">{esc(entry.get("path") or "path not stated")}</code></span></li>'
        for entry in unresolved
    )
    return (
        '<div class="soa-panel soa-warn">'
        f"<h3>{len(unresolved)} item(s) the extraction step could not resolve</h3>"
        f"<p>Reported by <code>{esc(UPSTREAM_STEP_ID)}</code> in its own output. Listed verbatim — "
        "these are gaps in the USDM this report was built from.</p>"
        f'<ul class="soa-gaps">{items}</ul></div>'
    )


def render_dangling_panel(design: dict) -> str:
    if not design["dangling"]:
        return ""
    items = "".join(
        f'<li><span class="soa-tag soa-tag-ambiguous">dangling</span>'
        f'<span><code>{esc(reference["field"])}</code> &rarr; <code>{esc(reference["value"])}</code> '
        f"is not declared in this design, so it could not be placed in the table"
        f'<code class="soa-gap-scope">{esc(reference["from"])}</code></span></li>'
        for reference in design["dangling"]
    )
    return (
        '<div class="soa-panel soa-bad">'
        f"<h3>{len(design['dangling'])} broken reference(s)</h3>"
        "<p>The schedule timeline points at activities or visits that the design never declares. "
        "The pairings below are therefore absent from the table rather than guessed at.</p>"
        f'<ul class="soa-gaps">{items}</ul></div>'
    )


def epoch_groups(columns: list[dict]) -> list[tuple[str | None, int]]:
    """Consecutive runs of columns sharing an epoch, for the grouping header row."""
    groups: list[tuple[str | None, int]] = []
    for column in columns:
        label = column["epochLabel"]
        if groups and groups[-1][0] == label:
            groups[-1] = (label, groups[-1][1] + 1)
        else:
            groups.append((label, 1))
    return groups


# Column widths, in px. The table uses `table-layout: fixed`, so these are the
# real widths rather than minimums, and the visit columns stay uniform however
# long a visit name or epoch label is.
ACTIVITY_COLUMN_PX = 250
VISIT_COLUMN_PX = 46

CELL_GLYPH = {SCHEDULED: "&#10005;", NOT_SCHEDULED: "&middot;", UNKNOWN: "?"}
CELL_WORD = {SCHEDULED: "scheduled", NOT_SCHEDULED: "not scheduled", UNKNOWN: "not stated"}


def render_design(design: dict) -> str:
    counts = design["counts"]

    if counts["activities"] == 0 or counts["encounters"] == 0:
        return (
            f'<h2>{esc(design["label"])}</h2>'
            f"{render_dangling_panel(design)}"
            '<div class="soa-panel soa-bad"><h3>No table could be drawn</h3>'
            f"<p>This design declares {counts['activities']} activity(ies) and "
            f"{counts['encounters']} visit(s). A schedule of activities needs at least one of each. "
            "Nothing is shown below because there is nothing to show — see the gaps above.</p></div>"
        )

    tables = design["tables"]
    intro = ""
    if len(tables) > 1:
        names = ", ".join(f"<b>{esc(table['label'])}</b>" for table in tables)
        intro = (
            '<div class="soa-panel">'
            f"<h3>{len(tables)} schedules in this design</h3>"
            f"<p>This design declares {len(tables)} schedule timelines — {names} — and each is drawn "
            "as its own table below. They are deliberately not merged: visits from schedules that are "
            "alternatives to one another, or that apply to different cycles, would otherwise sit side "
            "by side as though they belonged to one schedule. An activity belonging to a different "
            "schedule shows as unknown here, not as absent.</p></div>"
        )

    return (
        f'<h2>{esc(design["label"])}</h2>'
        f"{render_dangling_panel(design)}{intro}"
        + "".join(
            render_table(table, f"{design['id']}--{index}", len(tables))
            for index, table in enumerate(tables)
        )
    )


def render_table(design: dict, design_id: str, table_count: int) -> str:
    """Render one schedule timeline's table.

    `design_id` is the per-table id the toolbar, counter and detail panel are
    keyed on, so each table's controls only touch its own rows.
    """
    counts = design["counts"]
    heading = f'<h3 class="soa-table-title">{esc(design["label"])}</h3>' if table_count > 1 else ""

    if counts["encounters"] == 0:
        return (
            f"{heading}"
            '<div class="soa-panel soa-bad"><h4>No visits in this schedule</h4>'
            "<p>No visit could be placed in this schedule, so no table is drawn for it — see the "
            "gaps above.</p></div>"
        )

    # The label is pinned to the exact pixel width of the columns it spans.
    # Left to size itself it becomes the group's min-content width, and a long
    # epoch name over a single column widens that column out of the grid.
    epoch_cells = "".join(
        '<th colspan="{size}" class="{cls}" scope="colgroup" title="{title}">'
        '<div class="soa-epoch-label" style="width:{width}px">{label}</div></th>'.format(
            size=size,
            width=size * VISIT_COLUMN_PX - 6,
            cls="" if label else "soa-epoch-missing",
            title=esc(label) if label else "the epoch of these visits is not stated",
            label=esc(label) if label else "epoch not stated",
        )
        for label, size in epoch_groups(design["columns"])
    )
    colgroup = (
        f'<colgroup><col style="width:{ACTIVITY_COLUMN_PX}px">'
        f'<col span="{counts["encounters"]}" style="width:{VISIT_COLUMN_PX}px">'
        '<col class="soa-spacer-col"></colgroup>'
    )

    visit_cells = []
    for column in design["columns"]:
        classes = "" if column["hasData"] else "soa-col-unknown"
        timing = column["timing"] or "timing not stated"
        title = f'{column["label"]} — {timing}'
        if column["type"]:
            title = f'{title} — {column["type"]}'
        visit_cells.append(
            f'<th class="{classes}" scope="col" title="{esc(title)}">'
            f'<div class="soa-visit-label">{esc(column["label"])}</div>'
            f'<div class="soa-visit-timing">{esc(timing)}</div></th>'
        )

    body_rows = []
    for row in design["rows"]:
        # A gap that belongs to this activity, as opposed to one inherited from a
        # visit column that has no scheduling data at all. Without that
        # distinction a single unknown column marks every row and the filter
        # stops discriminating.
        row_gap = row["hasData"] is False or any(
            cell["state"] == UNKNOWN and cell["column"]["hasData"] for cell in row["cells"]
        )
        row_classes = "soa-row-unknown" if row["hasData"] is False else ""
        flag = '<span class="soa-row-flag" title="no scheduled instance references this activity">?</span>' if row["hasData"] is False else ""
        cells = []
        for cell in row["cells"]:
            column = cell["column"]
            state = cell["state"]
            label = f'{row["label"]} at {column["label"]}: {CELL_WORD[state]}'
            cells.append(
                f'<td class="soa-cell {state}" data-state="{esc(state)}" data-design="{esc(design_id)}"'
                f' data-activity="{esc(row["label"])}" data-activity-id="{esc(row["id"])}"'
                f' data-encounter="{esc(column["label"])}" data-encounter-id="{esc(column["id"])}"'
                f' data-timing="{esc(column["timing"])}" data-epoch="{esc(column["epochLabel"])}">'
                f'<button type="button" title="{esc(label)}" aria-label="{esc(label)}">'
                f"{CELL_GLYPH[state]}</button></td>"
            )
        title = row["description"] or row["label"]
        body_rows.append(
            f'<tr class="{row_classes}" data-design="{esc(design_id)}"'
            f' data-activity-label="{esc(row["label"])}" data-row-gap="{"true" if row_gap else "false"}">'
            f'<th class="soa-row-head" scope="row" title="{esc(title)}">{flag}{esc(row["label"])}</th>'
            f'{"".join(cells)}<td class="soa-spacer"></td></tr>'
        )

    return f"""{heading}
<div class="soa-toolbar">
  <input type="search" data-soa="search" placeholder="Filter activities&hellip;" aria-label="Filter activities">
  <label class="soa-toggle" title="Activities never scheduled anywhere, or unknown at a visit that does have scheduling data. Excludes cells that are unknown only because their whole visit column has no data."><input type="checkbox" data-soa="only-gap-rows"> Only activities with their own gaps</label>
  <label class="soa-toggle"><input type="checkbox" data-soa="highlight"> Outline unknown cells</label>
  <button type="button" class="soa-btn" data-soa="reset">Reset</button>
  <span class="soa-count" data-soa="visible-count" data-design="{esc(design_id)}" data-total="{counts["activities"]}">{counts["activities"]} activities</span>
</div>
<div class="soa-legend">
  <span><span class="soa-swatch yes">&#10005;</span> scheduled ({counts["scheduled"]})</span>
  <span><span class="soa-swatch no">&middot;</span> not scheduled ({counts["notScheduled"]})</span>
  <span><span class="soa-swatch unknown">?</span> not stated &mdash; a gap, not an absence ({counts["unknown"]})</span>
</div>
<div class="soa-scroll">
  <table class="soa-table">
    {colgroup}
    <thead>
      <tr class="soa-epoch-row"><th class="soa-corner" rowspan="2" scope="col">Activity</th>{epoch_cells}<th class="soa-spacer" rowspan="2"></th></tr>
      <tr class="soa-visit-row">{"".join(visit_cells)}</tr>
    </thead>
    <tbody>{"".join(body_rows)}</tbody>
  </table>
</div>
<div class="soa-detail" data-soa="detail" data-design="{esc(design_id)}"></div>"""


def render_fragment(model: dict, gaps: Gaps) -> str:
    total = {
        "activities": sum(design["counts"]["activities"] for design in model["designs"]),
        "encounters": sum(design["counts"]["encounters"] for design in model["designs"]),
        "scheduled": sum(design["counts"]["scheduled"] for design in model["designs"]),
        "unknown": sum(design["counts"]["unknown"] for design in model["designs"]),
    }
    chips = [
        ("NCT id", model["nctId"] or "not stated"),
        ("Phase", model["phase"] or "not stated"),
        ("USDM", model["usdmVersion"] or "version not stated"),
        ("Visits", total["encounters"]),
        ("Activities", total["activities"]),
    ]
    chip_html = "".join(
        f'<span class="soa-chip{"" if value not in ("not stated", "version not stated") else " soa-chip-gap"}">'
        f"{esc(name)} <b>{esc(value)}</b></span>"
        for name, value in chips
    )
    gap_chip = (
        f'<span class="soa-chip soa-chip-gap">Gaps <b>{gaps.count()}</b></span>'
        if gaps.count() > 0
        else '<span class="soa-chip">Gaps <b>0</b></span>'
    )
    unknown_chip = (
        f'<span class="soa-chip soa-chip-gap">Unknown cells <b>{total["unknown"]}</b></span>'
        if total["unknown"] > 0
        else '<span class="soa-chip">Unknown cells <b>0</b></span>'
    )

    designs = "".join(render_design(design) for design in model["designs"]) or (
        '<div class="soa-panel soa-bad"><h3>No study design</h3>'
        "<p>The USDM carries no <code>studyDesigns</code>, so there is no schedule to render.</p></div>"
    )

    title = model["title"] or "Study title not stated"
    return f"""<style>{CSS}</style>
<div id="soa-report">
  <div class="soa-head">
    <h1>Schedule of Activities</h1>
    <div class="soa-sub">{esc(title)}</div>
    <div class="soa-meta">{chip_html}{gap_chip}{unknown_chip}</div>
  </div>
  {render_gap_panel(gaps)}
  {render_source_tables_panel(model)}
  {render_unresolved_panel(model)}
  {designs}
  <div class="soa-foot">
    Built from <code>{esc(Path(model["sourcePath"]).name)}</code> as delivered by
    <code>{esc(UPSTREAM_STEP_ID)}</code> ({esc(model["sourcePath"])}).
    {esc(total["scheduled"])} scheduled pairing(s), {esc(total["unknown"])} unknown cell(s).
    Cells are derived from <code>studyDesign.scheduleTimelines[].instances[]</code>; a cell is only
    called &ldquo;not scheduled&rdquo; when the timeline covers both its activity and its visit.
  </div>
</div>
<script>{JS}</script>"""


def render_standalone(fragment: str, model: dict) -> str:
    title = model["title"] or "Schedule of Activities"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Schedule of Activities — {esc(model["nctId"] or "study")}</title>
<meta name="description" content="{esc(title)}">
<style>html, body {{ margin: 0; padding: 0; }} body {{ padding: 20px; background: #fff; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #0f1117; }} }}</style>
</head>
<body>
{fragment}
</body>
</html>"""


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    step_input = read_step_input()
    upstream = upstream_output(step_input)
    usdm_path = locate_usdm()
    print(f"reading USDM from {usdm_path}")

    gaps = Gaps()
    usdm = load_usdm(usdm_path)
    model = build_model(usdm, usdm_path, upstream, gaps)

    fragment = render_fragment(model, gaps)
    (OUTPUT_DIR / "presentation.html").write_text(fragment)
    (OUTPUT_DIR / "schedule-of-activities.html").write_text(render_standalone(fragment, model))

    designs = [
        {
            "id": design["id"],
            "label": design["label"],
            **design["counts"],
            "schedules": [
                {"label": table["label"], **table["counts"]} for table in design["tables"]
            ],
            "danglingReferences": len(design["dangling"]),
        }
        for design in model["designs"]
    ]
    unknown_total = sum(design["counts"]["unknown"] for design in model["designs"])
    renderable = any(
        design["counts"]["activities"] > 0 and design["counts"]["encounters"] > 0
        for design in model["designs"]
    )

    result = {
        "nctId": model["nctId"],
        "studyTitle": model["title"],
        "studyPhase": model["phase"],
        "usdmVersion": model["usdmVersion"],
        "usdmSource": str(usdm_path),
        "tableRendered": renderable,
        "soaComplete": gaps.count() == 0 and unknown_total == 0 and renderable,
        "soaTablesFound": model["soaTablesFound"],
        "soaTablesModelled": model["soaTablesModelled"],
        "sourceTables": model["sourceTables"],
        "designs": designs,
        "gapCount": gaps.count(),
        "gaps": gaps.entries,
        "danglingReferences": [
            reference for design in model["designs"] for reference in design["dangling"]
        ],
        "outputFiles": ["presentation.html", "schedule-of-activities.html"],
        "summary": summarise(designs, gaps, unknown_total, renderable),
    }
    (OUTPUT_DIR / "result.json").write_text(json.dumps(result, indent=2))
    print(result["summary"])


def summarise(designs: list[dict], gaps: Gaps, unknown_total: int, renderable: bool) -> str:
    if not renderable:
        return (
            f"No schedule could be drawn — see the {gaps.count()} gap(s) in the report; "
            "the USDM has no activities or no visits"
        )
    activities = sum(design["activities"] for design in designs)
    encounters = sum(design["encounters"] for design in designs)
    scheduled = sum(design["scheduled"] for design in designs)
    head = f"Rendered {activities} activity(ies) x {encounters} visit(s), {scheduled} scheduled"
    if gaps.count() == 0 and unknown_total == 0:
        return f"{head}, no gaps"
    return f"{head}, {unknown_total} unknown cell(s) and {gaps.count()} gap(s) flagged for review"


if __name__ == "__main__":
    main()
