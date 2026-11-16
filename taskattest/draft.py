# Copyright 2026 Sion Jang
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Draft a spec by reading the files, so nobody has to open a UTF-16 log to write one.

Deterministic: no model is consulted, and the same files give the same draft.

The danger this module is built around: an agent that drafts its spec from its
own wrong output will pass. So a draft never fits a threshold to an output. It
suggests structure (the file exists, these columns, not empty, ids unique) and
relations to the inputs (every output id exists in the input), seals the inputs
by hash, and reports what it observed as notes rather than as limits. The spec
is marked "draft": true, and every verdict made with it says so, until a person
removes that flag.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import runner
from . import trace as tr
from . import xlsx

RECORD_SUFFIXES = (".csv", ".tsv", ".jsonl") + xlsx.SUFFIXES
TEXT_SUFFIXES = (".md", ".txt", ".rst")
# An instance id names one thing, so it should be unique. A reference code names a
# kind of thing (a product, a barcode), so it repeats by design; it is only used to
# relate an output to an input. The same SKU ordered twice is not a duplicate, and
# proposing codes as unique produces false failures.
INSTANCE_ID = re.compile(r"(^id$|_id$|^uuid$|_uuid$|^key$|_key$|^no$|_no$|번호$|아이디$)",
                         re.IGNORECASE)
REFERENCE_ID = re.compile(r"(^code$|_code$|^sku$|_sku$|barcode|코드$)", re.IGNORECASE)
AMOUNT_NAME = re.compile(r"(amount|price|total|qty|quantity|cost|fee|금액|가격|수량|합계)",
                         re.IGNORECASE)
GROUP_NAME = re.compile(r"(cycle|episode|round|iteration|trial|delivery|attempt|run_?id)",
                        re.IGNORECASE)
STATE_NAME = re.compile(r"(phase|state|status|event|stage|step|mode|action)", re.IGNORECASE)
TRAILING_DIGIT = re.compile(r"\d$")
MAX_STEPS = 8
MAX_SAMPLE = 12


# ---------- profiling ----------

def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _flatten(record, prefix="", depth=0, out=None):
    out = {} if out is None else out
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 2:
            _flatten(value, name + ".", depth + 1, out)
        elif not isinstance(value, (list, dict)):
            out[name] = value
    return out


def profile_fields(records: list) -> dict:
    """Per field: how often present, numeric or not, range, and a sample of values."""
    columns: dict = {}
    order: list = []
    for record in records:
        for name, value in _flatten(record).items():
            if name not in columns:
                columns[name] = []
                order.append(name)
            columns[name].append(value)
    out = {}
    for name in order:
        values = [v for v in columns[name] if v is not None and str(v).strip() != ""]
        numbers = [_number(v) for v in values]
        numeric = bool(values) and all(n is not None for n in numbers)
        distinct = list(dict.fromkeys(str(v) for v in values))
        info = {"present": len(values), "numeric": numeric, "distinct": len(distinct)}
        if numeric:
            info["min"] = str(min(numbers))
            info["max"] = str(max(numbers))
            info["integer"] = all(n == n.to_integral_value() for n in numbers)
        elif len(distinct) <= 30:
            info["values"] = distinct[:MAX_SAMPLE] + (["..."] if len(distinct) > MAX_SAMPLE else [])
        out[name] = info
    return out


