"""Opt-in independent sampled reviews with one shared budget and replay path."""

from fractions import Fraction
import time

from . import committee
from .artifacts import verify_envelope
from .diagnostics import ReviewDiagnostics
from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, digest, keys, number
from .judges import ESCALATION
from .provider import MAX_REVIEW_SECONDS
from .submission import followup_plan

BUDGET_FIELDS = ("attempts", "allotted_completion_tokens", "elapsed_seconds")
ZERO = dict.fromkeys(BUDGET_FIELDS, 0)


def selection(profile, intake):
    """Public deterministic sampling, NOT protection against solver grinding."""
    inputs = {"version": "sha256-submission-config-v1", "seed": profile["seed"],
              **{k: intake["bindings"][k] for k in ("submission", "config", "verifier")}}
    hashed = digest(inputs)
    threshold = int(Fraction(str(profile["probability"])) * 2**256)
    return {"inputs": inputs, "sha256": hashed, "probability": profile["probability"],
            "threshold_hex": format(threshold, "065x"), "selected": int(hashed, 16) < threshold}


def add(*values):
    return {k: sum(v.get(k, 0) for v in values) for k in BUDGET_FIELDS}


def failure_spent(failure):
    return add(failure["state"]["provenance"], {"elapsed_seconds": failure["overhead_seconds"]})


def spent(entry):
    if entry is None or entry["carried"]:
        return ZERO
    return entry["review"]["provenance"] if entry["status"] == "completed" else failure_spent(entry["failure"])


def _context(intake, initial, first, followup):
    from .pipeline import _committee_material, evidence_refs
    data = intake["data"]
    profile = data["effective"]["judging"]["committee"]
    final = first is not None
    return (profile, data["effective"]["config"], data["manifest"],
            evidence_refs(intake, initial, followup),
            len(initial["data"]["runs"]) + (len(followup["data"]["runs"]) if final else 0),
            final, lambda child: _committee_material(intake, initial, first, followup,
                                                    committee=child, independent=True))


def _plan(first, name):
    if first is None:
        return None
    entry = first["data"][name]
    return entry["review"].get("region_plan") if entry and entry["status"] == "completed" else None


def _carry(first, name):
    if first is None:
        return None
    entry = first["data"][name]
    if entry is None:
        return None
    if entry["status"] == "failed" or entry["review"]["decision"]["kind"] == "judgment":
        return {**entry, "carried": True}
    return None


def _check_decision(value, intake, initial, followup):
    from .checks import enforce_score_gate, score_gate
    from .scoring import project
    from . import structured_claims
    if value["decision"]["kind"] == "judgment":
        judgment = value["decision"]["judgment"]
        structured_claims.verify_acknowledgment(judgment, intake["data"])
        gate = score_gate(intake["data"], initial, followup)
        projected, _ = project(judgment, gate, intake["data"]["effective"]["config"]["allow_unscored"])
        enforce_score_gate(projected, gate)


def _result(profile, chosen, primary, escalated, manifest, config, previous_runs):
    decisions = [primary["review"]["decision"]]
    if escalated is not None and escalated["status"] == "completed":
        decisions.append(escalated["review"]["decision"])
    requests, seen = [], set()
    for decision in decisions:
        for request in decision["requests"]:
            identity = digest({k: v for k, v in request.items() if k != "reason"})
            if identity not in seen:
                requests.append(request)
                seen.add(identity)
    authoritative = "escalation" if chosen["selected"] and profile["mode"] == "override" else "primary"
    if authoritative == "escalation" and (escalated is None or escalated["status"] != "completed"):
        raise InfrastructureError("authoritative_escalation_failed")
    reason = ("not_selected" if not chosen["selected"] else
              "selected_override" if authoritative == "escalation" else
              "audit_failed_primary_retained" if escalated["status"] == "failed" else "record_only")
    if requests:
        followup_plan(requests, manifest, config["limits"], previous_runs, config.get("checkers", []))
        decision = {"kind": "followup", "judgment": None, "requests": requests}
    else:
        decision = (escalated if authoritative == "escalation" else primary)["review"]["decision"]
    available = len(decisions) == 2 and all(d["kind"] == "judgment" for d in decisions)
    comparison = {"available": available, "valid_disagreement": None, "scores_disagreement": None}
    if available:
        a, b = [d["judgment"] for d in decisions]
        comparison.update(valid_disagreement=a["valid"] != b["valid"],
                          scores_disagreement=canonical(a["scores"]) != canonical(b["scores"]))
    return {"decision": decision, "authority": {"judge": authoritative, "reason": reason}, "comparison": comparison}


