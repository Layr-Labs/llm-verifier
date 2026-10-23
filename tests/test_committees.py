"""Committee trust boundaries and protocols with an offline model transport."""

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier import pipeline
from llm_verifier.artifacts import implementation, intake_snapshot, verify_intake
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.external_evidence import POLICY
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier.scaffold import check
from llm_verifier.simulation import simulate
from tests.support import completion, example, reseal, scripted_model

PROFILE = {"profile": "builtin:math-committee-v1"}


@contextmanager
def committee(name="proof", judging=None, limits=None):
    def mutate(raw):
        raw["judging"] = copy.deepcopy(PROFILE if judging is None else judging)
        raw.setdefault("limits", {}).update(limits or {})
    with example(name, mutate=mutate) as value:
        yield value


def report(*, findings=None, requests=None, assessment="The supplied argument supports the original claim."):
    return {"assessment": assessment, "findings": findings or [], "requests": requests or []}


def finding(name="gap"):
    return {"id": name, "kind": "blocking", "reason": "The boundary case needs justification.",
            "evidence_refs": ["source:proof.md"]}


def response(outcome=None):
    value = {"finding_id": "proof/gap", "reason": "The original proof treats this case explicitly.",
             "evidence_refs": ["source:proof.md"]}
    if outcome:
        value["outcome"] = outcome
    return value


def sequence(decision, outcome=None):
    reviews = [report(findings=[finding()] if outcome else [])] + [report() for _ in range(3)]
    if outcome:
        reviews.append({"responses": [response()]})
    reviews.append({"decision": decision, "resolutions": [response(outcome)] if outcome else []})
    return reviews


def review(root, intake, responses=None):
    initial = pipeline.execute(intake)
    decision = read_json(root / "simulation.json")["initial_decision"]
    with scripted_model(sequence(decision) if responses is None else responses):
        first = pipeline.review(intake, initial)
    return initial, first