def detect_line_prefix(path: Path):
    """The text before the JSON object on most lines, if those lines parse as JSON."""
    try:
        lines = tr._decode(Path(path)).splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    candidates = Counter()
    for line in lines:
        brace = line.find("{")
        if brace > 0:
            candidates[line[:brace]] += 1
    if not candidates:
        return None
    prefix, count = candidates.most_common(1)[0]
    if count < max(2, len(lines) // 4):
        return None
    parsed = 0
    for line in lines:
        if line.startswith(prefix):
            try:
                if isinstance(json.loads(line[len(prefix):]), dict):
                    parsed += 1
            except json.JSONDecodeError:
                pass
    return prefix if parsed >= 0.9 * count else None


def classify(path: Path):
    """("records", rows, header, records_key) / ("document", data) / ("text", text) / (kind, None)."""
    suffix = path.suffix.lower()
    if not path.is_file():
        return ("missing", None)
    if suffix in RECORD_SUFFIXES:
        rows = runner.load_records(path)
        return ("records", rows, runner.columns_of(path, rows), None)
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
            return ("records", data, runner.columns_of(path, data), None)
        if isinstance(data, dict):
            return ("document", data)
        return ("json_other", data)
    if suffix in TEXT_SUFFIXES:
        return ("text", path.read_text(encoding="utf-8-sig", errors="replace"))
    return ("other", None)


# ---------- suggestions ----------

def _rel(path: Path, spec_dir: Path) -> str:
    try:
        return Path(os.path.relpath(path, spec_dir)).as_posix()
    except ValueError:  # another drive on Windows
        return path.as_posix()


def _instance_ids(header) -> list:
    return [c for c in header if INSTANCE_ID.search(str(c))]


def _reference_ids(header) -> list:
    return [c for c in header if INSTANCE_ID.search(str(c)) or REFERENCE_ID.search(str(c))]


def _record_checks(name: str, ref: str, rows: list, header: list, records_key,
                   input_record_sets: dict) -> tuple:
    checks, notes = [], []
    extra = {"records_key": records_key} if records_key else {}
    if header:
        checks.append({"name": f"{name}: required columns", "type": "csv_columns",
                       "target": ref, "columns": list(header), **extra,
                       "why": "the columns the file has now; delete any that are optional"})
    checks.append({"name": f"{name}: not empty", "type": "row_count", "target": ref, **extra,
                   "why": "an empty result is the most common silent failure"})
    for column in _instance_ids(header):
        checks.append({"name": f"{name}: {column} unique", "type": "unique_key", "target": ref,
                       "key": column, **extra,
                       "why": f"'{column}' looks like an instance id; proposed whether or not it is "
                              f"unique today, so a duplicate shows up as a failure"})
    for column in _reference_ids(header):
        for input_name, (input_ref, input_header, input_key, _) in input_record_sets.items():
            if column in input_header:
                checks.append({
                    "name": f"{name}: every {column} comes from {input_name}", "type": "subset_of",
                    "target": ref, "key": column, "source": input_ref, "source_key": column,
                    **extra, **({"source_records_key": input_key} if input_key else {}),
                    "why": "catches a plausible record that was never in the input"})
    profile = profile_fields(rows)
    for column in header:
        if not AMOUNT_NAME.search(str(column)):
            continue
        # Decided from the column name and the INPUTS, never from this output's values:
        # a wrong output must not be able to switch off the check that would catch it.
        negative_inputs = [n for n, (_, _, _, prof) in input_record_sets.items()
                           if prof.get(column, {}).get("numeric")
                           and Decimal(prof[column]["min"]) < 0]
        if negative_inputs:
            notes.append(f"{name}: {column} not bounded at 0, because input "
                         f"{negative_inputs[0]} already has negative {column} values")
            continue
        checks.append({"name": f"{name}: {column} not negative", "type": "numeric_range",
                       "target": ref, "column": column, "min": 0, **extra,
                       "why": "an amount-like column, and no input has negative values in it; "
                              "the upper bound is yours to set"})
    notes.append(f"{name}: {len(rows)} record(s), columns {list(header)}")
    notes.append(f"{name}: the column and key checks mirror this file as it is. If it is the "
                 f"agent's own output they cannot catch a column it left out; draft from a "
                 f"known-good example, or before the run. The seals and the checks against the "
                 f"inputs are what carry correctness")
    return checks, notes, profile


def _document_checks(name: str, ref: str, data: dict) -> tuple:
    checks = [{"name": f"{name}: parses as an object", "type": "json_valid", "target": ref,
               "top_level": "object", "why": "catches prose, a markdown fence or a truncated write"}]
    keys = list(data.keys())
    if keys:
        checks.append({"name": f"{name}: top-level keys", "type": "json_keys", "target": ref,
                       "required": keys, "why": "the keys present now; delete any that are optional"})
    nested = [k for k, v in data.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    return checks, nested


def _text_checks(name: str, ref: str, text: str) -> list:
    checks = [{"name": f"{name}: nothing left unwritten", "type": "no_placeholders", "target": ref,
               "why": "TODO, lorem ipsum, template braces or a refusal left in a deliverable"}]
    headings = [line.strip() for line in text.splitlines() if line.lstrip().startswith("#")]
    if headings:
        checks.append({"name": f"{name}: sections", "type": "text_contains", "target": ref,
                       "contains": headings[:20],
                       "why": "the headings present now; keep the ones that are required"})
    return checks


def _trace_analysis(records: list) -> dict:
    profile = profile_fields(records)
    group = None
    candidates = []
    for field, info in profile.items():
        if info.get("numeric") and info.get("integer") and 2 <= info["distinct"] <= max(2, len(records) // 2):
            values = [_number(v) for v in (tr.lookup(r, field) for r in records) if _number(v) is not None]
            non_decreasing = all(a <= b for a, b in zip(values, values[1:]))
            if non_decreasing:
                candidates.append((0 if GROUP_NAME.search(field) else 1, field))
    if candidates:
        group = sorted(candidates)[0][1]

    state = None
    states = [(0 if STATE_NAME.search(f) else 1, -i["distinct"], f) for f, i in profile.items()
              if not i.get("numeric") and 2 <= i["distinct"] <= 60 and i["present"] >= len(records) * 0.9]
    if states:
        state = sorted(states)[0][2]

    steps = []
    common_total = 0
    if group and state:
        groups: dict = {}
        for record in records:
            g = tr.lookup(record, group)
            s = tr.lookup(record, state)
            if g is tr._MISSING or s is tr._MISSING:
                continue
            seq = groups.setdefault(json.dumps(g), [])
            if not seq or seq[-1] != s:
                seq.append(s)
        n = len(groups)
        first_pos: dict = {}
        for seq in groups.values():
            seen = {}
            for i, s in enumerate(seq):
                seen.setdefault(s, i / max(1, len(seq) - 1))
            for s, pos in seen.items():
                first_pos.setdefault(s, []).append(pos)
        common = [s for s, pos in first_pos.items() if len(pos) >= 0.8 * n]
        common.sort(key=lambda s: (sum(first_pos[s]) / len(first_pos[s]), str(s)))
        common_total = len(common)
        milestones = [s for s in common if not TRAILING_DIGIT.search(str(s))] or common
        if len(milestones) > MAX_STEPS:
            last = len(milestones) - 1
            picks = sorted({round(i * last / (MAX_STEPS - 1)) for i in range(MAX_STEPS)})
            milestones = [milestones[i] for i in picks]
        steps = milestones
        groups_count = n
    else:
        groups_count = 0
    return {"profile": profile, "group": group, "state": state, "steps": steps,
            "groups": groups_count, "common_states": common_total}


def draft_spec(spec_dir, inputs=None, outputs=None, traces=None, line_prefix=None,
               task_id="draft_task", description="") -> dict:
    """Read the files and return {"spec": ..., "notes": [...], "profiles": {...}}."""
    spec_dir = Path(spec_dir).resolve()
    inputs = {k: Path(v).resolve() for k, v in (inputs or {}).items()}
    outputs = {k: Path(v).resolve() for k, v in (outputs or {}).items()}
    traces = [Path(t).resolve() for t in (traces or [])]
    notes, checks, profiles = [], [], {}
    spec_inputs, spec_outputs = {}, {}

    record_inputs: dict = {}
    for name, path in inputs.items():
        spec_inputs[name] = _rel(path, spec_dir)
        ref = f"inputs.{name}"
        if not path.is_file():
            notes.append(f"input {name}: {path.name} does not exist; nothing was proposed for it")
            continue
        checks.append({"name": f"{name}: not tampered with", "type": "file_hash", "target": ref,
                       "sha256": runner.sha256_file(path),
                       "why": "sealed as found. Draft before the agent runs to seal what it was "
                              "given; drafted afterwards, this certifies the state after the run"})
        try:
            kind = classify(path)
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            notes.append(f"input {name}: could not be read as data ({exc})")
            continue
        if kind[0] == "records":
            prof = profile_fields(kind[1])
            record_inputs[name] = (ref, kind[2], None, prof)
            profiles[f"inputs.{name}"] = prof
        elif kind[0] == "document":
            for key in [k for k, v in kind[1].items()
                        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]:
                rows = kind[1][key]
                record_inputs[f"{name}.{key}"] = (ref, runner.columns_of(path, rows), key,
                                                  profile_fields(rows))

    for name, path in outputs.items():
        spec_outputs[name] = _rel(path, spec_dir)
        ref = f"outputs.{name}"
        checks.append({"name": f"{name}: exists", "type": "file_exists", "target": ref,
                       "why": "the minimum: the agent produced it"})
        try:
            kind = classify(path)
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            notes.append(f"output {name}: exists but could not be read ({exc}); that is itself a finding")
            continue
        if kind[0] == "missing":
            notes.append(f"output {name}: not produced yet, so only its existence is checked. "
                         f"Describe its columns or keys, or draft again once it exists")
        elif kind[0] == "records":
            c, n, p = _record_checks(name, ref, kind[1], kind[2], None, record_inputs)
            checks += c
            notes += n
            profiles[ref] = p
        elif kind[0] == "document":
            c, nested = _document_checks(name, ref, kind[1])
            checks += c
            for key in nested:
                rows = kind[1][key]
                c, n, p = _record_checks(f"{name}.{key}", ref, rows, runner.columns_of(path, rows),
                                         key, record_inputs)
                checks += c
                notes += n
                profiles[f"{ref}#{key}"] = p
        elif kind[0] == "text":
            checks += _text_checks(name, ref, kind[1])
        else:
            notes.append(f"output {name}: not a format with content checks; existence only")

    if traces:
        refs = []
        prefix = line_prefix
        if prefix is None:
            detected = {detect_line_prefix(t) for t in traces if t.suffix.lower() not in (".jsonl", ".json", ".csv")}
            detected.discard(None)
            if len(detected) == 1:
                prefix = detected.pop()
                notes.append(f"traces: detected line_prefix {prefix!r}; only lines starting with it are records")
            elif len(detected) > 1:
                notes.append(f"traces: files disagree on a line prefix {sorted(detected)}; pass line_prefix")
        for i, path in enumerate(traces, start=1):
            key = f"trace{i}"
            spec_inputs[key] = _rel(path, spec_dir)
            refs.append(f"inputs.{key}")
        records = []
        for path in traces:
            try:
                records += tr.load_trace(path, prefix, None, runner.load_records)
            except (tr.TraceError, ValueError, json.JSONDecodeError, OSError) as exc:
                notes.append(f"trace {path.name}: could not be read ({exc})")
        base = {"target": refs if len(refs) > 1 else refs[0], **({"line_prefix": prefix} if prefix else {})}
        if not records:
            notes.append("traces: no records were read, so no trace checks were proposed")
        else:
            a = _trace_analysis(records)
            profiles["traces"] = a["profile"]
            notes.append(f"traces: {len(records)} record(s), fields {list(a['profile'])[:20]}")
            if a["state"]:
                info = a["profile"][a["state"]]
                shown = info.get("values") or f"{info['distinct']} distinct values, see profiles.traces"
                notes.append(f"traces: '{a['state']}' looks like the state field; {shown}")
            if a["group"]:
                notes.append(f"traces: '{a['group']}' looks like the grouping field; "
                             f"{a['groups']} group(s)")
            if a["group"] and a["state"] and len(a["steps"]) >= 2:
                checks.append({
                    "name": f"each {a['group']} runs its milestones in order", "type": "trace_order",
                    **base, "group_by": a["group"], "min_groups": a["groups"],
                    "steps": [{"field": a["state"], "eq": s} for s in a["steps"]],
                    "why": (f"{len(a['steps'])} milestone(s) chosen from {a['common_states']} state(s) "
                            f"that appear in at least 80% of {a['groups']} {a['group']} group(s). "
                            f"min_groups is set to every group observed: lower it to your threshold")})
                checks.append({
                    "name": f"{a['group']}s reaching {a['steps'][-1]}", "type": "trace_count", **base,
                    "where": {"field": a["state"], "eq": a["steps"][-1]}, "distinct": a["group"],
                    "min": a["groups"],
                    "why": "set min to how many must succeed; it is set to every group observed"})
            elif a["state"]:
                checks.append({"name": f"{a['state']} reaches its last common value", "type": "trace_eventually",
                               **base, "condition": {"field": a["state"],
                                                     "eq": a["profile"][a["state"]].get("values", [None])[0]},
                               "why": "no grouping field was found; edit the value to the one that means done"})
            notes.append("traces: no invariant was proposed, because an invariant fitted to this run "
                         "would only certify this run. Write the ones you require from the observed "
                         "ranges in profiles.traces")
            if outputs or [k for k in inputs]:
                notes.append("traces: pair them with a final-state check. A log records the action; "
                             "only the final state records the outcome")

    spec = {"task_id": task_id, "description": description or "Drafted by taskattest from the files.",
            "draft": True,
            "draft_notes": ["Review every check, then delete \"draft\": true. Until then each verdict "
                            "made with this spec carries a warning."] + notes,
            "inputs": spec_inputs, "outputs": spec_outputs}
    if spec_outputs:
        spec["core_output"] = next(iter(spec_outputs))
    spec["checks"] = checks
    return {"spec": spec, "notes": notes, "profiles": profiles}


def write_spec(spec: dict, path, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; refusing to overwrite a spec someone may have reviewed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="taskattest draft",
                                 description="Draft a spec by reading the files. Deterministic, no model calls.")
    ap.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--output", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--trace", action="append", default=[], metavar="PATH")
    ap.add_argument("--line-prefix", default=None)
    ap.add_argument("--task-id", default="draft_task")
    ap.add_argument("--description", default="")
    ap.add_argument("--out", required=True, help="where to write the draft spec")
    ap.add_argument("--force", action="store_true", help="overwrite an existing spec")
    a = ap.parse_args(argv)

    def pairs(items, flag):
        out = {}
        for item in items:
            if "=" not in item:
                ap.error(f"{flag} takes NAME=PATH, got {item!r}")
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
        return out

    out_path = Path(a.out).resolve()
    result = draft_spec(out_path.parent, pairs(a.input, "--input"), pairs(a.output, "--output"),
                        a.trace, a.line_prefix, a.task_id, a.description)
    try:
        write_spec(result["spec"], out_path, a.force)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 2
    print(f"drafted {len(result['spec']['checks'])} check(s) -> {out_path}")
    for note in result["notes"]:
        print(f"  note: {note}")
    print("  review it, then delete \"draft\": true")
    return 0