def _budget(profile, config, entries, recovery, prior, elapsed=None):
    total = add(*(spent(e) for e in entries), *(failure_spent(f) for f in recovery))
    if elapsed is not None:
        total["elapsed_seconds"] = elapsed
    limits = config["limits"]
    for key, limit in (("attempts", "total_provider_requests"), ("allotted_completion_tokens", "total_review_tokens")):
        if total[key] + prior.get(key, 0) > limits[limit]:
            raise VerifierError("escalation_aggregate_budget")
    number(total["elapsed_seconds"], 0, min(MAX_REVIEW_SECONDS, limits["total_review_seconds"] - prior.get("elapsed_seconds", 0)))
    return {"protocol": ESCALATION, "committee_sha256": digest(profile), **total}


def _verify_entry(entry, name, context, intake, initial, first, followup, prior, simulation):
    profile, config, manifest, refs, count, final, material = context
    carried = _carry(first, name)
    if carried is not None:
        if canonical(entry) != canonical(carried):
            raise VerifierError("escalation_carried_review_binding")
        return
    keys(entry, {"status", "carried", "review"})
    if entry["status"] != "completed" or entry["carried"] is not False:
        raise VerifierError("escalation_review_status")
    committee.verify(entry["review"], profile[name], config, manifest, refs, count, final,
                     material(profile[name]), prior, simulation, files=intake["data"]["files"], plan=_plan(first, name),
                     execution_version=committee.SERIAL_EXECUTION)
    _check_decision(entry["review"], intake, initial, followup)


def _verify_failure(failure, context, intake, initial, first, followup, prior, simulation):
    profile, config, manifest, refs, count, final, material = context
    keys(failure, {"state", "calls", "truncated", "overhead_seconds"})
    phase_prior = first["data"]["provenance"].get("elapsed_seconds", 0) if first else 0
    remaining = max(0, min(phase_prior + MAX_REVIEW_SECONDS, config["limits"]["total_review_seconds"]) - prior.get("elapsed_seconds", 0))
    number(failure["overhead_seconds"], 0, remaining)
    if abs(failure["state"]["time_limit_seconds"] + failure["overhead_seconds"] - remaining) > 1e-9:
        raise VerifierError("escalation_failure_deadline_binding")
    if type(failure["calls"]) is not list or type(failure["truncated"]) is not bool:
        raise VerifierError("escalation_failure_evidence")
    # The diagnostic text is never interpreted as a judgment. The separately
    # replayed state binds completed reports and the exact interrupted request.
    committee.verify_failure(failure["state"], profile["escalation"], config, manifest, refs, count, final,
                             material(profile["escalation"]), prior, simulation,
                             files=intake["data"]["files"], plan=_plan(first, "escalation"),
                             validator=lambda v: _check_decision(v, intake, initial, followup),
                             execution_version=committee.SERIAL_EXECUTION)


