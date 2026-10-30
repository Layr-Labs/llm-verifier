"""Dependency-ready calls with rolling, phase-global admission and replay."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from threading import Lock
import time

from . import committee as protocol, regions, scheduler as old
from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, digest, integer, keys, number, text

VERSION = "dependency-ready-v1"


def capacity(config):
    return config["limits"].get("max_concurrent_calls", config["limits"]["total_provider_requests"])


class Coordinator(old.Coordinator):
    def __init__(self, config, prior, *, seconds=None, deadline=None):
        self.config, self.prior = config, prior
        self.started = time.monotonic()
        self.limit = old.limit(config, prior)
        if seconds is not None:
            self.limit = min(self.limit, max(0, seconds))
        if deadline is not None:
            self.limit = min(self.limit, max(0, deadline - self.started))
        self.attempts = self.tokens = 0
        self.lock = Lock()
        # Explicit finite graph/request bound: never use Python's default pool size.
        self.pool = ThreadPoolExecutor(max_workers=capacity(config), thread_name_prefix="judge")
        self.resources = {}
        self.queue_lock = Lock()
        self.tails = {}

    def submit(self, fn, args, resource=None):
        """Serialize aliases without parking pool workers on a resource lock."""
        result = Future()
        with self.queue_lock:
            previous = self.tails.get(resource) if resource is not None else None
            if resource is not None:
                self.tails[resource] = result

        def dispatch(_=None):
            try:
                inner = self.pool.submit(fn, *args)
            except Exception as exc:
                result.set_exception(exc)
                return

            def complete(done):
                try:
                    result.set_result(done.result())
                except Exception as exc:
                    result.set_exception(exc)
            inner.add_done_callback(complete)

        if previous is None:
            dispatch()
        else:
            previous.add_done_callback(dispatch)
        return result


class Runtime(old.Runtime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution = {"version": VERSION, "offset_seconds": self.offset,
                          "time_limit_seconds": self.seconds,
                          "max_concurrent_calls": self.coordinator.config["limits"].get("max_concurrent_calls"),
                          "groups": []}
        self.pending = []

    def invoke(self, member, schema, context, messages, call, observe, provider):
        call["started_seconds"] = self.elapsed()
        try:
            # Queued work consumes no generation reservation until a slot opens.
            self.coordinator.reserve(member, call, observe)
        except Exception as exc:
            call["error"] = old.code(exc)
            call["finished_seconds"] = self.elapsed()
            observe.finish(call["attempts"], call["error"], None)
            return None
        return super().invoke(member, schema, context, messages, call, observe, provider)

    def submit(self, tasks):
        if not tasks:
            return []
        # A group is a deterministic preparation unit, not a scheduling barrier.
        prompts = [self.material(*task) for task in tasks]
        prepared = []
        for (member, schema, context), messages in zip(tasks, prompts):
            call = {**old.binding(member, messages, schema, context), "attempts": 0, "error": None,
                    "started_seconds": None, "finished_seconds": None}
            observe = self.capture.start(member["id"], member["stage"], member["provider"], messages, schema,
                context["region"]["id"] if member["stage"] == "region" else None)
            provider = self.providers[member["id"]] if self.providers is not None else None
            prepared.append((member, schema, context, messages, call, observe, provider))
        group = {"queued_seconds": self.elapsed(), "finished_seconds": None, "calls": [p[4] for p in prepared]}
        self.execution["groups"].append(group)
        futures = [self.coordinator.submit(self.invoke, p, id(p[-1]) if p[-1] is not None else None) for p in prepared]
        self.pending.extend(futures)
        return futures

    def gather(self, futures):
        for future in futures:
            record = future.result()
            if record is None:
                # Match the failed future to its stable logical call slot.
                index = self.pending.index(future)
                call = [c for g in self.execution["groups"] for c in g["calls"]][index]
                raise InfrastructureError(call["error"])
            yield record["report"]

    def call(self, member, schema, context):
        return next(self.gather(self.submit([(member, schema, context)])))

    def drain(self):
        # Always drain all submitted ready peers, including after planner failure.
        self.records = [record for future in self.pending if (record := future.result()) is not None]
        for group in self.execution["groups"]:
            group["finished_seconds"] = max(c["finished_seconds"] for c in group["calls"])


def run(committee, config, manifest, refs, previous_runs, final, material, prior, *, providers=None,
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
                files=files, plan=plan, details=details, ready=runtime)
            if validator is not None:
                validator({"decision": decision})
            runtime.drain()
            if runtime.elapsed() > runtime.seconds:
                last = runtime.execution["groups"][-1]["calls"][-1]
                last["error"] = old.EXHAUSTED
                runtime.records.pop()
                runtime.capture.calls[-1]["outcome"] = {"attempts": last["attempts"], "error": old.EXHAUSTED, "record_sha256": None}
                raise InfrastructureError(old.EXHAUSTED)
        except Exception as exc:
            runtime.drain()
            # A terminal validation overrun invalidates the last logical report.
            invalid = {id(c) for g in runtime.execution["groups"] for c in g["calls"] if c["error"] is not None}
            runtime.records = [r for c, f in zip([c for g in runtime.execution["groups"] for c in g["calls"]], runtime.pending)
                               if id(c) not in invalid and (r := f.result()) is not None]
            elapsed = runtime.elapsed()
            calls = [c for g in runtime.execution["groups"] for c in g["calls"]]
            failure = InfrastructureError(old.code(exc))
            failure.review_state = {"error": old.code(exc), "reviews": runtime.records, "execution": runtime.execution,
                "calls": runtime.capture.calls, "wall_elapsed_seconds": elapsed, "time_limit_seconds": runtime.seconds,
                "provenance": old.provenance(committee, calls, min(elapsed, runtime.seconds))}
            raise failure from None
        elapsed = runtime.elapsed()
        calls = [c for g in runtime.execution["groups"] for c in g["calls"]]
        return {"decision": decision, "reviews": runtime.records, **details, "execution": runtime.execution,
                "provenance": old.provenance(committee, calls, elapsed)}, providers is not None


class Replay(old.Replay):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queued = self.available = 0
        self.ends = {}

    def submit(self, tasks):
        if not tasks:
            return []
        prompts = [self.material(*task) for task in tasks]
        try:
            group = self.value["execution"]["groups"][self.index]
            self.index += 1
            keys(group, {"queued_seconds", "finished_seconds", "calls"})
            number(group["queued_seconds"], max(self.queued, self.available), 10**9)
            self.queued = group["queued_seconds"]
            number(group["finished_seconds"], group["queued_seconds"], 10**9)
            if type(group["calls"]) is not list or len(group["calls"]) != len(tasks):
                raise old.ReplayBinding()
            reports = []
            for (member, schema, context), prompt, call in zip(tasks, prompts, group["calls"]):
                expected = old.binding(member, prompt, schema, context)
                keys(call, set(expected) | {"attempts", "error", "started_seconds", "finished_seconds"})
                number(call["started_seconds"], group["queued_seconds"], group["finished_seconds"])
                number(call["finished_seconds"], call["started_seconds"], group["finished_seconds"])
                if any(call[k] != v for k, v in expected.items()):
                    raise old.ReplayBinding()
                integer(call["attempts"], 0, member["provider"]["max_attempts"])
                if call["error"] is not None:
                    text(call["error"], 128)
                    if old.code(InfrastructureError(call["error"])) != call["error"]:
                        raise old.ReplayBinding()
                if not call["attempts"] and call["error"] != old.EXHAUSTED:
                    raise old.ReplayBinding()
                self.calls.append(call)
                spent = old.provenance(self.committee, self.calls, 0)
                if (spent["attempts"] + self.prior.get("attempts", 0) > self.config["limits"]["total_provider_requests"] or
                    spent["allotted_completion_tokens"] + self.prior.get("allotted_completion_tokens", 0) > self.config["limits"]["total_review_tokens"]):
                    raise old.ReplayBinding()
                receipt = None
                if "calls" in self.value:
                    evidence = self.value["calls"][len(self.calls) - 1]
                    if any(evidence.get(k) != v for k, v in expected.items()):
                        raise old.ReplayBinding()
                    grants = [{"attempt": i, "completion_tokens": member["provider"]["max_completion_tokens"]}
                              for i in range(1, call["attempts"] + 1)]
                    if canonical(evidence.get("grants")) != canonical(grants):
                        raise old.ReplayBinding()
                    for event in evidence.get("events", []):
                        if "attempt" in event and not 1 <= event["attempt"] <= call["attempts"]:
                            raise old.ReplayBinding()
                    receipt = evidence.get("outcome")
                    keys(receipt, {"attempts", "error", "record_sha256"})
                    if receipt["attempts"] != call["attempts"] or receipt["error"] != call["error"]:
                        raise old.ReplayBinding()
                if call["error"] is not None:
                    if receipt is not None and receipt["record_sha256"] is not None:
                        raise old.ReplayBinding()
                    failure = old.CallFailure(call["error"])
                    reports.append(failure)
                    self.ends[id(failure)] = call["finished_seconds"]
                    continue
                if call["finished_seconds"] > self.seconds:
                    raise old.ReplayBinding()
                record = self.value["reviews"][self.record_index]
                self.record_index += 1
                record_binding = {k: expected[k] for k in ("member_id", "stage")}
                if "region_id" in expected:
                    record_binding["region_id"] = expected["region_id"]
                keys(record, set(record_binding) | {"report", "provenance"})
                if any(record[k] != v for k, v in record_binding.items()):
                    raise old.ReplayBinding()
                protocol._provenance(record["provenance"], member, prompt, schema, self.simulation,
                    call["finished_seconds"] - call["started_seconds"], call["attempts"])
                if record["provenance"]["attempts"] != call["attempts"]:
                    raise old.ReplayBinding()
                if receipt is not None and receipt["record_sha256"] != digest(record):
                    raise old.ReplayBinding()
                reports.append(record["report"])
                self.ends[id(record["report"])] = call["finished_seconds"]
            if group["finished_seconds"] != max(c["finished_seconds"] for c in group["calls"]):
                raise old.ReplayBinding()
            self.finished = max(self.finished, group["finished_seconds"])
        except (VerifierError, IndexError, KeyError, TypeError) as exc:
            raise old.ReplayBinding() from exc
        return reports

    def gather(self, reports):
        for report in reports:
            self.available = max(self.available, self.ends[id(report)])
            yield old.deliver(report)

    def call(self, member, schema, context):
        return next(self.gather(self.submit([(member, schema, context)])))


def verify_active_calls(values, config):
    events = []
    for value in values:
        execution = value.get("execution", {})
        if execution.get("version") != VERSION:
            continue  # Historical wave records keep their own versioned rules.
        offset = execution["offset_seconds"]
        for group in execution["groups"]:
            for call in group["calls"]:
                start, finish = call["started_seconds"], call["finished_seconds"]
                if finish > start:
                    events.extend(((offset + start, 1), (offset + finish, -1)))
    active = 0
    for _, change in sorted(events):
        active += change
        if active > capacity(config):
            raise VerifierError("committee_concurrency_binding")


def verify(value, committee, config, manifest, refs, previous_runs, final, material, prior, simulation, *,
           files=None, plan=None, validator=None, failed=False, phase_limit=None):
    extra = {"region_plan", "region_coverage"} if committee["protocol"] == regions.PROTOCOL else set()
    keys(value, {"reviews", "provenance", "execution"} | ({"error", "wall_elapsed_seconds", "time_limit_seconds", "calls"}
        if failed else {"decision"} | extra))
    execution = value["execution"]
    keys(execution, {"version", "offset_seconds", "time_limit_seconds", "max_concurrent_calls", "groups"})
    if execution["version"] != VERSION or execution["max_concurrent_calls"] != config["limits"].get("max_concurrent_calls"):
        raise VerifierError("committee_execution_version")
    maximum = len(committee["members"]) + committee.get("max_regions", 1) - 1
    if (type(execution["groups"]) is not list or len(execution["groups"]) > maximum or
            type(value["reviews"]) is not list or len(value["reviews"]) > maximum):
        raise VerifierError("committee_review_count")
    seconds = old.limit(config, prior) if phase_limit is None else min(old.limit(config, prior), phase_limit)
    number(execution["offset_seconds"], 0, seconds)
    expected_limit = max(0, seconds - execution["offset_seconds"])
    number(execution["time_limit_seconds"], 0, expected_limit)
    if abs(execution["time_limit_seconds"] - expected_limit) > 1e-9:
        raise VerifierError("committee_deadline_binding")
    replay = Replay(value, committee, config, material, prior, simulation, execution["time_limit_seconds"])
    details = {}
    try:
        decision = protocol._protocol(committee, config, manifest, refs, previous_runs, final, replay.call,
            files=files, plan=plan, details=details, ready=replay)
        if validator is not None:
            validator({"decision": decision})
    except old.ReplayBinding:
        raise VerifierError("committee_replay_binding") from None
    except VerifierError as exc:
        if not failed or old.code(exc) != value["error"]:
            raise VerifierError("committee_failure_binding") from None
    else:
        if failed:
            raise VerifierError("failure_contains_completed_review")
        if canonical(decision) != canonical(value["decision"]) or any(canonical(value[k]) != canonical(details[k]) for k in extra):
            raise VerifierError("committee_decision_binding")
    if failed and (type(value["calls"]) is not list or len(value["calls"]) != len(replay.calls)):
        raise VerifierError("committee_receipt_count")
    if replay.index != len(execution["groups"]) or replay.record_index != len(value["reviews"]):
        raise VerifierError("committee_unused_evidence")
    elapsed = value["wall_elapsed_seconds"] if failed else value["provenance"]["elapsed_seconds"]
    number(elapsed, replay.finished, 10**9 if failed else execution["time_limit_seconds"])
    if failed and value["time_limit_seconds"] != execution["time_limit_seconds"]:
        raise VerifierError("committee_deadline_binding")
    expected = old.provenance(committee, replay.calls, min(elapsed, execution["time_limit_seconds"]))
    if canonical(value["provenance"]) != canonical(expected):
        raise VerifierError("committee_budget_binding")
    verify_active_calls([value], config)
    return value
