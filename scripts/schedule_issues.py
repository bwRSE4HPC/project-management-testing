#!/usr/bin/env python3
"""Schedule a parent issue and its sub-issues from per-sub-task `Duration` values.

Model
    The parent's start date anchors the chain. Each sub task takes `Duration`
    weeks, running back to back in creation order, and the parent's target date
    is the last scheduled sub task's end date:

        parent.start  = parent Start date, or createdAt when that field is empty
        sub[n].start  = sub[n-1].target + 1 day    (sub[0].start = parent.start)
        sub[n].target = sub[n].start + 7 * duration - 1 day
        parent.target = last scheduled sub task's target

    A sub task with no `Duration` gets no dates and stops the chain there: no
    gap is assumed for it and nothing after it is scheduled. The next run picks
    it up once the value arrives.

    The parent's Start date is seeded once, when empty, and never overwritten;
    afterwards it is read back as the anchor. Every other value this script owns
    — the parent target and all sub-task dates — is recomputed unconditionally.

Flows
    full     (workflow_dispatch, or an `opened` issue)
        Recomputes the whole chain from the parent's start. New sub tasks land
        last, so `opened` on a sub task is a full recompute too.
    cascade  (`field_added` / `field_removed` on a sub issue, Duration only)
        Anchors on the edited sub task's stored start date and rewrites it, its
        successors and the parent target. Earlier sub tasks are left alone. If
        the anchor is missing, or an earlier sub task has no target to hand off
        from, the run falls back to a full recompute.

Transport
    Every call is GraphQL through `gh api graphql`, which reads GITHUB_TOKEN
    from the environment. Field config is by *name*, resolved to node IDs once
    per run; a name that no longer exists, or has the wrong data type, is a hard
    error rather than a silent no-op, because org default fields are
    admin-renameable.

Dry run
    Set DRY_RUN=1 to read and report without writing anything.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, timedelta
from typing import Any

# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #

FIELDS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    issueFields(first: 50) { nodes {
      ... on IssueFieldDate   { name id dataType }
      ... on IssueFieldNumber { name id dataType }
    } }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      createdAt
      parent { number }
      issueFieldValues(first: 20) { nodes {
        ... on IssueFieldDateValue   { value field { ... on IssueFieldDate   { name } } }
        ... on IssueFieldNumberValue { value field { ... on IssueFieldNumber { name } } }
      } }
    }
  }
}
"""

SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { number }
      }
    }
  }
}
"""

SET_VALUES_MUTATION = """
mutation($id: ID!, $fields: [IssueFieldCreateOrUpdateInput!]!) {
  setIssueFieldValue(input: { issueId: $id, issueFields: $fields }) { clientMutationId }
}
"""


class GhError(RuntimeError):
    pass


def gh_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one GraphQL call through `gh` and return its `data` object."""
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    proc = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=json.dumps(body).encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise GhError(detail or "gh api graphql exited non-zero")
    payload = json.loads(proc.stdout)
    if payload.get("errors"):
        raise GhError("; ".join(str(e.get("message", e)) for e in payload["errors"]))
    return payload["data"]


# --------------------------------------------------------------------------- #
# Issue model
# --------------------------------------------------------------------------- #


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_duration(value: Any) -> tuple[float | None, str | None]:
    """Return (weeks, error). Blank is unset; a non-positive value is an error."""
    if value in (None, ""):
        return None, None
    try:
        weeks = float(value)
    except (TypeError, ValueError):
        return None, f"{value!r} is not a number"
    if weeks <= 0:
        return None, f"{weeks:g} weeks is not a positive duration"
    return weeks, None


class Issue:
    """One issue plus the three field values this script cares about."""

    def __init__(self, raw: dict[str, Any], names: dict[str, str]):
        values = {
            node["field"]["name"]: node["value"]
            for node in raw["issueFieldValues"]["nodes"]
            if node.get("field")
        }
        self.number: int = raw["number"]
        self.node_id: str = raw["id"]
        self.created: date = date.fromisoformat(raw["createdAt"][:10])
        self.parent: int | None = raw["parent"]["number"] if raw.get("parent") else None
        self.stored_start = parse_date(values.get(names["start"]))
        self.stored_target = parse_date(values.get(names["target"]))
        self.duration, self.duration_error = parse_duration(values.get(names["duration"]))

    @property
    def label(self) -> str:
        return f"#{self.number}"

    def show(self, value: date | None) -> str:
        return value.isoformat() if value else "-"


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def fetch_issue(owner: str, repo: str, number: int, names: dict[str, str]) -> Issue:
    data = gh_graphql(ISSUE_QUERY, {"owner": owner, "name": repo, "number": number})
    raw = data["repository"]["issue"]
    if not raw:
        raise GhError(f"issue #{number} not found in {owner}/{repo}")
    return Issue(raw, names)


