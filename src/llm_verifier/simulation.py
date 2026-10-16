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

    def __init__(self, config, decision):
        self.config, self.decision = config, decision

    def review(self, messages, schema, seconds, requests):
        return self.decision, {"provider": "simulation", "endpoint": "none", "configured_model": self.config["model"],
            "returned_model": "simulation-scripted", "request_id": None, "usage": {},
            "prompt_sha256": digest(messages), "schema_sha256": digest(schema),
            "provider_config_sha256": digest(self.config), "attempts": 1, "elapsed_seconds": 0}


def simulate(effective, candidate, script, output):
    keys(script, {"initial_results", "initial_decision", "followup_results", "final_decision"})
    intake = intake_snapshot(effective, candidate, simulation=True)
    initial = execute(intake, runner=ScriptedRunner(script["initial_results"]))
    first = review(intake, initial, provider=ScriptedProvider(effective["config"]["provider"], script["initial_decision"]))
    followup = final = None
    if first["data"]["decision"]["kind"] == "followup":
        followup = execute(intake, first, initial, runner=ScriptedRunner(script["followup_results"]))
        final = review(intake, initial, first, followup, provider=ScriptedProvider(effective["config"]["provider"], script["final_decision"]))
    elif script["followup_results"] or script["final_decision"] is not None:
        raise VerifierError("simulation_unexpected_followup")
    result = dossier(intake, initial, first, followup, final)
    # Only an explicitly marked envelope, no Yukon-compatible top-level score.
    write_json(output, {"simulation_only": True, "publishable": False, "dossier": result})
    return result
