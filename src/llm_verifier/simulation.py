"""Explicit offline plumbing simulation. Never usable as an official judge."""

from .artifacts import intake_snapshot
from .errors import VerifierError
from .files import read_json, write_json
from .jsonutil import canonical, digest, keys
from .pipeline import dossier, execute, review
from .runner import output_record


class ScriptedRunner:
    simulation = True

    def __init__(self, results):
        self.results = iter(results)

    def run(self, files, source, inputs, timeout):
        try:
            value = next(self.results)
        except StopIteration:
            raise VerifierError("simulation_missing_execution") from None
        return output_record("completed", 0, canonical(value), b"")


class ScriptedProvider:
    simulation = True

    def __init__(self, config, decision, *, sequence=False):
        self.config, self.decision = config, decision
        self.sequence = sequence
        if sequence and type(decision) is not list:
            raise VerifierError("simulation_region_reports")
        self.pending = iter(decision) if sequence else None

    def review(self, messages, schema, seconds, requests):
        decision = self.decision
        if self.sequence:
            try:
                decision = next(self.pending)
            except StopIteration:
                raise VerifierError("simulation_missing_region_report") from None
        return decision, {"provider": "simulation", "endpoint": "none", "configured_model": self.config["model"],
            "returned_model": "simulation-scripted", "request_id": None, "usage": {},
            "prompt_sha256": digest(messages), "schema_sha256": digest(schema),
            "provider_config_sha256": digest(self.config), "attempts": 1, "elapsed_seconds": 0}


def simulate(effective, candidate, script, output, *, accepted_results_context=None):
    committee = effective.get("judging", {}).get("committee")
    first_key, final_key = ("initial_reviews", "final_reviews") if committee else ("initial_decision", "final_decision")
    keys(script, {"initial_results", first_key, "followup_results", final_key})
    def judge(intake, initial, first=None, followup=None):
        supplied = script[final_key if first is not None else first_key]
        if committee is None:
            return review(intake, initial, first, followup,
                          provider=ScriptedProvider(effective["config"]["provider"], supplied))
        if type(supplied) is not dict:
            raise VerifierError("simulation_member_reports")
        providers = {m["id"]: ScriptedProvider(m["provider"], supplied.get(m["id"]), sequence=m["stage"] == "region") for m in committee["members"]}
        result = review(intake, initial, first, followup, providers=providers)
        if supplied.keys() != {r["member_id"] for r in result["data"]["reviews"]}:
            raise VerifierError("simulation_unexpected_member_reports")
        for member in committee["members"]:
            if member["stage"] == "region" and len(supplied[member["id"]]) != sum(
                    r["member_id"] == member["id"] for r in result["data"]["reviews"]):
                raise VerifierError("simulation_unexpected_region_reports")
        return result
    intake = intake_snapshot(effective, candidate, simulation=True, accepted_results_context=accepted_results_context)
    initial = execute(intake, runner=ScriptedRunner(script["initial_results"]))
    first = judge(intake, initial)
    followup = final = None
    if first["data"]["decision"]["kind"] == "followup":
        followup = execute(intake, first, initial, runner=ScriptedRunner(script["followup_results"]))
        final = judge(intake, initial, first, followup)
    elif script["followup_results"] or script[final_key] is not None:
        raise VerifierError("simulation_unexpected_followup")
    result = dossier(intake, initial, first, followup, final)
    # Only an explicitly marked envelope, no Yukon-compatible top-level score.
    write_json(output, {"simulation_only": True, "publishable": False, "dossier": result})
    return result