class JudgeConfiguration(unittest.TestCase):
    def test_hashsmash_role_mapping_and_inherited_provider_overrides(self):
        spec = {"profile": "builtin:hashsmash-v1", "overrides": {
            "lane_cryptanalysis": {"provider": {"model": "crypto-model", "reasoning_effort": "high"}}}}
        with committee(judging=spec) as (root, effective, intake):
            members = effective["judging"]["committee"]["members"]
            expected = [
                ("lane_evaluability", "evaluability-reviewer", "review"),
                ("lane_cryptanalysis", "cryptanalysis-reviewer", "review"),
                ("lane_cost", "cost-auditor", "review"),
                ("lane_experiments", "experiment-skeptic", "review"),
                ("lane_defender", "constructive-defender", "defense"),
                ("lane_adjudicator", "adjudicator", "adjudication"),
            ]
            self.assertEqual([(m["id"], m["persona"], m["stage"]) for m in members],
                             [(name, f"builtin:{persona}-v1", stage) for name, persona, stage in expected])
            for member in members:
                provider = copy.deepcopy(effective["config"]["provider"])
                if member["id"] == "lane_cryptanalysis":
                    provider.update(model="crypto-model", reasoning_effort="high")
                self.assertEqual(member["provider"], provider)
            self.assertEqual(effective["judging"]["files"], {})
            self.assertTrue(check(root)["valid"])
            verify_intake(intake)

    def test_builtin_defaults_and_legacy_bytes(self):
        with example() as (_, effective, _):
            self.assertNotIn("judging", effective)
            self.assertNotIn("total_review_tokens", effective["config"]["limits"])
            self.assertEqual(effective["config"]["limits"]["total_provider_requests"], 4)
        with committee() as (root, effective, intake):
            self.assertEqual(len(effective["judging"]["committee"]["members"]), 6)
            self.assertEqual(effective["config"]["limits"]["total_provider_requests"], 24)
            self.assertEqual(effective["judging"]["files"], {})
            self.assertTrue(check(root)["valid"])
            verify_intake(intake)
            self.assertIn("judges/personas/proof-reviewer-v1.md", implementation()["files"])

    def test_local_catalogs_profile_pointer_additions_and_overrides_are_snapshotted(self):
        with example() as (root, _, _):
            write_json(root / "judges/first.json", {"version": 1, "personas": [
                {"id": "local:domain-v1", "instructions_path": "judges/domain.md"}], "profiles": []})
            (root / "judges/domain.md").write_text("Audit the domain-specific hypotheses.")
            write_json(root / "judges/second.json", {"version": 1, "personas": [], "profiles": [
                {"id": "local:one-v1", "protocol": "single-v1", "members": [
                    {"id": "domain", "stage": "review", "persona": "local:domain-v1"}]}]})
            spec = {**PROFILE, "catalogs": ["judges/first.json", "judges/second.json"],
                    "add_members": [{"id": "domain", "stage": "review", "persona": "local:domain-v1"}],
                    "overrides": {"domain": {"provider": {"model": "domain-model", "reasoning_effort": "high"}},
                                  "proof": {"persona": "local:domain-v1"}}}
            write_json(root / "judges/committee.json", spec)
            raw = read_json(root / "verifier.json"); raw["judging"] = {"path": "judges/committee.json"}
            write_json(root / "verifier.json", raw)
            effective = load_config(root, "verifier.json", "submission")
            intake = intake_snapshot(effective, root / "submission")
            members = {m["id"]: m for m in effective["judging"]["committee"]["members"]}
            self.assertEqual(members["domain"]["provider"]["model"], "domain-model")
            self.assertEqual(members["proof"]["persona"], "local:domain-v1")
            self.assertEqual(len(effective["judging"]["files"]), 4)
            (root / "judges/domain.md").write_text("Changed after intake.")
            verify_intake(intake)
            self.assertNotEqual(digest(effective), digest(load_config(root, "verifier.json", "submission")))
            spec.update(profile="local:one-v1"); spec.pop("add_members"); spec.pop("overrides")
            write_json(root / "judges/committee.json", spec)
            self.assertEqual(load_config(root, "verifier.json", "submission")["judging"]["committee"]["protocol"], "single-v1")

    def test_inline_single_and_member_override(self):
        spec = {"protocol": "single-v1", "members": [{"id": "one", "stage": "review",
                "persona": "builtin:proof-reviewer-v1", "provider": {"model": "another-model"}}]}
        with committee(judging=spec) as (root, effective, intake):
            initial = pipeline.execute(intake)
            with scripted_model([read_json(root / "simulation.json")["initial_decision"]]):
                first = pipeline.review(intake, initial)
            result = pipeline.dossier(intake, initial, first)
            self.assertTrue(result["data"]["judgment"]["valid"])
            self.assertEqual(first["data"]["reviews"][0]["provenance"]["configured_model"], "another-model")
            self.assertEqual(len(first["data"]["reviews"]), 1)

    def test_unknown_duplicate_and_unsafe_configuration(self):
        cases = [
            {"profile": "builtin:missing-v1"},
            {**PROFILE, "catalogs": ["same.json", "same.json"]},
            {**PROFILE, "overrides": {"missing": {"persona": "builtin:proof-reviewer-v1"}}},
            {**PROFILE, "overrides": {"proof": {"provider": {"api_key": "forbidden"}}}},
            {**PROFILE, "overrides": {"proof": {"stage": "adjudication"}}},
            {**PROFILE, "add_members": [{"id": "proof", "stage": "review", "persona": "builtin:proof-reviewer-v1"}]},
            {**PROFILE, "add_members": [{"id": "extra", "stage": "defense", "persona": "builtin:proof-reviewer-v1"}]},
            {"protocol": "custom-code", "members": []},
            {"path": "../escape.json"},
            {"path": "judges.json", "profile": "builtin:single-v1"},
            {"protocol": "review-defense-adjudication-v1", "members": [
                {"id": "one", "stage": "review", "persona": "builtin:proof-reviewer-v1"}]},
        ]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(VerifierError):
                with committee(judging=spec):
                    pass

    def test_catalog_duplicates_namespace_and_protected_paths(self):
        for mode in ("duplicate", "namespace", "candidate", "symlink", "oversize", "recursive"):
            with self.subTest(mode=mode), example() as (root, _, _):
                persona = {"id": "local:custom-v1", "instructions": "Review the evidence."}
                raw = read_json(root / "verifier.json")
                raw["judging"] = {**PROFILE, "catalogs": ["personas.json"]}
                catalog = {"version": 1, "personas": [persona], "profiles": []}
                if mode == "duplicate": catalog["personas"].append(copy.deepcopy(persona))
                if mode == "namespace": persona["id"] = "builtin:custom-v1"
                if mode in ("candidate", "symlink", "oversize"):
                    persona.pop("instructions")
                    persona["instructions_path"] = "submission/proof.md" if mode == "candidate" else "persona.md"
                    if mode == "symlink": (root / "persona.md").symlink_to(root / "submission/proof.md")
                    if mode == "oversize": (root / "persona.md").write_text("x" * 65537)
                if mode == "recursive":
                    raw["judging"] = {"path": "personas.json"}; catalog = {"path": "personas.json"}
                write_json(root / "personas.json", catalog); write_json(root / "verifier.json", raw)
                with self.assertRaises(VerifierError): load_config(root, "verifier.json", "submission")

    def test_resolved_config_cannot_diverge_from_sources(self):
        with committee() as (root, effective, _):
            for mutate in (
                lambda e: e["judging"]["committee"]["members"][0].update(instructions="Forged"),
                lambda e: e["judging"]["committee"]["members"][0]["provider"].update(model="forged-model"),
                lambda e: e.pop("judging"),
                lambda e: e["config"].pop("judging"),
            ):
                changed = copy.deepcopy(effective); mutate(changed)
                with self.assertRaises(VerifierError): intake_snapshot(changed, root / "submission")

    def test_minimum_budget_checked_before_inference(self):
        for limits in ({"total_provider_requests": 4}, {"total_review_tokens": 32768}):
            with self.subTest(limits=limits), patch.object(HttpTransport, "request") as transport, self.assertRaisesRegex(VerifierError, "committee_minimum_budget"):
                with committee(limits=limits): pass
            transport.assert_not_called()