def fetch_chain(owner: str, repo: str, parent: int, names: dict[str, str]) -> list[Issue]:
    """Sub issues of `parent` in creation order — the order GitHub lists them in."""
    numbers: list[int] = []
    after: str | None = None
    while True:
        data = gh_graphql(
            SUB_ISSUES_QUERY,
            {"owner": owner, "name": repo, "number": parent, "after": after},
        )
        connection = data["repository"]["issue"]["subIssues"]
        numbers.extend(node["number"] for node in connection["nodes"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        after = page["endCursor"]
    subs = [fetch_issue(owner, repo, n, names) for n in numbers]
    subs.sort(key=lambda sub: (sub.created, sub.number))
    return subs


def topological_parent(owner: str, repo: str, issue: Issue, names: dict[str, str]) -> Issue:
    """Climb `parent` links to the top-level issue (normally one hop)."""
    current = issue
    for hop in range(11):
        if current.parent is None:
            return current
        current = fetch_issue(owner, repo, current.parent, names)
    raise GhError("parent chain deeper than 10, refusing to continue")


# --------------------------------------------------------------------------- #
# Date math (pure)
# --------------------------------------------------------------------------- #


def span_days(weeks: float) -> int:
    """Inclusive day span for a duration in weeks; a part week rounds up."""
    return math.ceil(7 * weeks) - 1


def compute_cascade(
    anchor: date, durations: list[float | None]
) -> list[tuple[date, date] | None]:
    """(start, target) per entry, or None where it is not schedulable.

    `None` in `durations` stops the chain from that point onward.
    """
    scheduled: list[tuple[date, date] | None] = []
    cursor = anchor
    for weeks in durations:
        if weeks is None:
            # Nothing can hand off from here on: every later slot stays unset.
            scheduled.extend([None] * (len(durations) - len(scheduled)))
            return scheduled
        start = cursor
        end = start + timedelta(days=span_days(weeks))
        scheduled.append((start, end))
        cursor = end + timedelta(days=1)
    return scheduled


def last_target(scheduled: list[tuple[date, date] | None]) -> date | None:
    """Target of the last scheduled slot, which becomes the parent's target."""
    for slot in reversed(scheduled):
        if slot is not None:
            return slot[1]
    return None


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def write_dates(
    issue: Issue,
    want_start: date | None,
    want_target: date | None,
    ids: dict[str, str],
    names: dict[str, str],
    allow_start_write: bool,
    dry_run: bool,
) -> list[str]:
    """Bulk-write an issue's dates, skipping fields that already match.

    `allow_start_write` is False for a parent that already has a start date: the
    computed anchor is reported but never written over the stored value.
    Returns one human-readable note per field.
    """
    fields: list[dict[str, str]] = []
    notes: list[str] = []

    for role, want, stored in (
        ("start", want_start, issue.stored_start),
        ("target", want_target, issue.stored_target),
    ):
        name = names[role]
        if want is None:
            notes.append(f"{name}: left as {issue.show(stored)}")
            continue
        if stored == want:
            notes.append(f"{name}: unchanged ({want.isoformat()})")
            continue
        if role == "start" and not allow_start_write:
            notes.append(
                f"{name}: kept at {issue.show(stored)}, computed {want.isoformat()} "
                "but a parent start date is never overwritten"
            )
            continue
        fields.append({"fieldId": ids[role], "dateValue": want.isoformat()})
        notes.append(f"{name}: {issue.show(stored)} -> {want.isoformat()}")

    if not fields:
        return notes
    if dry_run:
        return [f"[dry-run] would write {len(fields)} field(s): "
                + ", ".join(f["dateValue"] for f in fields)] + notes
    gh_graphql(SET_VALUES_MUTATION, {"id": issue.node_id, "fields": fields})
    return notes


def report(issue: Issue, notes: list[str]) -> None:
    print(f"{issue.label}: " + "; ".join(notes))


def warn_invalid_durations(subs: list[Issue]) -> None:
    for sub in subs:
        if sub.duration_error:
            print(f"::warning::sub issue {sub.label} has an unusable Duration: "
                  f"{sub.duration_error}")


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #


def run_full(
    owner: str,
    repo: str,
    parent: Issue,
    ids: dict[str, str],
    names: dict[str, str],
    dry_run: bool,
) -> int:
    subs = fetch_chain(owner, repo, parent.number, names)
    if not subs:
        print(f"parent {parent.label} has no sub issues; nothing to schedule")
        return 0

    anchor = parent.stored_start or parent.created
    print(f"anchor {anchor.isoformat()} from parent "
          f"{'Start date' if parent.stored_start else 'createdAt'}")

    scheduled = compute_cascade(anchor, [sub.duration for sub in subs])
    target = last_target(scheduled)
    warn_invalid_durations(subs)

    print(f"plan (full recompute, {len(subs)} sub issues)")
    print(f"  {parent.label}  start {parent.show(parent.stored_start or anchor):<10} "
          f"target {parent.show(target):<10} parent")
    for sub, slot in zip(subs, scheduled):
        if slot is None:
            why = sub.duration_error or "duration not set"
            print(f"  {sub.label}  start {sub.show(sub.stored_start):<10} "
                  f"target {sub.show(sub.stored_target):<10} skipped: {why}")
        else:
            print(f"  {sub.label}  start {slot[0].isoformat():<10} "
                  f"target {slot[1].isoformat():<10} {sub.duration:g}w")

    report(parent, write_dates(
        parent, anchor, target, ids, names,
        allow_start_write=parent.stored_start is None, dry_run=dry_run,
    ))
    for sub, slot in zip(subs, scheduled):
        if slot is None:
            continue
        report(sub, write_dates(
            sub, slot[0], slot[1], ids, names, allow_start_write=True, dry_run=dry_run,
        ))
    return 1 if any(sub.duration_error for sub in subs) else 0


def run_cascade(
    owner: str,
    repo: str,
    edited: Issue,
    parent: Issue,
    ids: dict[str, str],
    names: dict[str, str],
    dry_run: bool,
) -> int:
    subs = fetch_chain(owner, repo, parent.number, names)
    index = next((i for i, sub in enumerate(subs) if sub.number == edited.number), None)
    if index is None:
        print(f"{edited.label} is not a sub issue of {parent.label}; nothing to do")
        return 0

    earlier = subs[:index]
    if edited.stored_start is None:
        print(f"{edited.label} has no stored start date to anchor on; "
              "falling back to a full recompute")
        return run_full(owner, repo, parent, ids, names, dry_run)
    if any(sub.stored_target is None for sub in earlier):
        print("an earlier sub issue has no target date to hand off from; "
              "falling back to a full recompute")
        return run_full(owner, repo, parent, ids, names, dry_run)

    scheduled = compute_cascade(edited.stored_start, [sub.duration for sub in subs[index:]])
    target = last_target(scheduled)
    warn_invalid_durations(subs[index:])

    print(f"plan (cascade from {edited.label}, anchor "
          f"{edited.stored_start.isoformat()}, {len(earlier)} left untouched)")
    for sub in earlier:
        print(f"  {sub.label}  start {sub.show(sub.stored_start):<10} "
              f"target {sub.show(sub.stored_target):<10} untouched")
    for offset, slot in enumerate(scheduled):
        sub = subs[index + offset]
        if slot is None:
            why = sub.duration_error or "duration not set"
            print(f"  {sub.label}  start {sub.show(sub.stored_start):<10} "
                  f"target {sub.show(sub.stored_target):<10} skipped: {why}")
        else:
            print(f"  {sub.label}  start {slot[0].isoformat():<10} "
                  f"target {slot[1].isoformat():<10} {sub.duration:g}w")
    # When nothing after the gap is schedulable there is no target to write, so
    # show what the parent keeps instead of a blank.
    parent_note = ("parent target only" if target is not None
                   else "no parent target computable, left as stored")
    print(f"  {parent.label}  start {parent.show(parent.stored_start):<10} "
          f"target {parent.show(target or parent.stored_target):<10} {parent_note}")

    for offset, slot in enumerate(scheduled):
        if slot is None:
            continue
        sub = subs[index + offset]
        report(sub, write_dates(
            sub, slot[0], slot[1], ids, names, allow_start_write=True, dry_run=dry_run,
        ))
    if target is not None:
        report(parent, write_dates(
            parent, None, target, ids, names,
            allow_start_write=False, dry_run=dry_run,
        ))
    return 1 if any(sub.duration_error for sub in subs[index:]) else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"missing required environment variable {name}")
    return value


def resolve_fields(owner: str, repo: str, names: dict[str, str]) -> dict[str, str]:
    """Map the three roles to field node ids, failing loudly on a bad config.

    Keyed by *role*, never by name: two roles pointed at one name would
    otherwise overwrite each other's entry and quietly skip a check.
    """
    data = gh_graphql(FIELDS_QUERY, {"owner": owner, "name": repo})
    nodes = data["repository"]["issueFields"]["nodes"]
    by_name = {node["name"]: node for node in nodes if node.get("name")}
    expected = {"start": "DATE", "target": "DATE", "duration": "NUMBER"}

    missing = sorted({names[role] for role in expected if names[role] not in by_name})
    if missing:
        available = ", ".join(sorted(by_name)) or "none visible"
        sys.exit(
            f"issue field(s) not found: {', '.join(missing)}. "
            f"Visible fields: {available}. "
            "Fix the workflow's FIELD_* variables or recreate the field."
        )

    ids: dict[str, str] = {}
    for role, want_type in expected.items():
        name = names[role]
        got_type = by_name[name].get("dataType")
        if got_type != want_type:
            sys.exit(
                f"issue field {name!r} is {got_type}, but the {role} role needs "
                f"{want_type}; scheduling would silently fail to read or write it"
            )
        ids[role] = by_name[name]["id"]
    return ids


def main() -> int:
    owner, _, repo = require("REPO").partition("/")
    if not repo:
        sys.exit("REPO must be 'owner/name'")
    names = {
        "start": os.environ.get("FIELD_START", "Start date").strip() or "Start date",
        "target": os.environ.get("FIELD_TARGET", "Target date").strip() or "Target date",
        "duration": os.environ.get("FIELD_DURATION", "Duration").strip() or "Duration",
    }
    if len(set(names.values())) != len(names):
        sys.exit(
            "the three FIELD_* names must be three different fields, got "
            + ", ".join(f"{role}={name!r}" for role, name in names.items())
        )
    number = require("ISSUE_NUMBER")
    if not number.isdigit():
        sys.exit(f"ISSUE_NUMBER must be an issue number, got {number!r}")
    number = int(number)
    # event_name is "issues" for every issue event; the event that matters
    # (opened vs field_added vs field_removed) is the action.
    event = os.environ.get("TRIGGER_EVENT", "").strip()
    action = os.environ.get("TRIGGER_ACTION", "").strip()
    changed = os.environ.get("CHANGED_FIELD", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    print(f"repo={owner}/{repo} issue=#{number} event={event or '-'}"
          f"{':' + action if action else ''} changed_field={changed or '-'} "
          f"dry_run={dry_run}")

    ids = resolve_fields(owner, repo, names)
    for role in ("start", "target", "duration"):
        print(f"  field {role:<8} {names[role]!r} -> {ids[role]}")

    trigger = fetch_issue(owner, repo, number, names)

    # What the schedule actually reads: a sub task's Duration, and the parent's
    # Start date, which is the chain anchor. A field event for anything else must
    # not reschedule, or the run would overwrite the value just edited.
    field_event = action in ("field_added", "field_removed")
    top_level = trigger.parent is None
    if not field_event:
        depends = True  # opened, or a manual dispatch
    elif top_level:
        depends = changed == names["start"]
    else:
        depends = changed == names["duration"]
    if not depends:
        print(f"{changed or 'an unknown field'} changed on {trigger.label}: the "
              f"schedule reads {names['duration']!r} on a sub issue and "
              f"{names['start']!r} on the parent only")
        return 0

    # A Duration change on a sub issue is the one case that can be answered
    # locally: re-chain from that issue and leave everything upstream alone.
    if field_event and not top_level:
        parent, mode = topological_parent(owner, repo, trigger, names), "cascade"
    else:
        parent, mode = (trigger if top_level
                        else topological_parent(owner, repo, trigger, names)), "full"

    print(f"mode={mode} parent={parent.label}")
    if mode == "full":
        return run_full(owner, repo, parent, ids, names, dry_run)
    return run_cascade(owner, repo, trigger, parent, ids, names, dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as error:
        sys.exit(f"graphql call failed: {error}")