def _resume(value, context, intake, initial, first, followup, prior, simulation):
    profile = context[0]
    parents = [intake, initial] + ([first, followup] if first is not None else [])
    data = verify_envelope(value, "review_failure", intake, parents)
    keys(data, {"phase", "error", "calls", "truncated", "publishable", "escalation_checkpoint"})
    if data["phase"] != ("final" if first else "initial") or data["publishable"] is not False:
        raise VerifierError("escalation_resume_phase")
    if value["simulation"] and not simulation:
        raise VerifierError("simulation_downgrade")
    saved = data["escalation_checkpoint"]
    keys(saved, {"selection", "primary", "failures", "elapsed_seconds"})
    if canonical(saved["selection"]) != canonical(selection(profile, intake)) or not saved["selection"]["selected"]:
        raise VerifierError("escalation_selection_binding")
    _verify_entry(saved["primary"], "primary", context, intake, initial, first, followup, prior, simulation)
    failures = saved["failures"]
    if type(failures) is not list or not 1 <= len(failures) <= 128:
        raise VerifierError("escalation_recovery_count")
    used = add(prior, spent(saved["primary"]))
    for failure in failures:
        _verify_failure(failure, context, intake, initial, first, followup, used, simulation)
        used = add(used, failure_spent(failure))
    total = _budget(profile, context[1], [saved["primary"]], failures, prior)
    if saved["elapsed_seconds"] != total["elapsed_seconds"]:
        raise VerifierError("escalation_checkpoint_time_binding")
    return saved["primary"], failures, saved["elapsed_seconds"]


def _serial_run(intake, initial, first=None, followup=None, *, providers=None, capture=None, failure_out=None, resume=None):
    context = _context(intake, initial, first, followup)
    profile, config, manifest, refs, count, final, material = context
    selected = selection(profile, intake)
    prior = first["data"]["provenance"] if final else ZERO
    parents = [intake, initial] + ([first, followup] if final else [])
    simulation = providers is not None or any(p["simulation"] for p in parents) or bool(resume and resume["simulation"])
    capture = capture or ReviewDiagnostics(intake, parents, "final" if final else "initial", simulation)
    if providers is not None and (type(providers) is not dict or providers.keys() != {"primary", "escalation"}):
        raise VerifierError("escalation_provider_children")
    primary, recovery, recovery_elapsed = (None, [], 0) if resume is None else _resume(resume, context, intake, initial, first, followup, prior, simulation)
    started = time.monotonic()
    previous = add(spent(primary), *(failure_spent(f) for f in recovery))
    overhead = recovery_elapsed - previous["elapsed_seconds"] if recovery else 0
    phase_limit = min(MAX_REVIEW_SECONDS, config["limits"]["total_review_seconds"] - prior.get("elapsed_seconds", 0))
    deadline = started + (phase_limit - recovery_elapsed)
    entries = {}
    for name in ("primary", "escalation"):
        if name == "escalation" and not selected["selected"]:
            entries[name] = None
            continue
        carried = _carry(first, name)
        if name == "primary" and primary is not None:
            entries[name] = primary
            continue
        if carried is not None:
            entries[name] = carried
            continue
        used = add(prior, *(spent(e) for e in entries.values()), *(failure_spent(f) for f in recovery))
        used["elapsed_seconds"] += overhead
        diagnostic_start = len(capture.calls)
        try:
            value, simulated = committee.run(profile[name], config, manifest, refs, count, final,
                material(profile[name]), used, providers=providers[name] if providers else None,
                files=intake["data"]["files"], plan=_plan(first, name), capture=capture, deadline=deadline, serial=True,
                validator=lambda v: _check_decision(v, intake, initial, followup))
            simulation |= simulated
            _check_decision(value, intake, initial, followup)
            entries[name] = {"status": "completed", "carried": False, "review": value}
        except VerifierError as exc:
            if name == "primary" or not hasattr(exc, "review_state"):
                raise
            remaining = max(0, phase_limit - (used["elapsed_seconds"] - prior.get("elapsed_seconds", 0)))
            failure = {"state": exc.review_state, "calls": capture.calls[diagnostic_start:], "truncated": capture.truncated,
                       "overhead_seconds": max(0, remaining - exc.review_state["time_limit_seconds"])}
            saved = {"selection": selected, "primary": entries["primary"], "failures": recovery + [failure],
                     "elapsed_seconds": add(spent(entries["primary"]), *(failure_spent(f) for f in recovery + [failure]))["elapsed_seconds"]}
            capture.checkpoint = saved
            exc.escalation_checkpoint = capture.envelope(exc)
            if profile["mode"] == "override":
                raise
            if failure_out is not None:
                capture.write(failure_out, exc)
            entries[name] = {"status": "failed", "carried": False, "failure": failure}
    total = add(*(spent(e) for e in entries.values()), *(failure_spent(f) for f in recovery))
    elapsed = max(total["elapsed_seconds"] + overhead, recovery_elapsed + (time.monotonic() - started))
    if entries["escalation"] is not None and entries["escalation"]["status"] == "failed":
        # A killed/timed-out audit consumes the remaining reservation. Keep its
        # actual wall overrun in failure evidence without discarding the primary.
        elapsed = min(phase_limit, elapsed)
    result = {"selection": selected, **entries, "recovery": recovery, "recovery_elapsed_seconds": recovery_elapsed,
              **_result(profile, selected, entries["primary"], entries["escalation"], manifest, config, count),
              "provenance": _budget(profile, config, entries.values(), recovery, prior, elapsed)}
    return result, simulation


