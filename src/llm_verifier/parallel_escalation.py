"""Concurrent independent children and drained, selectively resumable checkpoints."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy

from . import escalation as legacy, scheduler, ready_scheduler
from .artifacts import verify_envelope
from .diagnostics import ReviewDiagnostics
from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, digest, keys, number
from .judges import ESCALATION

VERSION = "parallel-children-v1"
NAMES = ("primary", "escalation")


def _charges(round_value):
    entries, reused = round_value["entries"], round_value["reused"]
    values = []
    for name in NAMES:
        entry = entries[name]
        if entry is None or name in reused or entry["carried"]:
            continue
        values.append(entry["review"]["provenance"] if entry["status"] == "completed" else entry["failure"]["state"]["provenance"])
    result = legacy.add(*values)
    result["elapsed_seconds"] = round_value["elapsed_seconds"]
    return result


def _history_spent(history):
    return legacy.add(*(_charges(r) for r in history))


def _reusable(history):
    return {name: entry for r in history for name, entry in r["entries"].items()
            if entry is not None and entry["status"] == "completed" and not entry["carried"]}


def _phase_limit(config, prior, history):
    return max(0, scheduler.limit(config, prior) - _history_spent(history)["elapsed_seconds"])


def _failed(entries):
    return any(e is not None and not e["carried"] and e["status"] == "failed" for e in entries.values())


def _verify_round(value, context, intake, initial, first, followup, prior, history, simulation):
    profile, config, manifest, refs, count, final, material = context
    keys(value, {"entries", "reused", "wall_elapsed_seconds", "elapsed_seconds"})
    keys(value["entries"], set(NAMES))
    reused = value["reused"]
    if type(reused) is not list or reused != [n for n in NAMES if n in reused]:
        raise VerifierError("escalation_reused_binding")
    saved = _reusable(history)
    phase_limit = _phase_limit(config, prior, history)
    charged_before = legacy.add(prior, _history_spent(history))
    lower = 0
    selected = legacy.selection(profile, intake)["selected"]
    for name in NAMES:
        entry = value["entries"][name]
        carry = legacy._carry(first, name)
        if name == "escalation" and not selected:
            if entry is not None or name in reused:
                raise VerifierError("unselected_escalation_review")
            continue
        if carry is not None:
            if canonical(carry) != canonical(entry) or name in reused:
                raise VerifierError("escalation_carried_review_binding")
            continue
        if name in saved:
            if name not in reused or canonical(saved[name]) != canonical(entry):
                raise VerifierError("escalation_reused_binding")
            continue
        if name in reused or type(entry) is not dict:
            raise VerifierError("escalation_reused_binding")
        complete = entry.get("status") == "completed"
        keys(entry, {"status", "carried", "review" if complete else "failure"})
        if entry["carried"] is not False or entry["status"] not in ("completed", "failed"):
            raise VerifierError("escalation_review_status")
        child = entry["review"] if complete else entry["failure"]["state"]
        if not complete:
            failure = entry["failure"]
            keys(failure, {"state", "calls", "truncated", "overhead_seconds"})
            if (type(failure["calls"]) is not list or type(failure["truncated"]) is not bool or failure["overhead_seconds"] != 0
                    or canonical(failure["calls"]) != canonical(child.get("calls"))):
                raise VerifierError("escalation_failure_evidence")
        scheduler.verify(child, profile[name], config, manifest, refs, count, final, material(profile[name]),
            charged_before, simulation, files=intake["data"]["files"], plan=legacy._plan(first, name),
            validator=lambda v: legacy._check_decision(v, intake, initial, followup), failed=not complete,
            phase_limit=phase_limit, execution_version=ready_scheduler.VERSION)
        duration = child["provenance"]["elapsed_seconds"] if complete else child["wall_elapsed_seconds"]
        lower = max(lower, child["execution"]["offset_seconds"] + duration)
    number(value["wall_elapsed_seconds"], lower, 10**9)
    number(value["elapsed_seconds"], 0, phase_limit)
    if value["elapsed_seconds"] != min(value["wall_elapsed_seconds"], phase_limit):
        raise VerifierError("escalation_round_time_binding")
    spent = legacy.add(charged_before, _charges(value))
    for field, name in (("attempts", "total_provider_requests"), ("allotted_completion_tokens", "total_review_tokens")):
        if spent[field] > config["limits"][name]:
            raise VerifierError("escalation_aggregate_budget")
    # An undispatched exhausted call must be explained by the GLOBAL ledger or
    # deadline, not an invented per-child budget. This also handles peers whose
    # retries consumed the last grant later in logical report order.
    for name in NAMES:
        entry = value["entries"][name]
        if entry is None or entry["carried"] or name in reused:
            continue
        child = entry["review"] if entry["status"] == "completed" else entry["failure"]["state"]
        for wave in child["execution"].get("waves", child["execution"].get("groups", [])):
            for call in wave["calls"]:
                if call["error"] == scheduler.EXHAUSTED:
                    member = next(m for m in profile[name]["members"] if m["id"] == call["member_id"])
                    if (spent["attempts"] < config["limits"]["total_provider_requests"] and
                            spent["allotted_completion_tokens"] + member["provider"]["max_completion_tokens"] <= config["limits"]["total_review_tokens"] and
                            value["wall_elapsed_seconds"] < phase_limit):
                        raise VerifierError("escalation_exhaustion_binding")
    ready_scheduler.verify_active_calls([e["review"] if e["status"] == "completed" else e["failure"]["state"]
        for n, e in value["entries"].items() if e is not None and not e["carried"] and n not in reused], config)
    return value


def _validate_history(history, context, intake, initial, first, followup, prior, simulation):
    if type(history) is not list or len(history) > 128:
        raise VerifierError("escalation_recovery_count")
    for index, item in enumerate(history):
        _verify_round(item, context, intake, initial, first, followup, prior, history[:index], simulation)
        if not _failed(item["entries"]):
            raise VerifierError("escalation_recovery_completed")


def _resume(value, context, intake, initial, first, followup, prior, simulation):
    parents = [intake, initial] + ([first, followup] if first else [])
    data = verify_envelope(value, "review_failure", intake, parents)
    keys(data, {"phase", "error", "calls", "truncated", "publishable", "escalation_checkpoint"})
    if data["phase"] != ("final" if first else "initial") or data["publishable"] is not False:
        raise VerifierError("escalation_resume_phase")
    if value["simulation"] and not simulation:
        raise VerifierError("simulation_downgrade")
    checkpoint = data["escalation_checkpoint"]
    if checkpoint.get("version") != VERSION or checkpoint.get("status") != "settled":
        raise VerifierError("escalation_checkpoint_in_progress")
    keys(checkpoint, {"version", "status", "selection", "rounds"})
    if canonical(checkpoint["selection"]) != canonical(legacy.selection(context[0], intake)):
        raise VerifierError("escalation_selection_binding")
    history = checkpoint["rounds"]
    if not history:
        raise VerifierError("escalation_recovery_count")
    _validate_history(history, context, intake, initial, first, followup, prior, simulation)
    return history


def run(intake, initial, first=None, followup=None, *, providers=None, capture=None, failure_out=None, resume=None):
    context = legacy._context(intake, initial, first, followup)
    profile, config, manifest, refs, count, final, material = context
    selected = legacy.selection(profile, intake)
    prior = first["data"]["provenance"] if final else legacy.ZERO
    parents = [intake, initial] + ([first, followup] if final else [])
    simulation = providers is not None or any(p["simulation"] for p in parents) or bool(resume and resume["simulation"])
    capture = capture or ReviewDiagnostics(intake, parents, "final" if final else "initial", simulation)
    if providers is not None and (type(providers) is not dict or providers.keys() != set(NAMES)):
        raise VerifierError("escalation_provider_children")
    history = _resume(resume, context, intake, initial, first, followup, prior, simulation) if resume else []
    reusable = _reusable(history)
    entries, active, reused = {}, [], []
    for name in NAMES:
        carry = legacy._carry(first, name)
        if name == "escalation" and not selected["selected"]:
            entries[name] = None
        elif carry is not None:
            entries[name] = carry
        elif name in reusable:
            entries[name] = reusable[name]
            reused.append(name)
        else:
            active.append(name)
    # Validate injected registries before creating any model work.
    if providers is not None:
        for name in NAMES:
            if type(providers[name]) is not dict or providers[name].keys() != {m["id"] for m in profile[name]["members"]}:
                raise VerifierError("committee_provider_members")
    captures = {n: ReviewDiagnostics(intake, parents, capture.phase, simulation, budget=capture) for n in active}
    used = legacy.add(prior, _history_spent(history))
    with ready_scheduler.Coordinator(config, used, seconds=_phase_limit(config, prior, history)) as coordinator:
        def child(name):
            try:
                review, _ = scheduler.run(profile[name], config, manifest, refs, count, final, material(profile[name]), used,
                    providers=providers[name] if providers is not None else None, files=intake["data"]["files"],
                    plan=legacy._plan(first, name), capture=captures[name], coordinator=coordinator,
                    validator=lambda v: legacy._check_decision(v, intake, initial, followup))
                return {"status": "completed", "carried": False, "review": review}
            except InfrastructureError as exc:
                if not hasattr(exc, "review_state"):
                    raise
                return {"status": "failed", "carried": False, "failure": {"state": exc.review_state,
                    "calls": captures[name].calls, "truncated": captures[name].truncated, "overhead_seconds": 0}}

        def snapshot(*, persist=True):
            with capture._lock:
                capture.calls = copy.deepcopy([call for n in NAMES if n in captures for call in captures[n].calls])
                capture.checkpoint = {"version": VERSION, "status": "in-progress", "selection": selected,
                    "completed": copy.deepcopy(entries), "running": [n for n in active if n not in entries],
                    "reserved_attempts": coordinator.attempts, "reserved_completion_tokens": coordinator.tokens,
                    "phase_time_limit_seconds": coordinator.limit,
                    "elapsed_seconds": min(coordinator.elapsed(), coordinator.limit), "recovery": history}
            if persist and failure_out is not None:
                capture.write(failure_out, InfrastructureError("escalation_in_progress"))

        # A trusted injected object can be stateful across both child registries.
        # Serialize those children in logical order; distinct objects still overlap.
        alias = providers is not None and bool({id(p) for p in providers["primary"].values()} &
                                               {id(p) for p in providers["escalation"].values()})
        with ThreadPoolExecutor(max_workers=1 if alias else 2, thread_name_prefix="judge-child") as children:
            futures = {children.submit(child, n): n for n in active}
            first_error = None
            for future in as_completed(futures):
                try:
                    entries[futures[future]] = future.result()
                except Exception as exc:
                    first_error = first_error or exc
                # Persistence failure cannot abandon collection of already
                # admitted peers. Keep refreshing diagnostic-only memory, then
                # propagate the original error after every future is drained.
                try:
                    snapshot(persist=first_error is None)
                except Exception as exc:
                    first_error = first_error or exc
            if first_error is not None:
                try:
                    first_error.escalation_checkpoint = capture.envelope(first_error)
                except Exception:
                    # Evidence serialization must not replace the first error.
                    pass
                raise first_error
        wall = coordinator.elapsed()
        round_value = {"entries": {n: entries[n] for n in NAMES}, "reused": reused,
                       "wall_elapsed_seconds": wall, "elapsed_seconds": min(wall, coordinator.limit)}
    has_failure = _failed(entries)

    def finalize_failure(error):
        wall = coordinator.elapsed()
        round_value.update(wall_elapsed_seconds=wall, elapsed_seconds=min(wall, coordinator.limit))
        capture.checkpoint = {"version": VERSION, "status": "settled", "selection": selected, "rounds": history + [round_value]}
        error.escalation_checkpoint = capture.envelope(error)
        if failure_out is not None:
            capture.write(failure_out, error)

    if has_failure:
        failed_primary = entries["primary"]["status"] == "failed"
        failed_entry = entries["primary" if failed_primary else "escalation"]
        error = InfrastructureError(failed_entry["failure"]["state"]["error"])
        if failed_primary or profile["mode"] == "override":
            finalize_failure(error)
            raise error
    try:
        composed = legacy._result(profile, selected, entries["primary"], entries["escalation"], manifest, config, count)
    except Exception as exc:
        if has_failure:
            finalize_failure(exc)
        else:
            snapshot()
        raise
    wall = coordinator.elapsed()
    round_value.update(wall_elapsed_seconds=wall, elapsed_seconds=min(wall, coordinator.limit))
    if has_failure:
        # The saved checkpoint and returned result use exactly the same final
        # accounting, including composition after the model calls have drained.
        finalize_failure(error)
    elif wall > coordinator.limit:
        snapshot()
        raise InfrastructureError(scheduler.EXHAUSTED)
    total = legacy.add(_history_spent(history), _charges(round_value))
    return {"selection": selected, **entries, "recovery": history,
        "recovery_elapsed_seconds": _history_spent(history)["elapsed_seconds"],
        "execution": {"version": VERSION, **{k: v for k, v in round_value.items() if k != "entries"}},
        **composed,
        "provenance": {"protocol": ESCALATION, "committee_sha256": digest(profile), **total}}, simulation


def verify(value, intake, initial, first, followup, simulation):
    context = legacy._context(intake, initial, first, followup)
    profile, config, manifest, _, count, _, _ = context
    keys(value, {"selection", "primary", "escalation", "recovery", "recovery_elapsed_seconds", "execution",
                 "decision", "authority", "comparison", "provenance"})
    selected = legacy.selection(profile, intake)
    if canonical(selected) != canonical(value["selection"]):
        raise VerifierError("escalation_selection_binding")
    execution = value["execution"]
    keys(execution, {"version", "reused", "wall_elapsed_seconds", "elapsed_seconds"})
    if execution["version"] != VERSION:
        raise VerifierError("escalation_execution_version")
    prior = first["data"]["provenance"] if first else legacy.ZERO
    history = value["recovery"]
    _validate_history(history, context, intake, initial, first, followup, prior, simulation)
    current = {"entries": {n: value[n] for n in NAMES}, **{k: v for k, v in execution.items() if k != "version"}}
    _verify_round(current, context, intake, initial, first, followup, prior, history, simulation)
    if value["primary"]["status"] != "completed":
        raise VerifierError("escalation_primary_failed")
    expected = legacy._result(profile, selected, value["primary"], value["escalation"], manifest, config, count)
    if any(canonical(value[k]) != canonical(v) for k, v in expected.items()):
        raise VerifierError("escalation_decision_binding")
    recovered = _history_spent(history)
    if value["recovery_elapsed_seconds"] != recovered["elapsed_seconds"]:
        raise VerifierError("escalation_recovery_time")
    total = legacy.add(recovered, _charges(current))
    expected_provenance = {"protocol": ESCALATION, "committee_sha256": digest(profile), **total}
    if canonical(value["provenance"]) != canonical(expected_provenance):
        raise VerifierError("escalation_budget_binding")
    return value
