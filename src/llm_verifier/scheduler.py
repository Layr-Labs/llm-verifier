"""Bounded independent model calls with shared admission and replayable waves."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Lock
import time

from . import committee as protocol, regions
from .diagnostics import ReviewDiagnostics, error_code, review_call
from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, digest, integer, keys, number, text
from .provider import MAX_REVIEW_SECONDS, OpenAI

VERSION = "independent-waves-v1"
MAX_WORKERS = 4
EXHAUSTED = "committee_review_budget_exhausted"


def limit(config, prior):
    return max(0, min(MAX_REVIEW_SECONDS, config["limits"]["total_review_seconds"] - prior.get("elapsed_seconds", 0)))


def code(exc):
    if not isinstance(exc, VerifierError):
        return "review_failed"
    value = error_code(exc)
    return value if isinstance(exc, InfrastructureError) else "committee_invalid_review_" + value


def binding(member, messages, schema, context):
    value = {"member_id": member["id"], "stage": member["stage"], "prompt_sha256": digest(messages),
             "schema_sha256": digest(schema), "provider_config_sha256": digest(member["provider"])}
    if member["stage"] == "region":
        value["region_id"] = context["region"]["id"]
    return value


class Coordinator:
    """One executor and one non-refundable reservation ledger per phase."""
    def __init__(self, config, prior, *, seconds=None, deadline=None):
        self.config, self.prior = config, prior
        self.started = time.monotonic()
        self.limit = limit(config, prior)
        if seconds is not None:
            self.limit = min(self.limit, max(0, seconds))
        if deadline is not None:
            self.limit = min(self.limit, max(0, deadline - self.started))
        self.attempts = self.tokens = 0
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="judge")
        self.resources = {}

    def elapsed(self):
        return time.monotonic() - self.started

    def reserve(self, member, call, observe):
        with self.lock:
            remaining = self.limit - self.elapsed()
            cap = member["provider"]["max_completion_tokens"]
            limits = self.config["limits"]
            if (remaining <= 0 or self.attempts + self.prior.get("attempts", 0) >= limits["total_provider_requests"]
                    or self.tokens + self.prior.get("allotted_completion_tokens", 0) + cap > limits["total_review_tokens"]):
                raise InfrastructureError(EXHAUSTED)
            self.attempts += 1
            self.tokens += cap
            call["attempts"] += 1
            observe.grant(call["attempts"], cap)
            return remaining

    def resource(self, provider):
        with self.lock:
            return self.resources.setdefault(id(provider), Lock())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # All admitted work settles before exposing a failure/checkpoint.
        self.pool.shutdown(wait=True)


def provenance(committee, calls, elapsed):
    members = {m["id"]: m for m in committee["members"]}
    return {"protocol": committee["protocol"], "committee_sha256": digest(committee),
            "attempts": sum(c["attempts"] for c in calls), "elapsed_seconds": elapsed,
            "allotted_completion_tokens": sum(c["attempts"] * members[c["member_id"]]["provider"]["max_completion_tokens"] for c in calls)}


class CallFailure:
    def __init__(self, error):
        self.error = error


def deliver(value):
    if isinstance(value, CallFailure):
        raise InfrastructureError(value.error)
    return value


class Runtime:
    def __init__(self, coordinator, committee, material, providers, capture, *, owned):
        self.coordinator, self.committee, self.material = coordinator, committee, material
        self.providers = providers
        self.capture = capture or ReviewDiagnostics({}, [], "review", providers is not None)
        self.offset = 0 if owned else min(coordinator.limit, coordinator.elapsed())
        self.seconds = max(0, coordinator.limit - self.offset)
        self.execution = {"version": VERSION, "offset_seconds": self.offset,
                          "time_limit_seconds": self.seconds, "waves": []}
        self.records = []

    def elapsed(self):
        return self.coordinator.elapsed() - self.offset

    def invoke(self, member, schema, context, messages, call, observe, provider):
        try:
            with self.coordinator.resource(provider) if provider is not None else nullcontext():
                if provider is None:
                    provider = OpenAI(member["provider"])
                seconds = self.seconds - self.elapsed()
                if seconds <= 0:
                    raise InfrastructureError(EXHAUSTED)
                first = True
                def reserve():
                    nonlocal first
                    if first:
                        first = False
                        remaining = self.seconds - self.elapsed()
                        if remaining <= 0:
                            raise InfrastructureError(EXHAUSTED)
                        return remaining
                    return self.coordinator.reserve(member, call, observe)
                native = type(provider) is OpenAI
                requests = member["provider"]["max_attempts"] if native else 1
                report, saved = review_call(provider, member["provider"], messages, schema, seconds, requests,
                    observer=observe, reserve=reserve if native else None)
                protocol._provenance(saved, member, messages, schema, self.providers is not None or provider.simulation,
                                     seconds, requests)
                if saved["attempts"] != call["attempts"]:
                    raise VerifierError("committee_attempt_binding")
                if self.elapsed() > self.seconds:
                    raise InfrastructureError(EXHAUSTED)
                record = {"member_id": member["id"], "stage": member["stage"], "report": report, "provenance": saved}
                if member["stage"] == "region":
                    record["region_id"] = context["region"]["id"]
                observe.finish(call["attempts"], None, record)
                return record
        except Exception as exc:
            call["error"] = code(exc)
            if observe is not None:
                observe({"error": call["error"]})
                observe.finish(call["attempts"], call["error"], None)
            return None
        finally:
            call["finished_seconds"] = self.elapsed()

    def wave(self, tasks):
        prepared = []
        # Prompt construction and diagnostic slots are deterministic and precede
        # dispatch. Mutable injected objects occur at most once per wave.
        prompts = [self.material(*task) for task in tasks]
        for (member, schema, context), messages in zip(tasks, prompts):
            call = {**binding(member, messages, schema, context), "attempts": 0, "error": None, "finished_seconds": None}
            observe = self.capture.start(member["id"], member["stage"], member["provider"], messages, schema,
                context["region"]["id"] if member["stage"] == "region" else None) if self.capture else None
            provider = self.providers[member["id"]] if self.providers is not None else None
            prepared.append((member, schema, context, messages, call, observe, provider))
        wave = {"started_seconds": self.elapsed(), "finished_seconds": None, "calls": [p[4] for p in prepared]}
        self.execution["waves"].append(wave)
        # Admit first attempts in task order, without reserving a full retry cap.
        for member, _, _, _, call, observe, _ in prepared:
            try:
                self.coordinator.reserve(member, call, observe)
            except InfrastructureError:
                call["error"] = EXHAUSTED
                call["finished_seconds"] = self.elapsed()
                observe.finish(0, EXHAUSTED, None)
        futures = [self.coordinator.pool.submit(self.invoke, *p) if p[4]["error"] is None else None for p in prepared]
        records = [future.result() if future is not None else None for future in futures]
        wave["finished_seconds"] = self.elapsed()
        self.records.extend(record for record in records if record is not None)
        return [record["report"] if record is not None else CallFailure(call["error"])
                for record, call in zip(records, wave["calls"])]

    def call(self, member, schema, context):
        return deliver(self.wave([(member, schema, context)])[0])

    def batch(self, tasks):
        index = 0
        while index < len(tasks):
            wave, used = [], set()
            while index < len(tasks) and len(wave) < MAX_WORKERS:
                task = tasks[index]
                identity = id(self.providers[task[0]["id"]]) if self.providers is not None else index
                if identity in used:
                    break
                wave.append(task)
                used.add(identity)
                index += 1
            for result in self.wave(wave):
                yield deliver(result)


def _wave_run(committee, config, manifest, refs, previous_runs, final, material, prior, *, providers=None,
        files=None, plan=None, capture=None, deadline=None, validator=None, coordinator=None):
    registry = {m["id"] for m in committee["members"]}
    if providers is not None and (type(providers) is not dict or providers.keys() != registry):
        raise VerifierError("committee_provider_members")
    owned = coordinator is None
    manager = Coordinator(config, prior, deadline=deadline) if owned else nullcontext(coordinator)
    with manager as shared:
        runtime = Runtime(shared, committee, material, providers, capture, owned=owned)
        details = {}
        try:
            decision = protocol._protocol(committee, config, manifest, refs, previous_runs, final, runtime.call,
                files=files, plan=plan, details=details, batch=runtime.batch)
            if validator is not None:
                validator({"decision": decision})
            if runtime.elapsed() > runtime.seconds:
                # Terminal validation/overhead can cross the deadline after the
                # response. Preserve that raw response only in diagnostics.
                last = runtime.execution["waves"][-1]["calls"][-1]
                last["error"] = EXHAUSTED
                runtime.records.pop()
                runtime.capture.calls[-1]["outcome"] = {"attempts": last["attempts"], "error": EXHAUSTED, "record_sha256": None}
                raise InfrastructureError(EXHAUSTED)
        except Exception as exc:
            elapsed = runtime.elapsed()
            failure = InfrastructureError(code(exc))
            calls = [c for w in runtime.execution["waves"] for c in w["calls"]]
            failure.review_state = {"error": code(exc), "reviews": runtime.records, "execution": runtime.execution,
                "calls": runtime.capture.calls,
                "wall_elapsed_seconds": elapsed, "time_limit_seconds": runtime.seconds,
                "provenance": provenance(committee, calls, min(elapsed, runtime.seconds))}
            raise failure from None
        elapsed = runtime.elapsed()
        calls = [c for w in runtime.execution["waves"] for c in w["calls"]]
        return {"decision": decision, "reviews": runtime.records, **details, "execution": runtime.execution,
                "provenance": provenance(committee, calls, elapsed)}, providers is not None


class ReplayBinding(Exception):
    pass


class Replay:
    def __init__(self, value, committee, config, material, prior, simulation, seconds):
        self.value, self.committee, self.config = value, committee, config
        self.material, self.prior, self.simulation = material, prior, simulation
        self.seconds = seconds
        self.index = self.record_index = 0
        self.calls = []
        self.finished = 0

    def wave(self, tasks):
        # Material errors happen before any dispatch and are replayable protocol
        # failures. Artifact/binding errors below must never masquerade as them.
        messages = [self.material(*task) for task in tasks]
        try:
            waves = self.value["execution"]["waves"]
            if self.index >= len(waves):
                raise VerifierError("committee_missing_wave")
            wave = waves[self.index]
            self.index += 1
            keys(wave, {"started_seconds", "finished_seconds", "calls"})
            number(wave["started_seconds"], self.finished, 10**9)
            number(wave["finished_seconds"], wave["started_seconds"], 10**9)
            self.finished = wave["finished_seconds"]
            if type(wave["calls"]) is not list or len(wave["calls"]) != len(tasks):
                raise VerifierError("committee_wave_binding")
            reports = []
            for (member, schema, context), call, prompt in zip(tasks, wave["calls"], messages):
                expected = binding(member, prompt, schema, context)
                keys(call, set(expected) | {"attempts", "error", "finished_seconds"})
                number(call["finished_seconds"], wave["started_seconds"], wave["finished_seconds"])
                if any(call[k] != v for k, v in expected.items()):
                    raise VerifierError("committee_call_binding")
                integer(call["attempts"], 0, member["provider"]["max_attempts"])
                if call["error"] is not None:
                    text(call["error"], 128)
                    if error_code(InfrastructureError(call["error"])) != call["error"]:
                        raise VerifierError("committee_call_error")
                spent = provenance(self.committee, self.calls, 0)
                available = min(self.config["limits"]["total_provider_requests"] - self.prior.get("attempts", 0) - spent["attempts"],
                    (self.config["limits"]["total_review_tokens"] - self.prior.get("allotted_completion_tokens", 0) - spent["allotted_completion_tokens"]) // member["provider"]["max_completion_tokens"])
                if call["attempts"] > available:
                    raise VerifierError("committee_reservation_binding")
                if not call["attempts"] and call["error"] != EXHAUSTED:
                    raise VerifierError("committee_empty_reservation")
                self.calls.append(call)
                receipt = None
                if "calls" in self.value:
                    evidence = self.value["calls"][len(self.calls) - 1]
                    if any(evidence.get(k) != v for k, v in expected.items()):
                        raise VerifierError("committee_receipt_binding")
                    grants = evidence.get("grants")
                    expected_grants = [{"attempt": i, "completion_tokens": member["provider"]["max_completion_tokens"]}
                                       for i in range(1, call["attempts"] + 1)]
                    if canonical(grants) != canonical(expected_grants):
                        raise VerifierError("committee_reservation_evidence")
                    for event in evidence.get("events", []):
                        if "attempt" in event and not 1 <= event["attempt"] <= call["attempts"]:
                            raise VerifierError("committee_attempt_evidence")
                    receipt = evidence.get("outcome")
                    keys(receipt, {"attempts", "error", "record_sha256"})
                    if receipt["attempts"] != call["attempts"] or receipt["error"] != call["error"]:
                        raise VerifierError("committee_receipt_binding")
                if call["error"] is not None:
                    if receipt is not None and receipt["record_sha256"] is not None:
                        raise VerifierError("committee_discarded_review")
                    reports.append(CallFailure(call["error"]))
                    continue
                if call["finished_seconds"] > self.seconds or not call["attempts"]:
                    raise VerifierError("committee_late_review")
                records = self.value["reviews"]
                if self.record_index >= len(records):
                    raise VerifierError("committee_missing_review")
                record = records[self.record_index]
                self.record_index += 1
                expected_record = {k: expected[k] for k in ("member_id", "stage")}
                if "region_id" in expected:
                    expected_record["region_id"] = expected["region_id"]
                keys(record, set(expected_record) | {"report", "provenance"})
                if any(record[k] != v for k, v in expected_record.items()):
                    raise VerifierError("committee_stage_binding")
                protocol._provenance(record["provenance"], member, prompt, schema,
                    self.simulation, call["finished_seconds"] - wave["started_seconds"], call["attempts"])
                if record["provenance"]["attempts"] != call["attempts"]:
                    raise VerifierError("committee_attempt_binding")
                if receipt is not None and receipt["record_sha256"] != digest(record):
                    raise VerifierError("committee_receipt_binding")
                reports.append(record["report"])
        except (VerifierError, IndexError, KeyError, TypeError) as exc:
            raise ReplayBinding() from exc
        return reports

    def call(self, member, schema, context):
        return deliver(self.wave([(member, schema, context)])[0])

    def batch(self, tasks):
        index = 0
        while index < len(tasks):
            if self.index >= len(self.value["execution"]["waves"]):
                for task in tasks[index:]:
                    self.material(*task)
                raise ReplayBinding()
            try:
                count = len(self.value["execution"]["waves"][self.index]["calls"])
                if not 1 <= count <= min(MAX_WORKERS, len(tasks) - index):
                    raise ReplayBinding()
            except (IndexError, KeyError, TypeError):
                raise ReplayBinding() from None
            for result in self.wave(tasks[index:index + count]):
                yield deliver(result)
            index += count


def verify(value, committee, config, manifest, refs, previous_runs, final, material, prior, simulation, *,
           files=None, plan=None, validator=None, failed=False, phase_limit=None):
    if value.get("execution", {}).get("version") == "dependency-ready-v1":
        from .ready_scheduler import verify as ready_verify
        return ready_verify(value, committee, config, manifest, refs, previous_runs, final, material, prior,
                            simulation, files=files, plan=plan, validator=validator, failed=failed, phase_limit=phase_limit)
    extra = {"region_plan", "region_coverage"} if committee["protocol"] == regions.PROTOCOL else set()
    keys(value, {"reviews", "provenance", "execution"} | ({"error", "wall_elapsed_seconds", "time_limit_seconds"}
        if failed else {"decision"} | extra) | ({"calls"} if failed else set()))
    execution = value["execution"]
    keys(execution, {"version", "offset_seconds", "time_limit_seconds", "waves"})
    if execution["version"] != VERSION:
        raise VerifierError("committee_execution_version")
    maximum = len(committee["members"]) + committee.get("max_regions", 1) - 1
    if (type(execution["waves"]) is not list or len(execution["waves"]) > maximum or
            type(value["reviews"]) is not list or len(value["reviews"]) > maximum):
        raise VerifierError("committee_review_count")
    seconds = limit(config, prior) if phase_limit is None else min(limit(config, prior), phase_limit)
    number(execution["offset_seconds"], 0, seconds)
    expected_limit = max(0, seconds - execution["offset_seconds"])
    number(execution["time_limit_seconds"], 0, expected_limit)
    if abs(execution["time_limit_seconds"] - expected_limit) > 1e-9:
        raise VerifierError("committee_deadline_binding")
    replay = Replay(value, committee, config, material, prior, simulation, execution["time_limit_seconds"])
    details = {}
    try:
        decision = protocol._protocol(committee, config, manifest, refs, previous_runs, final, replay.call,
            files=files, plan=plan, details=details, batch=replay.batch)
        if validator is not None:
            validator({"decision": decision})
    except ReplayBinding:
        raise VerifierError("committee_replay_binding") from None
    except VerifierError as exc:
        if not failed or code(exc) != value["error"]:
            raise VerifierError("committee_failure_binding") from None
    else:
        if failed:
            # A fully replayed judgment is never admissible failure evidence.
            raise VerifierError("failure_contains_completed_review")
        if canonical(decision) != canonical(value["decision"]) or any(canonical(value[k]) != canonical(details[k]) for k in extra):
            raise VerifierError("committee_decision_binding")
    if failed and (type(value["calls"]) is not list or len(value["calls"]) != len(replay.calls)):
        raise VerifierError("committee_receipt_count")
    if replay.index != len(execution["waves"]) or replay.record_index != len(value["reviews"]):
        raise VerifierError("committee_unused_evidence")
    elapsed = value["wall_elapsed_seconds"] if failed else value["provenance"]["elapsed_seconds"]
    number(elapsed, replay.finished, 10**9 if failed else execution["time_limit_seconds"])
    if failed and value["time_limit_seconds"] != execution["time_limit_seconds"]:
        raise VerifierError("committee_deadline_binding")
    expected = provenance(committee, replay.calls, min(elapsed, execution["time_limit_seconds"]))
    if canonical(value["provenance"]) != canonical(expected):
        raise VerifierError("committee_budget_binding")
    return value


def run(*args, **kwargs):
    from .ready_scheduler import run as ready_run
    return ready_run(*args, **kwargs)