def _serial_verify(value, intake, initial, first, followup, simulation):
    """Explicit historical replay only; current intakes never select this path."""
    context = _context(intake, initial, first, followup)
    profile, config, manifest, refs, count, final, material = context
    keys(value, {"selection", "primary", "escalation", "recovery", "recovery_elapsed_seconds", "decision", "authority", "comparison", "provenance"})
    chosen = selection(profile, intake)
    if canonical(value["selection"]) != canonical(chosen):
        raise VerifierError("escalation_selection_binding")
    prior = first["data"]["provenance"] if final else ZERO
    _verify_entry(value["primary"], "primary", context, intake, initial, first, followup, prior, simulation)
    recovery = value["recovery"]
    if type(recovery) is not list or len(recovery) > 128 or recovery and not chosen["selected"]:
        raise VerifierError("escalation_recovery_count")
    used = add(prior, spent(value["primary"]))
    for failure in recovery:
        _verify_failure(failure, context, intake, initial, first, followup, used, simulation)
        used = add(used, failure_spent(failure))
    recovery_spent = add(spent(value["primary"]), *(failure_spent(f) for f in recovery))
    elapsed = value["recovery_elapsed_seconds"]
    if recovery:
        if elapsed != recovery_spent["elapsed_seconds"]:
            raise VerifierError("escalation_recovery_time")
        used["elapsed_seconds"] = prior.get("elapsed_seconds", 0) + elapsed
    elif type(elapsed) not in (int, float) or elapsed != 0:
        raise VerifierError("escalation_recovery_time")
    entry = value["escalation"]
    if not chosen["selected"]:
        if entry is not None:
            raise VerifierError("unselected_escalation_review")
    elif entry is not None and entry.get("status") == "failed" and _carry(first, "escalation") is None:
        keys(entry, {"status", "carried", "failure"})
        if entry["carried"] is not False or profile["mode"] != "record-only":
            raise VerifierError("escalation_failed_authority")
        _verify_failure(entry["failure"], context, intake, initial, first, followup, used, simulation)
    else:
        _verify_entry(entry, "escalation", context, intake, initial, first, followup, used, simulation)
    expected = _result(profile, chosen, value["primary"], entry, manifest, config, count)
    if any(canonical(value[k]) != canonical(v) for k, v in expected.items()):
        raise VerifierError("escalation_decision_binding")
    total = _budget(profile, config, [value["primary"], entry], recovery, prior)
    if recovery:
        total["elapsed_seconds"] += elapsed - recovery_spent["elapsed_seconds"]
    provenance = value["provenance"]
    keys(provenance, set(total))
    if any(canonical(provenance[k]) != canonical(v) for k, v in total.items() if k != "elapsed_seconds"):
        raise VerifierError("escalation_budget_binding")
    number(provenance["elapsed_seconds"], total["elapsed_seconds"],
           min(MAX_REVIEW_SECONDS, config["limits"]["total_review_seconds"] - prior.get("elapsed_seconds", 0)))
    return value


def verify(value, intake, initial, first, followup, simulation):
    from .parallel_escalation import verify as parallel_verify
    return parallel_verify(value, intake, initial, first, followup, simulation)


def run(intake, initial, first=None, followup=None, *, providers=None, capture=None, failure_out=None, resume=None):
    from .parallel_escalation import run as parallel_run
    return parallel_run(intake, initial, first, followup, providers=providers, capture=capture,
                        failure_out=failure_out, resume=resume)
