"""Checks for scripts/schedule_issues.py. Stdlib only.

    python3 tests/test_schedule_issues.py

Also collectable by pytest. Nothing here touches the network: `gh_graphql` is
replaced by a fake that serves canned issues and records every mutation, so the
flow checks assert on what would have been written and on the state that results.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
import schedule_issues as s  # noqa: E402

D = date
NAMES = {"start": "Start date", "target": "Target date", "duration": "Duration"}
IDS = {"start": "IFD_start", "target": "IFD_target", "duration": "IFN_dur"}
SUBS = [2, 3, 4, 5, 6]

WEEKS = {2: 2, 3: 1, 4: 3, 5: 1, 6: 2}
CREATED = {2: "2026-10-01", 3: "2026-10-02", 4: "2026-10-03", 5: "2026-10-05",
           6: "2026-10-06"}
NODE_TO_NUMBER = {f"NODE{n}": n for n in [1, *SUBS]}

state = {}   # issue number -> {field name: value}
writes = []  # (node id, [(fieldId, dateValue), ...])


def check(name, got, want):
    assert got == want, f"{name}\n  got  {got}\n  want {want}"
    print(f"OK  {name}")


# --------------------------------------------------------------------------
# the pure date arithmetic
# --------------------------------------------------------------------------
def iso(slots):
    return [(slot[0].isoformat(), slot[1].isoformat()) if slot else None
            for slot in slots]


def test_span_days():
    check("span_days 1w/2w/half week/quarter+1",
          [s.span_days(w) for w in (1, 2, 0.5, 1.25)], [6, 13, 3, 8])


def test_compute_cascade():
    check("chain 2w/1w/3w from 2026-10-04",
          iso(s.compute_cascade(D(2026, 10, 4), [2, 1, 3])),
          [("2026-10-04", "2026-10-17"), ("2026-10-18", "2026-10-24"),
           ("2026-10-25", "2026-11-14")])

    chain = s.compute_cascade(D(2026, 10, 4), [2, 1, 3, 5])
    check("every sub starts the day after the previous target",
          [nxt[0] for nxt in chain[1:]],
          [prev[1] + timedelta(days=1) for prev in chain[:-1]])

    check("first sub starts on the parent's start day",
          s.compute_cascade(D(2026, 10, 4), [1])[0][0], D(2026, 10, 4))

    check("an unset duration stops the chain and everything after it",
          iso(s.compute_cascade(D(2026, 10, 4), [2, None, 1, 4])),
          [("2026-10-04", "2026-10-17"), None, None, None])

    check("leap day does not shift the week boundary",
          iso(s.compute_cascade(D(2028, 2, 27), [1, 1])),
          [("2028-02-27", "2028-03-04"), ("2028-03-05", "2028-03-11")])


def test_last_target():
    check("parent target is the last scheduled slot",
          s.last_target(s.compute_cascade(D(2026, 10, 4), [2, 1, None])), D(2026, 10, 24))
    check("trailing slot when everything is scheduled",
          s.last_target(s.compute_cascade(D(2026, 10, 4), [2])), D(2026, 10, 17))
    check("no target at all when nothing is schedulable",
          s.last_target(s.compute_cascade(D(2026, 10, 4), [None])), None)


def test_total_duration():
    check("the parent's weeks are its sub tasks' weeks added up",
          s.total_duration([2, 1, 3, 1, 2]), 9.0)
    check("part weeks add up too", s.total_duration([0.5, 1.25]), 1.75)
    check("binary float noise does not leak into the field",
          s.total_duration([0.1, 0.2]), 0.3)
    check("an unknown duration makes the total unknown",
          s.total_duration([2, None, 1]), None)
    check("no sub tasks means no total", s.total_duration([]), None)


def test_parsers():
    check("a blank duration reads as unset", s.parse_duration(None), (None, None))
    check("an empty duration reads as unset", s.parse_duration(""), (None, None))
    check("a string number parses", s.parse_duration("3")[0], 3.0)
    check("a part week parses", s.parse_duration(3.5)[0], 3.5)
    check("zero is rejected", bool(s.parse_duration(0)[1]), True)
    check("a negative duration is rejected", bool(s.parse_duration(-2)[1]), True)
    check("a non-number is rejected", bool(s.parse_duration("abc")[1]), True)
    check("a date parses", s.parse_date("2026-10-04T00:00:00Z"), D(2026, 10, 4))
    check("an unparseable date reads as unset", s.parse_date("junk"), None)


# --------------------------------------------------------------------------
# the fake transport
# --------------------------------------------------------------------------
def raw(number, parent=None):
    values = state.get(number, {})
    nodes = [{"value": value, "field": {"name": name}}
             for name, value in values.items()]
    return {
        "id": f"NODE{number}",
        "number": number,
        "createdAt": f"{CREATED.get(number, '2026-09-30')}T09:00:00Z",
        "parent": {"number": parent} if parent else None,
        "issueFieldValues": {"nodes": nodes},
    }


class FakeGraphQL:
    def __init__(self, subs):
        self.subs = subs

    def __call__(self, query, variables=None):
        v = variables or {}
        if query is s.FIELDS_QUERY:
            return {"repository": {"issueFields": {"nodes": [
                {"name": "Start date", "id": IDS["start"], "dataType": "DATE"},
                {"name": "Target date", "id": IDS["target"], "dataType": "DATE"},
                {"name": "Duration", "id": IDS["duration"], "dataType": "NUMBER"},
                {"name": "Priority", "id": "IFX_prio", "dataType": "SINGLE_SELECT"},
            ]}}}
        if query is s.SUB_ISSUES_QUERY:
            return {"repository": {"issue": {"subIssues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": n} for n in self.subs]}}}}
        if query is s.ISSUE_QUERY:
            n = v["number"]
            return {"repository": {"issue": raw(n, parent=1 if n in self.subs else None)}}
        if query is s.SET_VALUES_MUTATION:
            writes.append((v["id"], [(f["fieldId"], f.get("dateValue", f.get("numberValue")))
                                     for f in v["fields"]]))
            for field in v["fields"]:
                name = {"IFD_start": "Start date",
                        "IFD_target": "Target date",
                        "IFN_dur": "Duration"}[field["fieldId"]]
                key = "dateValue" if "dateValue" in field else "numberValue"
                state[NODE_TO_NUMBER[v["id"]]][name] = field[key]
            return {"setIssueFieldValue": {"clientMutationId": None}}
        raise AssertionError("unexpected query")


def reset(parent_start="2026-10-04", parent_target="2026-11-30", durations=True,
          sub_dates=None, parent_duration=None):
    """Start from a clean, known issue set and install the fake transport."""
    writes.clear()
    state.clear()
    state[1] = {"Start date": parent_start, "Target date": parent_target}
    if parent_duration is not None:
        state[1]["Duration"] = parent_duration
    for n in SUBS:
        state[n] = {}
        if durations:
            state[n]["Duration"] = WEEKS[n]
    for n, (start, target) in (sub_dates or {}).items():
        state[n]["Start date"] = start
        state[n]["Target date"] = target
    s.gh_graphql = FakeGraphQL(SUBS)
    return s.Issue(raw(1), NAMES)


def ids_written(node):
    """{fieldId: value} across every mutation for one issue."""
    out = {}
    for nid, fields in writes:
        if nid == node:
            out.update(dict(fields))
    return out


def touched(node):
    return any(nid == node for nid, _ in writes)


# --------------------------------------------------------------------------
# flow A: full recompute
# --------------------------------------------------------------------------
def test_full_recompute():
    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})
    check("full recompute succeeds", s.run_full("o", "r", parent, IDS, NAMES, dry_run=False), 0)
    check("the chain 2w/1w/3w/1w/2w lands on these dates", [
        (state[n].get("Start date"), state[n].get("Target date")) for n in SUBS],
        [("2026-10-04", "2026-10-17"), ("2026-10-18", "2026-10-24"),
         ("2026-10-25", "2026-11-14"), ("2026-11-15", "2026-11-21"),
         ("2026-11-22", "2026-12-05")])
    check("parent target is the last sub's target",
          ids_written("NODE1"), {"IFD_target": "2026-12-05", "IFN_dur": 9.0})
    check("parent start is never rewritten", "IFD_start" in ids_written("NODE1"), False)
    check("a sub that is already right is not written at all", touched("NODE2"), False)
    check("one mutation per changed issue", len(writes), 5)


def test_parent_duration_is_the_sum_of_sub_tasks():
    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("the parent's Duration is the sum of its sub tasks",
          state[1].get("Duration"), 9.0)

    parent = reset(parent_target="2026-12-05", parent_duration=9,
                   sub_dates={2: ("2026-10-04", "2026-10-17")})
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("a parent that already adds up is not written at all",
          touched("NODE1"), False)

    parent = reset(parent_duration=99,  # hand-set and wrong
                   sub_dates={2: ("2026-10-04", "2026-10-17")})
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("a hand-set parent Duration loses to the sum",
          ids_written("NODE1").get("IFN_dur"), 9.0)

    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})
    state[4].pop("Duration")
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("the sum stops where the chain stops, so #5 and #6 are not counted",
          ids_written("NODE1").get("IFN_dur"), 3.0)

    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})
    for n in SUBS:
        state[n].pop("Duration")
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("nothing is schedulable, so no parent Duration is written",
          "IFN_dur" in ids_written("NODE1"), False)


def test_parent_start_is_seeded_once():
    parent = reset(parent_start=None)
    state[1].pop("Start date")
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("an empty parent start is seeded from createdAt",
          ids_written("NODE1").get("IFD_start"), "2026-09-30")

    parent = reset(parent_start="2027-01-04", parent_target=None)
    state[1].pop("Target date")
    s.run_full("o", "r", parent, IDS, NAMES, dry_run=False)
    check("a stored parent start wins over createdAt as the anchor",
          ids_written("NODE2").get("IFD_start"), "2027-01-04")
    check("and the chain is computed from it",
          ids_written("NODE1").get("IFD_target"), "2027-03-07")
    check("but it is still not written back", "IFD_start" in ids_written("NODE1"), False)


def test_write_values_protects_the_parent_start():
    parent = reset(parent_start="2026-10-04")
    notes = s.write_values(parent, D(2027, 5, 1), None, IDS, NAMES,
                           allow_start_write=False, dry_run=False)
    check("a caller asking to move a parent start is refused in the note",
          (ids_written("NODE1"), "never overwritten" in notes[0]), ({}, True))


def test_unusable_duration_fails_the_run():
    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})
    state[4]["Duration"] = 0
    check("a zero duration fails the run", s.run_full("o", "r", parent, IDS, NAMES, dry_run=False), 1)
    check("and the chain stops there", touched("NODE5"), False)


def test_dry_run_writes_nothing():
    parent = reset()
    check("dry run succeeds", s.run_full("o", "r", parent, IDS, NAMES, dry_run=True), 0)
    check("dry run issues no mutation", writes, [])


# --------------------------------------------------------------------------
# flow B: partial cascade from the edited sub task
# --------------------------------------------------------------------------
ALIGNED = {2: ("2026-10-04", "2026-10-17"), 3: ("2026-10-18", "2026-10-24")}


def test_cascade_only_touches_the_edited_sub_and_its_successors():
    parent = reset(sub_dates={**ALIGNED, 4: ("2026-10-25", "2099-01-01")})
    state[4]["Duration"] = 4  # lengthened, start kept
    edited = s.Issue(raw(4, parent=1), NAMES)
    check("cascade succeeds",
          s.run_cascade("o", "r", edited, parent, IDS, NAMES, dry_run=False), 0)
    check("earlier subs are untouched", [touched(f"NODE{n}") for n in (2, 3)],
          [False, False])
    check("the edited sub keeps its start and gets a new target",
          (state[4]["Start date"], state[4]["Target date"]),
          ("2026-10-25", "2026-11-21"))
    check("successors shift by the new span",
          (ids_written("NODE5").get("IFD_start"), ids_written("NODE6").get("IFD_target")),
          ("2026-11-22", "2026-12-12"))
    check("the parent target follows the shifted tail",
          ids_written("NODE1"), {"IFD_target": "2026-12-12", "IFN_dur": 10.0})
    check("a cascade re-sums the parent Duration as well",
          state[1].get("Duration"), 10.0)


def test_cascade_cannot_sum_past_an_upstream_gap():
    parent = reset(sub_dates=ALIGNED)
    state[2].pop("Duration")  # cleared by hand after its dates were written
    edited = s.Issue(raw(3, parent=1), NAMES)
    s.run_cascade("o", "r", edited, parent, IDS, NAMES, dry_run=False)
    check("the parent target is still recomputed",
          ids_written("NODE1").get("IFD_target"), "2026-12-05")
    check("but a total that would be short is never written",
          "IFN_dur" in ids_written("NODE1"), False)


def test_cascade_stops_at_an_unset_duration():
    parent = reset(sub_dates=ALIGNED)
    state[4].pop("Duration")
    edited = s.Issue(raw(3, parent=1), NAMES)
    s.run_cascade("o", "r", edited, parent, IDS, NAMES, dry_run=False)
    check("nothing after the gap is written",
          [touched(f"NODE{n}") for n in (4, 5, 6)], [False, False, False])
    check("the parent target falls back to the last scheduled sub",
          ids_written("NODE1"), {"IFD_target": "2026-10-24", "IFN_dur": 3.0})


def test_cascade_falls_back_when_the_stored_chain_has_a_hole():
    parent = reset(sub_dates={2: ("2026-10-04", "2026-10-17")})  # #3 has no target
    edited = s.Issue(raw(4, parent=1), NAMES)
    s.run_cascade("o", "r", edited, parent, IDS, NAMES, dry_run=False)
    check("it reschedules from the parent start instead",
          ids_written("NODE3").get("IFD_start"), "2026-10-18")
    check("filling the hole it needed", ids_written("NODE3").get("IFD_target"),
          "2026-10-24")


def test_cascade_falls_back_without_a_stored_start():
    parent = reset(sub_dates=ALIGNED)
    edited = s.Issue(raw(5, parent=1), NAMES)  # #5 has no Start date
    s.run_cascade("o", "r", edited, parent, IDS, NAMES, dry_run=False)
    check("a sub with no start cannot anchor a cascade, so full recompute",
          ids_written("NODE5").get("IFD_start"), "2026-11-15")


# --------------------------------------------------------------------------
# how main() turns trigger context into a mode
# --------------------------------------------------------------------------
def run_main(**env):
    saved = dict(os.environ)
    os.environ.update({"REPO": "o/r", "FIELD_START": "Start date",
                       "FIELD_TARGET": "Target date", "FIELD_DURATION": "Duration"})
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        return s.main()
    finally:
        os.environ.clear()
        os.environ.update(saved)


# #4 is deliberately off the canonical chain, so the two modes disagree.
OFF_CHAIN = {**ALIGNED, 4: ("2027-06-01", "2099-01-01")}


def test_a_new_issue_recomputes_the_whole_chain():
    reset(sub_dates=OFF_CHAIN)
    check("an opened event succeeds",
          run_main(ISSUE_NUMBER="4", TRIGGER_EVENT="issues",
                   TRIGGER_ACTION="opened"), 0)
    check("full mode overwrites the sub's start to fit the chain",
          state[4]["Start date"], "2026-10-25")


def test_a_duration_change_anchors_on_the_edited_sub():
    reset(sub_dates=OFF_CHAIN)
    check("a Duration field event succeeds",
          run_main(ISSUE_NUMBER="4", TRIGGER_EVENT="issues",
                   TRIGGER_ACTION="field_added", CHANGED_FIELD="Duration"), 0)
    check("cascade keeps the sub's own start", state[4]["Start date"], "2027-06-01")
    check("and anchors the successors on it",
          ids_written("NODE5").get("IFD_start"), "2027-06-22")
    check("earlier subs stay untouched", [touched(f"NODE{n}") for n in (2, 3)],
          [False, False])


def test_an_unrelated_field_change_does_nothing():
    reset(sub_dates=OFF_CHAIN)
    check("a Start date event on a sub issue succeeds",
          run_main(ISSUE_NUMBER="4", TRIGGER_EVENT="issues",
                   TRIGGER_ACTION="field_added", CHANGED_FIELD="Start date"), 0)
    check("and writes nothing, so the just-entered value survives", writes, [])


def test_a_parent_duration_change_does_nothing():
    reset(sub_dates=OFF_CHAIN)
    check("a Duration event on the parent succeeds",
          run_main(ISSUE_NUMBER="1", TRIGGER_EVENT="issues",
                   TRIGGER_ACTION="field_added", CHANGED_FIELD="Duration"), 0)
    check("the parent's own Duration is not part of the schedule", writes, [])


def test_a_parent_start_change_reanchors_the_chain():
    reset(parent_start="2027-01-04", sub_dates=OFF_CHAIN)
    check("a Start date event on the parent succeeds",
          run_main(ISSUE_NUMBER="1", TRIGGER_EVENT="issues",
                   TRIGGER_ACTION="field_added", CHANGED_FIELD="Start date"), 0)
    check("the whole chain moves to the new anchor",
          (state[2]["Start date"], state[2]["Target date"]),
          ("2027-01-04", "2027-01-17"))
    check("the parent target follows", ids_written("NODE1").get("IFD_target"),
          "2027-03-07")
    check("the start the human just typed is not written back",
          "IFD_start" in ids_written("NODE1"), False)


def test_a_renamed_field_fails_loudly():
    reset(sub_dates=OFF_CHAIN)
    try:
        run_main(ISSUE_NUMBER="1", TRIGGER_EVENT="workflow_dispatch",
                 FIELD_DURATION="Timeline")
    except SystemExit as exit_request:
        check("a missing field name stops the run",
              "not found" in str(exit_request), True)
    else:
        check("a missing field name stops the run", "no SystemExit", "SystemExit")


def _main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nall checks passed")


if __name__ == "__main__":
    _main()