class CommitteeProtocol(unittest.TestCase):
    def test_hashsmash_finding_defense_and_adjudication(self):
        with committee(judging={"profile": "builtin:hashsmash-v1"}) as (root, _, intake):
            decision = read_json(root / "simulation.json")["initial_decision"]
            defense = {**response(), "finding_id": "lane_cryptanalysis/gap"}
            resolution = {**defense, "outcome": "dismissed"}
            replies = [report(), report(findings=[finding()]), report(), report(),
                       {"responses": [defense]}, {"decision": decision, "resolutions": [resolution]}]
            initial, first = review(root, intake, replies)
            self.assertEqual([r["member_id"] for r in first["data"]["reviews"]],
                             ["lane_evaluability", "lane_cryptanalysis", "lane_cost",
                              "lane_experiments", "lane_defender", "lane_adjudicator"])
            result = pipeline.dossier(intake, initial, first)
            self.assertTrue(result["data"]["judgment"]["valid"])
            self.assertEqual(first["data"]["provenance"]["attempts"], 6)

    def test_independent_reviews_and_one_final_exportable_judgment(self):
        with committee() as (root, _, intake):
            initial = pipeline.execute(intake)
            decision = read_json(root / "simulation.json")["initial_decision"]
            bodies = []
            replies = iter(sequence(decision))
            def request(body, *args):
                bodies.append(json.loads(body)); return completion(next(replies))
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=request):
                first = pipeline.review(intake, initial)
            for body in bodies:
                self.assertEqual(body["messages"][0]["content"].count(POLICY), 1)
                self.assertNotIn(report()["assessment"], body["messages"][0]["content"])
            for body in bodies[:4]: self.assertNotIn('"assessment":', body["messages"][1]["content"])
            self.assertIn('"assessment":', bodies[4]["messages"][1]["content"])
            result = pipeline.finalize(intake, initial, first, root / "score.json", root / "dossier.json", results_path=root / "accepted.json")
            self.assertTrue(read_json(root / "score.json")["valid"])
            self.assertEqual(first["data"]["provenance"]["attempts"], 5)
            self.assertEqual(first["data"]["provenance"]["allotted_completion_tokens"], 5 * 8192)
            self.assertEqual(pipeline.export_results(result), read_json(root / "accepted.json"))

    def test_defense_required_and_objections_resolved_exactly(self):
        for outcome, valid in (("dismissed", True), ("confirmed", False), ("unresolved", False)):
            with self.subTest(outcome=outcome), committee() as (root, _, intake):
                decision = read_json(root / "simulation.json")["initial_decision"]
                decision["judgment"]["valid"] = valid
                initial, first = review(root, intake, sequence(decision, outcome))
                self.assertEqual(len(first["data"]["reviews"]), 6)
                self.assertEqual(pipeline.dossier(intake, initial, first)["data"]["judgment"]["valid"], valid)
                self.assertEqual(first["data"]["reviews"][4]["stage"], "defense")

    def test_cannot_accept_confirmed_or_unresolved_findings(self):
        for outcome in ("confirmed", "unresolved"):
            with self.subTest(outcome=outcome), committee() as (root, _, intake):
                decision = read_json(root / "simulation.json")["initial_decision"]
                with self.assertRaisesRegex(InfrastructureError, "committee_unresolved_acceptance"):
                    review(root, intake, sequence(decision, outcome))

    def test_invalid_or_incomplete_reports_fail_as_infrastructure(self):
        for mode in ("missing_defense", "extra_resolution", "duplicate_finding", "unknown_ref", "empty_dismissal", "wrong_shape", "missing_resolution"):
            with self.subTest(mode=mode), committee() as (root, _, intake):
                values = sequence(read_json(root / "simulation.json")["initial_decision"], "dismissed")
                if mode == "missing_defense": values[4]["responses"] = []
                if mode == "extra_resolution": values[5]["resolutions"].append(response("dismissed"))
                if mode == "duplicate_finding": values[0]["findings"].append(finding())
                if mode == "unknown_ref": values[0]["findings"][0]["evidence_refs"] = ["source:missing.md"]
                if mode == "empty_dismissal": values[5]["resolutions"][0]["evidence_refs"] = []
                if mode == "wrong_shape": values[0] = {"valid": True}
                if mode == "missing_resolution": values[5]["resolutions"] = []
                with self.assertRaises(InfrastructureError): review(root, intake, values)

    def test_revalidation_detects_missing_reordered_forged_and_extra_stages(self):
        with committee() as (root, _, intake):
            initial, first = review(root, intake, sequence(read_json(root / "simulation.json")["initial_decision"], "dismissed"))
            mutations = [
                lambda d: d["reviews"].pop(4),
                lambda d: d["reviews"].reverse(),
                lambda d: d["reviews"].append(copy.deepcopy(d["reviews"][-1])),
                lambda d: d["reviews"][0]["provenance"].update(prompt_sha256="0" * 64),
                lambda d: d["reviews"][0]["provenance"].update(configured_model="forged-model"),
                lambda d: d["reviews"][0]["provenance"].update(endpoint="https://invalid.example"),
                lambda d: d["reviews"][0].update(member_id="forged"),
                lambda d: d["reviews"][0]["report"].update(assessment="Changed stage evidence"),
                lambda d: d["reviews"][-1]["report"]["resolutions"].clear(),
                lambda d: d["decision"]["judgment"].update(valid=False),
                lambda d: d["provenance"].update(attempts=True),
                lambda d: d["provenance"].update(allotted_completion_tokens=0),
            ]
            for mutate in mutations:
                changed = copy.deepcopy(first); mutate(changed["data"])
                with self.assertRaises(VerifierError): pipeline.dossier(intake, initial, reseal(changed))
            other = intake_snapshot(intake["data"]["effective"], root / "submission")
            with self.assertRaises(VerifierError): pipeline.verify_initial_review(other, initial, first)

    def test_missing_review_clears_stale_outputs(self):
        with committee() as (root, _, intake):
            initial, first = review(root, intake)
            first["data"]["reviews"].pop(); first = reseal(first)
            for name in ("score.json", "dossier.json", "accepted.json", "accepted-results-receipt.json"):
                write_json(root / name, {"stale": True})
            with self.assertRaises(VerifierError):
                pipeline.finalize(intake, initial, first, root / "score.json", root / "dossier.json", results_path=root / "accepted.json")
            self.assertFalse((root / "score.json").exists())
            self.assertFalse((root / "accepted.json").exists())
            self.assertFalse((root / "accepted-results-receipt.json").exists())

    def test_followup_requests_deduplicate_and_all_reviewers_see_final_evidence(self):
        with committee("computation") as (root, _, intake):
            script = read_json(root / "simulation.json")
            request = script["initial_decision"]["requests"][0]
            duplicate = {**request, "reason": "Same input requested independently."}
            values = [report(requests=[request]), report(requests=[duplicate]), report(), report()]
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"checked_n":25}', b'')) as runner:
                initial = pipeline.execute(intake)
                with scripted_model(values): first = pipeline.review(intake, initial)
                self.assertEqual(first["data"]["decision"]["requests"], [request])
                self.assertEqual(len(first["data"]["reviews"]), 4)
                extra = pipeline.execute(intake, first, initial)
                with scripted_model(sequence(script["final_decision"])): final = pipeline.review(intake, initial, first, extra)
            result = pipeline.dossier(intake, initial, first, extra, final)
            self.assertEqual(result["data"]["judgment"]["scores"], [25])
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(len(final["data"]["reviews"]), 5)
            self.assertEqual(len(result["data"]["phases"]), 5)

    def test_request_overflow_is_not_silently_discarded(self):
        with committee("computation", limits={"max_followups": 1}) as (root, _, intake):
            request = read_json(root / "simulation.json")["initial_decision"]["requests"][0]
            other = {**request, "inputs": {"n": 26}}
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{}', b'')):
                initial = pipeline.execute(intake)
            with scripted_model([report(requests=[request]), report(requests=[other]), report(), report()]), self.assertRaisesRegex(InfrastructureError, "run_count_budget"):
                pipeline.review(intake, initial)

    def test_final_review_cannot_request_more_experiments(self):
        with committee("computation") as (root, _, intake):
            request = read_json(root / "simulation.json")["initial_decision"]["requests"][0]
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{}', b'')):
                initial = pipeline.execute(intake)
                with scripted_model([report(requests=[request])] + [report()] * 3): first = pipeline.review(intake, initial)
                extra = pipeline.execute(intake, first, initial)
            with scripted_model([report(requests=[request])]), self.assertRaisesRegex(InfrastructureError, "committee_final_requests"):
                pipeline.review(intake, initial, first, extra)

    def test_request_and_reserved_token_budgets_include_retries(self):
        for limits in ({"total_provider_requests": 5}, {"total_review_tokens": 5 * 8192}):
            with self.subTest(limits=limits), committee(limits=limits) as (root, _, intake):
                initial = pipeline.execute(intake)
                replies = iter([HttpResponse(503, b"")] + [completion(report())] * 4)
                with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=lambda *args: next(replies)) as calls, patch("llm_verifier.provider.time.sleep"), self.assertRaisesRegex(InfrastructureError, "committee_review_budget_exhausted"):
                    pipeline.review(intake, initial)
                self.assertEqual(calls.call_count, 5)

    def test_whole_evaluation_budget_spans_followup(self):
        with committee("computation", limits={"total_provider_requests": 8}) as (root, _, intake):
            request = read_json(root / "simulation.json")["initial_decision"]["requests"][0]
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{}', b'')):
                initial = pipeline.execute(intake)
                with scripted_model([report(requests=[request])] + [report()] * 3): first = pipeline.review(intake, initial)
                extra = pipeline.execute(intake, first, initial)
            with scripted_model([report()] * 4), self.assertRaisesRegex(InfrastructureError, "committee_review_budget_exhausted"):
                pipeline.review(intake, initial, first, extra)

    def test_simulation_is_explicit_and_cannot_publish(self):
        with committee() as (root, effective, _):
            values = sequence(read_json(root / "simulation.json")["initial_decision"], "dismissed")
            members = ["proof", "skeptic", "cost", "evidence", "defender", "adjudicator"]
            script = {"initial_results": [], "initial_reviews": dict(zip(members, values)), "followup_results": [], "final_reviews": None}
            result = simulate(effective, root / "submission", script, root / "simulation-out.json")
            self.assertTrue(result["simulation"])
            self.assertFalse(read_json(root / "simulation-out.json")["publishable"])
            with self.assertRaisesRegex(VerifierError, "simulation_cannot_publish"):
                pipeline.finalize(*result["data"]["phases"], root / "score.json", root / "dossier.json")
            self.assertFalse((root / "score.json").exists())

    def test_phase_wall_deadline_stops_before_next_member_call(self):
        from llm_verifier.simulation import ScriptedProvider
        with committee(limits={"total_review_seconds": 2}) as (root, effective, intake):
            initial = pipeline.execute(intake)
            providers = {m["id"]: ScriptedProvider(m["provider"], report())
                         for m in effective["judging"]["committee"]["members"]}
            with patch("llm_verifier.committee.time.monotonic", side_effect=[0, 1, 3]), self.assertRaisesRegex(InfrastructureError, "committee_review_budget_exhausted"):
                pipeline.review(intake, initial, providers=providers)

    def test_prior_results_and_unavailable_support_keep_atomic_lineage(self):
        from tests.test_prior_results import upstream
        from tests.test_unavailable_references import unavailable_snapshot, intake_for
        with upstream() as (_, _, _, bundle), committee() as (root, _, _):
            intake = intake_for(root, unavailable_snapshot(bundle))
            initial = pipeline.execute(intake)
            decision = read_json(root / "simulation.json")["initial_decision"]
            decision["judgment"]["evidence_refs"].append("prior:source-0")
            values = sequence(decision)
            values[0]["findings"] = [{"id": "prior", "kind": "observation",
                                      "reason": "The available original claim applies.",
                                      "evidence_refs": ["prior:source-0"]}]
            with scripted_model(values): first = pipeline.review(intake, initial)
            result = pipeline.finalize(intake, initial, first, root / "score.json", root / "dossier.json", results_path=root / "accepted.json")
            exported = pipeline.export_results(result)
            self.assertEqual(exported["ancestors"], [{"submission_id": "source-0"}])
            self.assertEqual(exported["source"]["manifest"], intake["data"]["files"]["submission.json"])
            self.assertEqual(read_json(root / "accepted-results-receipt.json")["snapshot_id"], "snapshot")
            values[0]["findings"][0]["evidence_refs"] = ["prior:unavailable-A"]
            with scripted_model(values), self.assertRaisesRegex(InfrastructureError, "committee_evidence_refs"):
                pipeline.review(intake, initial)

    def test_provider_format_override_handles_optional_inputs(self):
        with committee("computation", judging={**PROFILE, "overrides": {
                name: {"provider": {"response_format": "json_object"}}
                for name in ("proof", "skeptic", "cost", "evidence")}}) as (root, effective, _):
            manifest = read_json(root / "submission/submission.json")
            manifest["experiments"][0]["input_schema"]["properties"]["optional"] = {"type": "integer", "minimum": 0, "maximum": 1}
            write_json(root / "submission/submission.json", manifest)
            intake = intake_snapshot(effective, root / "submission")
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"checked_n":10}', b'')):
                initial = pipeline.execute(intake)
            decision = read_json(root / "simulation.json")["final_decision"]
            decision["judgment"]["evidence_refs"] = ["source:proof.md", "run:initial:0"]
            decision["judgment"]["scores"] = [10]
            with scripted_model(sequence(decision)): first = pipeline.review(intake, initial)
            self.assertEqual(pipeline.dossier(intake, initial, first)["data"]["judgment"]["scores"], [10])

    def test_preflight_checks_each_requesting_members_format(self):
        from llm_verifier.artifacts import preflight_snapshot
        spec = {**PROFILE, "overrides": {name: {"provider": {"response_format": "json_object"}}
                for name in ("proof", "skeptic", "cost", "evidence")}}
        with committee("computation", judging=spec) as (root, _, _):
            manifest = read_json(root / "submission/submission.json")
            manifest["experiments"][0]["input_schema"]["properties"]["optional"] = {
                "type": "integer", "minimum": 0, "maximum": 1}
            write_json(root / "submission/submission.json", manifest)
            self.assertTrue(check(root)["valid"])
            raw = read_json(root / "verifier.json")
            raw["judging"]["overrides"]["cost"]["provider"]["response_format"] = "json_schema"
            write_json(root / "verifier.json", raw)
            effective = load_config(root, "verifier.json", "submission")
            with patch.object(HttpTransport, "request") as transport:
                for validate in (lambda: check(root), lambda: preflight_snapshot(effective, root / "submission"),
                                 lambda: intake_snapshot(effective, root / "submission")):
                    with self.assertRaisesRegex(VerifierError, "strict_provider_optional_inputs_require_json_object"):
                        validate()
                transport.assert_not_called()

    def test_preprocessing_policy_and_protected_rules_reach_every_stage(self):
        from llm_verifier.evidence import policy_for
        for policy in ("atomic-claims-v1", "exploratory-v1"):
            with self.subTest(policy=policy), example("scored-proof") as (root, _, _):
                raw = read_json(root / "verifier.json")
                raw.update(judging={"profile": "builtin:hashsmash-v1"}, acceptance_policy=policy,
                           manifest_preprocessor="curvesmash-v1", rules_files=["extra-rules.md"])
                write_json(root / "verifier.json", raw)
                (root / "extra-rules.md").write_text("Apply the protected extra resource convention.")
                manifest = read_json(root / "submission/submission.json")
                manifest["claims"][0]["score"] = {"numerator": "2", "denominator": "4"}
                write_json(root / "submission/submission.json", manifest)
                effective = load_config(root, "verifier.json", "submission")
                intake = intake_snapshot(effective, root / "submission")
                original = (root / "submission/submission.json").read_text()
                decision = read_json(root / "simulation.json")["initial_decision"]
                defense = {**response(), "finding_id": "lane_cryptanalysis/gap"}
                replies = iter([report(), report(findings=[finding()]), report(), report(),
                                {"responses": [defense]}, {"decision": decision, "resolutions": [
                                    {**defense, "outcome": "dismissed"}]}])
                bodies = []
                def request(body, *args):
                    bodies.append(json.loads(body))
                    return completion(next(replies))
                initial = pipeline.execute(intake)
                with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=request):
                    first = pipeline.review(intake, initial)
                self.assertEqual(len(bodies), 6)
                for body in bodies:
                    system = body["messages"][0]["content"]
                    self.assertIn(policy_for(effective["config"], committee=True), system)
                    self.assertIn("Apply the protected extra resource convention.", system)
                    self.assertNotIn("This is one reviewer, not independent", system)
                    if policy == "exploratory-v1":
                        self.assertNotIn("ALL original submission.claims are established as written", system)
                    evidence = json.loads(body["messages"][1]["content"].split("\n", 1)[1].split(
                        "\nPrior committee reports", 1)[0])
                    self.assertEqual(evidence["original_submission_manifest"]["text"], original)
                    self.assertEqual(evidence["manifest_preprocessing"], intake["data"]["preprocessing"])
                result = pipeline.dossier(intake, initial, first)
                self.assertEqual(result["data"]["judgment"]["scores"], [0.5])
                exported = pipeline.export_results(result)
                self.assertEqual(exported["policy_id"], policy)
                self.assertEqual(exported["source"]["manifest"]["text"], original)

    def test_workflow_score_cannot_overwrite_judge_sources(self):
        from types import SimpleNamespace
        from llm_verifier.workflow import prepare
        from tests.support import REPO
        with example() as (root, _, _):
            write_json(root / "committee.json", PROFILE)
            raw = read_json(root / "verifier.json"); raw["judging"] = {"path": "committee.json"}
            raw["rules_files"] = ["extra-rules.md"]
            raw["submission_selector"] = {"path": "selected.txt", "directory": "submission"}
            (root / "extra-rules.md").write_text("Protected extra rules.")
            (root / "selected.txt").write_text("submission")
            write_json(root / "verifier.json", raw)
            args = SimpleNamespace(checkout=root, verifier_checkout=REPO, candidate_revision="a" * 40,
                                   verifier_revision="b" * 40, root_dir=".", submission="submission",
                                   score_path="committee.json", config="verifier.json", evaluation=None,
                                   out=root / "intake.json", run_id="1:1", accepted_results_context=None)
            for path in ("committee.json", "extra-rules.md", "selected.txt"):
                args.score_path = path
                with self.subTest(path=path), patch("llm_verifier.workflow.checkout_revision"), self.assertRaisesRegex(VerifierError, "score_path_overwrites_config"):
                    prepare(args)
            self.assertEqual(read_json(root / "committee.json"), PROFILE)

    def test_bound_scores_and_required_checks_still_gate_committee_decisions(self):
        with committee("scored-proof") as (root, _, intake):
            initial, first = review(root, intake)
            result = pipeline.dossier(intake, initial, first)
            self.assertIsNotNone(result["data"]["score_records"])
        with committee("collision") as (root, _, intake):
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"passed":false}', b'')):
                initial = pipeline.execute(intake)
            decision = read_json(root / "simulation.json")["initial_decision"]
            with scripted_model(sequence(decision)), self.assertRaises(VerifierError):
                pipeline.review(intake, initial)


if __name__ == "__main__":
    unittest.main()
