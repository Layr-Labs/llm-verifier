"""Region planning, blindness, budgets and replay using only mocked model HTTP."""

import copy
import json
import unittest
from unittest.mock import patch

from llm_verifier import pipeline, regions, structured_claims
from llm_verifier.artifacts import intake_snapshot
from llm_verifier.config import load_config
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier.scaffold import check
from llm_verifier.simulation import simulate
from tests.support import completion, example, reseal, scripted_model
from tests.test_committees import committee, finding, report

PROFILE = {"profile": "builtin:math-decomposed-v1"}


def plan_for(intake, split=False):
    result = []
    for path, source in sorted(intake["data"]["files"].items()):
        count = len(regions.lines(source))
        spans = [(1, count)]
        if split and path == "proof.md" and count > 1:
            spans = [(1, 1), (2, count)]
        for start, end in spans:
            result.append({"id": f"r{len(result) + 1}", "path": path, "start_line": start,
                           "end_line": end, "purpose": "Inspect this part in the context of the original claims."})
    return {"regions": result}


def region_report(region, **kwargs):
    return {**report(**kwargs), "coverage": {"region_id": region["id"],
            "start_line": region["start_line"], "end_line": region["end_line"], "complete": True}}


def outputs(plan, decision, *, final=False, outcome=None):
    result = ([] if final else [plan]) + [report()]
    for index, region in enumerate(plan["regions"]):
        result.append(region_report(region, findings=[finding()] if index == 0 and outcome else []))
    resolutions = []
    if outcome:
        response = {"finding_id": "region/" + plan["regions"][0]["id"] + "/gap",
                    "reason": "The original evidence addresses the concern.", "evidence_refs": ["source:proof.md"]}
        result.append({"responses": [response]})
        resolutions = [{**response, "outcome": outcome}]
    result.append({"decision": decision, "resolutions": resolutions})
    return result


class RegionReview(unittest.TestCase):
    def test_builtin_plan_and_coverage_survive_full_replay(self):
        with committee(judging=PROFILE) as (root, effective, intake):
            self.assertTrue(check(root)["valid"])
            self.assertEqual(effective["judging"]["committee"]["max_regions"], 8)
            plan = plan_for(intake, split=True)
            initial = pipeline.execute(intake)
            with scripted_model(outputs(plan, read_json(root / "simulation.json")["initial_decision"])):
                first = pipeline.review(intake, initial)
            data = first["data"]
            self.assertEqual(data["region_plan"], regions.freeze_plan(plan, intake["data"]["files"], 8))
            self.assertEqual(len(data["region_coverage"]), len(plan["regions"]))
            self.assertTrue(all(c["complete"] for c in data["region_coverage"]))
            self.assertEqual(data["provenance"]["attempts"], len(plan["regions"]) + 3)
            result = pipeline.dossier(intake, initial, first)
            self.assertTrue(result["data"]["judgment"]["valid"])
            (root / "submission/proof.md").write_text("Changed after intake")
            self.assertTrue(pipeline.dossier(intake, initial, first)["data"]["judgment"]["valid"])

    def test_plans_reject_missing_duplicated_gapped_overlapping_and_unbound_regions(self):
        with committee(judging=PROFILE) as (_, _, intake):
            initial = pipeline.execute(intake)
            original = plan_for(intake, split=True)
            mutations = [
                lambda p: p["regions"].pop(),
                lambda p: p.update(regions=[]),
                lambda p: p["regions"].append(copy.deepcopy(p["regions"][0])),
                lambda p: p["regions"][0].update(path="../proof.md"),
                lambda p: p["regions"][0].update(path="rules.md"),
                lambda p: p["regions"][0].update(start_line=True),
                lambda p: p["regions"][0].update(end_line=999999),
                lambda p: p["regions"][1].update(start_line=1),
                lambda p: p["regions"][1].update(start_line=3),
                lambda p: p["regions"][1].update(id=p["regions"][0]["id"]),
                lambda p: p.update(decision="accept"),
            ]
            for mutate in mutations:
                plan = copy.deepcopy(original); mutate(plan)
                with self.subTest(plan=plan), scripted_model([plan]), self.assertRaises(InfrastructureError):
                    pipeline.review(intake, initial)

    def test_plan_cap_and_source_count_are_enforced_before_review(self):
        with committee(judging={**PROFILE, "max_regions": 2}) as (_, _, intake):
            initial = pipeline.execute(intake)
            with scripted_model([plan_for(intake, split=True)]), self.assertRaisesRegex(InfrastructureError, "region_plan_count"):
                pipeline.review(intake, initial)
        with patch.object(HttpTransport, "request") as http:
            with self.assertRaisesRegex(VerifierError, "region_source_count"), committee(judging={**PROFILE, "max_regions": 1}):
                pass
            http.assert_not_called()

    def test_empty_files_and_line_endings_have_unambiguous_coverage(self):
        files = {"empty": {"text": "", "sha256": "a" * 64}, "text": {"text": "a\r\nb\r\n", "sha256": "b" * 64}}
        plan = {"regions": [{"id": "empty", "path": "empty", "start_line": 1, "end_line": 1, "purpose": "empty"},
                            {"id": "text", "path": "text", "start_line": 1, "end_line": 2, "purpose": "text"}]}
        frozen = regions.freeze_plan(plan, files, 2)
        self.assertEqual(regions.assignment(plan["regions"][1], frozen, files)["numbered_lines"], [[1, "a"], [2, "b"]])
        self.assertEqual(frozen["sources"]["empty"]["line_count"], 1)

    def test_incomplete_or_wrong_coverage_stops_before_adjudication(self):
        with committee(judging=PROFILE) as (_, _, intake):
            initial = pipeline.execute(intake)
            plan = plan_for(intake)
            for mutation in (lambda c: c.update(complete=False), lambda c: c.update(complete=1),
                             lambda c: c.update(region_id="other"), lambda c: c.update(end_line=1),
                             lambda c: c.update(start_line=True)):
                response = region_report(plan["regions"][0]); mutation(response["coverage"])
                with self.subTest(response=response), scripted_model([plan, report(), response]), self.assertRaises(InfrastructureError):
                    pipeline.review(intake, initial)

    def test_workers_are_blind_with_complete_original_evidence_and_no_tools(self):
        with committee(judging=PROFILE) as (root, _, intake):
            initial = pipeline.execute(intake)
            plan = plan_for(intake, split=True)
            values = outputs(plan, read_json(root / "simulation.json")["initial_decision"])
            values[1]["assessment"] = "GLOBAL_PRIVATE_MARKER"
            values[2]["assessment"] = "REGION_PRIVATE_MARKER"
            bodies = []
            def request(body, *args):
                bodies.append(json.loads(body)); return completion(values[len(bodies) - 1])
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=request):
                pipeline.review(intake, initial)
            self.assertIn("numbered_sources", bodies[0]["messages"][1]["content"])
            for body in bodies:
                self.assertNotIn("tools", body)
                self.assertIn("There is no web/PDF retrieval.", body["messages"][0]["content"])
            for body in bodies[1:-1]:
                user = body["messages"][1]["content"]
                self.assertNotIn("GLOBAL_PRIVATE_MARKER", user)
                self.assertNotIn("REGION_PRIVATE_MARKER", user)
                evidence = json.loads(user.split("\n", 1)[1].split("\nPrior committee reports", 1)[0])
                self.assertEqual(evidence["files"]["proof.md"], intake["data"]["files"]["proof.md"])
            self.assertIn("GLOBAL_PRIVATE_MARKER", bodies[-1]["messages"][1]["content"])
            self.assertIn("REGION_PRIVATE_MARKER", bodies[-1]["messages"][1]["content"])

    def test_region_findings_keep_defense_and_atomic_acceptance_rules(self):
        for outcome in ("dismissed", "confirmed", "unresolved"):
            with self.subTest(outcome=outcome), committee(judging=PROFILE) as (root, _, intake):
                initial = pipeline.execute(intake)
                plan = plan_for(intake)
                decision = read_json(root / "simulation.json")["initial_decision"]
                decision["judgment"]["valid"] = outcome == "dismissed"
                with scripted_model(outputs(plan, decision, outcome=outcome)):
                    first = pipeline.review(intake, initial)
                self.assertEqual(pipeline.dossier(intake, initial, first)["data"]["judgment"]["valid"], outcome == "dismissed")
                if outcome != "dismissed":
                    decision["judgment"]["valid"] = True
                    with scripted_model(outputs(plan, decision, outcome=outcome)), self.assertRaisesRegex(InfrastructureError, "committee_unresolved_acceptance"):
                        pipeline.review(intake, initial)

    def test_replay_rejects_reordered_missing_duplicated_or_changed_plan_and_coverage(self):
        with committee(judging=PROFILE) as (root, _, intake):
            initial = pipeline.execute(intake); plan = plan_for(intake, split=True)
            with scripted_model(outputs(plan, read_json(root / "simulation.json")["initial_decision"])):
                first = pipeline.review(intake, initial)
            mutations = [
                lambda d: d["reviews"].pop(2),
                lambda d: d["reviews"].insert(3, copy.deepcopy(d["reviews"][2])),
                lambda d: d["reviews"][2].update(region_id="other"),
                lambda d: d["reviews"][2]["report"]["coverage"].update(complete=False),
                lambda d: d["reviews"][0]["report"]["regions"][0].update(purpose="Altered purpose"),
                lambda d: d["region_plan"]["regions"].reverse(),
                lambda d: d["region_plan"]["sources"]["proof.md"].update(sha256="0" * 64),
                lambda d: d["region_coverage"].pop(),
                lambda d: d["region_coverage"][0].update(source_sha256="0" * 64),
                lambda d: d["provenance"].update(allotted_completion_tokens=0),
            ]
            for mutate in mutations:
                value = copy.deepcopy(first); mutate(value["data"])
                with self.subTest(mutation=mutate), self.assertRaises(VerifierError):
                    pipeline.dossier(intake, initial, reseal(value))

    def test_followup_reuses_plan_and_repeats_all_region_reviews(self):
        with committee("computation", judging=PROFILE) as (root, _, intake):
            script = read_json(root / "simulation.json"); plan = plan_for(intake, split=True)
            request = script["initial_decision"]["requests"][0]
            initial_values = [plan, report(requests=[request])]
            initial_values += [region_report(r, requests=[{**request, "reason": "Independent duplicate"}]) for r in plan["regions"]]
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"checked_n":25}', b'')):
                initial = pipeline.execute(intake)
                with scripted_model(initial_values): first = pipeline.review(intake, initial)
                self.assertEqual(first["data"]["decision"]["requests"], [request])
                extra = pipeline.execute(intake, first, initial)
                with scripted_model(outputs(plan, script["final_decision"], final=True)):
                    final = pipeline.review(intake, initial, first, extra)
            self.assertEqual(first["data"]["region_plan"], final["data"]["region_plan"])
            self.assertNotIn("planning", [r["stage"] for r in final["data"]["reviews"]])
            self.assertEqual(len(final["data"]["region_coverage"]), len(plan["regions"]))
            self.assertTrue(pipeline.dossier(intake, initial, first, extra, final)["data"]["judgment"]["valid"])
            changed = copy.deepcopy(final); changed["data"]["region_plan"]["regions"][0]["purpose"] = "New plan"
            with self.assertRaises(VerifierError): pipeline.dossier(intake, initial, first, extra, reseal(changed))

    def test_minimum_budget_reserves_all_regions_and_member_overrides(self):
        spec = {**PROFILE, "max_regions": 2, "overrides": {"region": {"provider": {"max_completion_tokens": 16384}}}}
        floor = 4 * 8192 + 2 * 16384  # planner, global, defender, adjudicator, two regions
        for limits in ({"total_provider_requests": 5}, {"total_review_tokens": floor - 1}):
            with self.subTest(limits=limits), patch.object(HttpTransport, "request") as http:
                with self.assertRaisesRegex(VerifierError, "committee_minimum_budget"), committee(judging=spec, limits=limits): pass
                http.assert_not_called()
        with committee(judging=spec, limits={"total_provider_requests": 6, "total_review_tokens": floor}) as (root, _, intake):
            initial = pipeline.execute(intake); plan = plan_for(intake)
            with scripted_model(outputs(plan, read_json(root / "simulation.json")["initial_decision"], outcome="dismissed")):
                first = pipeline.review(intake, initial)
            self.assertEqual(first["data"]["provenance"]["attempts"], 6)
            self.assertEqual(first["data"]["provenance"]["allotted_completion_tokens"], floor)

    def test_retries_exhaust_the_shared_budget_without_inventing_a_verdict(self):
        with committee(judging={**PROFILE, "max_regions": 2}, limits={"total_provider_requests": 6}) as (root, _, intake):
            initial = pipeline.execute(intake); plan = plan_for(intake)
            values = outputs(plan, read_json(root / "simulation.json")["initial_decision"], outcome="dismissed")
            replies = iter([HttpResponse(503, b"")] + [completion(v) for v in values[:-1]])
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=lambda *args: next(replies)) as http, patch("llm_verifier.provider.time.sleep"):
                with self.assertRaisesRegex(InfrastructureError, "committee_review_budget_exhausted"):
                    pipeline.review(intake, initial)
            self.assertEqual(http.call_count, 6)

    def test_local_profile_and_added_specialists_retain_region_contract(self):
        with committee(judging=PROFILE) as (root, effective, _):
            source = effective["judging"]["committee"]
            members = [{k: m[k] for k in ("id", "persona", "stage")} for m in source["members"]]
            write_json(root / "judges.json", {"version": 1, "personas": [], "profiles": [
                {"id": "local:regional-v1", "protocol": regions.PROTOCOL, "max_regions": 3, "members": members}]})
            raw = read_json(root / "verifier.json")
            raw["judging"] = {"profile": "local:regional-v1", "catalogs": ["judges.json"], "max_regions": 2,
                              "add_members": [{"id": "skeptic", "persona": "builtin:counterexample-skeptic-v1", "stage": "review"}]}
            write_json(root / "verifier.json", raw)
            resolved = load_config(root, "verifier.json", "submission")
            self.assertEqual(resolved["judging"]["committee"]["max_regions"], 2)
            self.assertEqual(len(resolved["judging"]["committee"]["members"]), 6)
            self.assertTrue(check(root)["valid"])
            raw["judging"] = {"path": "committee.json"}
            write_json(root / "committee.json", {"protocol": regions.PROTOCOL, "members": members, "max_regions": 2})
            write_json(root / "verifier.json", raw)
            self.assertTrue(check(root)["valid"])

    def test_invalid_stages_limits_and_wrong_protocol_options_are_rejected(self):
        invalid = [{**PROFILE, "max_regions": x} for x in (0, 17, True, "8")]
        invalid += [{"profile": "builtin:single-v1", "max_regions": 2},
                    {"profile": "builtin:math-committee-v1", "max_regions": 2}]
        with committee(judging=PROFILE) as (_, effective, _):
            members = [{k: m[k] for k in ("id", "persona", "stage")} for m in effective["judging"]["committee"]["members"]]
        for stage in ("planning", "region", "review", "defense", "adjudication"):
            invalid.append({"protocol": regions.PROTOCOL, "members": [m for m in members if m["stage"] != stage]})
        invalid.append({"protocol": "review-defense-adjudication-v1", "members": members})
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(VerifierError), committee(judging=spec): pass

    def test_simulation_uses_ordered_region_reports_and_rejects_extras(self):
        with committee(judging=PROFILE) as (root, effective, intake):
            plan = plan_for(intake)
            decision = read_json(root / "simulation.json")["initial_decision"]
            values = {"planner": plan, "proof": report(), "region": [region_report(r) for r in plan["regions"]],
                      "adjudicator": {"decision": decision, "resolutions": []}}
            script = {"initial_results": [], "initial_reviews": values, "followup_results": [], "final_reviews": None}
            result = simulate(effective, root / "submission", script, root / "out.json")
            self.assertTrue(result["simulation"])
            self.assertFalse(read_json(root / "out.json")["publishable"])
            for change in (lambda v: v["region"].pop(), lambda v: v["region"].append(copy.deepcopy(v["region"][0])),
                           lambda v: v.update(region=v["region"][0])):
                changed = copy.deepcopy(script); change(changed["initial_reviews"])
                with self.assertRaises(VerifierError): simulate(effective, root / "submission", changed, root / "bad.json")

    def test_structured_claim_acknowledgment_is_enforced_on_committee_paths(self):
        from tests.test_structured_claims import candidate, verdict
        for profile in (PROFILE, {"profile": "builtin:math-committee-v1"}, {"profile": "builtin:single-v1"}):
            with self.subTest(profile=profile), candidate(configure=lambda c: c.update(judging=profile)) as (_, _, intake):
                initial = pipeline.execute(intake); decision = verdict(intake)
                def values(decision):
                    if profile == PROFILE: return outputs(plan_for(intake), decision)
                    if profile["profile"] == "builtin:single-v1": return [decision]
                    return [report()] * 4 + [{"decision": decision, "resolutions": []}]
                with scripted_model(values(decision)):
                    first = pipeline.review(intake, initial)
                self.assertEqual(len(pipeline.accepted_structured_claims(pipeline.dossier(intake, initial, first))), 3)
                decision["judgment"][structured_claims.ACKNOWLEDGMENT] = "0" * 64
                with scripted_model(values(decision)), self.assertRaisesRegex(VerifierError, "structured_claims_review_binding"):
                    pipeline.review(intake, initial)

    def test_region_calls_obey_total_wall_deadline(self):
        from llm_verifier.simulation import ScriptedProvider
        with committee(judging=PROFILE, limits={"total_review_seconds": 2}) as (_, effective, intake):
            initial = pipeline.execute(intake); plan = plan_for(intake)
            providers = {m["id"]: ScriptedProvider(m["provider"], plan if m["stage"] == "planning" else report())
                         for m in effective["judging"]["committee"]["members"]}
            with patch("llm_verifier.committee.time.monotonic", side_effect=[0, 1, 3]), self.assertRaisesRegex(InfrastructureError, "committee_review_budget_exhausted"):
                pipeline.review(intake, initial, providers=providers)


    def test_final_region_requests_and_whole_evaluation_budget_still_fail_closed(self):
        for mode in ("request", "budget"):
            spec = {**PROFILE, "max_regions": 3}
            limits = {"total_provider_requests": 7} if mode == "budget" else {}
            with self.subTest(mode=mode), committee("computation", judging=spec, limits=limits) as (root, _, intake):
                plan = plan_for(intake); request = read_json(root / "simulation.json")["initial_decision"]["requests"][0]
                first_values = [plan, report(requests=[request])] + [region_report(r) for r in plan["regions"]]
                with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"checked_n":25}', b'')):
                    initial = pipeline.execute(intake)
                    with scripted_model(first_values): first = pipeline.review(intake, initial)
                    extra = pipeline.execute(intake, first, initial)
                final_values = [report()] + [region_report(r, requests=[request] if mode == "request" else []) for r in plan["regions"]]
                expected = "committee_final_requests" if mode == "request" else "committee_review_budget_exhausted"
                with scripted_model(final_values), self.assertRaisesRegex(InfrastructureError, expected):
                    pipeline.review(intake, initial, first, extra)

    def test_region_worker_format_is_preflighted_independently_of_planner(self):
        spec = {**PROFILE, "overrides": {name: {"provider": {"response_format": "json_object"}} for name in ("proof", "region")}}
        with committee("computation", judging=spec) as (root, _, _):
            manifest = read_json(root / "submission/submission.json")
            manifest["experiments"][0]["input_schema"]["properties"]["optional"] = {"type": "integer", "minimum": 0, "maximum": 1}
            write_json(root / "submission/submission.json", manifest)
            self.assertTrue(check(root)["valid"])
            raw = read_json(root / "verifier.json")
            raw["judging"]["overrides"]["region"]["provider"]["response_format"] = "json_schema"
            write_json(root / "verifier.json", raw)
            with patch.object(HttpTransport, "request") as http, self.assertRaisesRegex(VerifierError, "strict_provider_optional_inputs_require_json_object"):
                check(root)
            http.assert_not_called()

    def test_selected_policies_preprocessing_and_exact_scores_reach_region_workers(self):
        from llm_verifier.evidence import policy_for
        for policy in ("atomic-claims-v1", "exploratory-v1"):
            with self.subTest(policy=policy), example("scored-proof") as (root, _, _):
                raw = read_json(root / "verifier.json")
                raw.update(judging=PROFILE, acceptance_policy=policy, manifest_preprocessor="curvesmash-v1", rules_files=["extra.md"])
                write_json(root / "verifier.json", raw)
                (root / "extra.md").write_text("Protected region policy marker")
                manifest = read_json(root / "submission/submission.json")
                manifest["claims"][0]["score"] = {"numerator": "2", "denominator": "4"}
                write_json(root / "submission/submission.json", manifest)
                effective = load_config(root, "verifier.json", "submission")
                intake = intake_snapshot(effective, root / "submission")
                plan = plan_for(intake); initial = pipeline.execute(intake)
                responses = iter(outputs(plan, read_json(root / "simulation.json")["initial_decision"], outcome="dismissed"))
                bodies = []
                def request(body, *args):
                    bodies.append(json.loads(body)); return completion(next(responses))
                with patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"}), patch.object(HttpTransport, "request", side_effect=request):
                    first = pipeline.review(intake, initial)
                for body in bodies:
                    self.assertIn(policy_for(effective["config"], committee=True), body["messages"][0]["content"])
                    self.assertIn("Protected region policy marker", body["messages"][0]["content"])
                    user = body["messages"][1]["content"]
                    evidence = json.loads(user.split("\n", 1)[1].split("\nPrior committee reports", 1)[0])
                    self.assertEqual(evidence["original_submission_manifest"], intake["data"]["files"]["submission.json"])
                result = pipeline.dossier(intake, initial, first)
                self.assertEqual(result["data"]["judgment"]["scores"], [0.5])
                self.assertEqual(pipeline.export_results(result)["policy_id"], policy)
        with committee("collision", judging=PROFILE) as (root, _, intake):
            with patch.object(DockerRunner, "run", return_value=output_record("completed", 0, b'{"passed":false}', b'')):
                initial = pipeline.execute(intake)
            with scripted_model(outputs(plan_for(intake), read_json(root / "simulation.json")["initial_decision"])), self.assertRaises(VerifierError):
                pipeline.review(intake, initial)

    def test_many_regions_keep_local_finding_ids_distinct_and_replay_all_calls(self):
        with committee(judging={**PROFILE, "max_regions": 12}) as (root, effective, _):
            (root / "submission/proof.md").write_text("A mathematical step.\n" * 10)
            intake = intake_snapshot(effective, root / "submission")
            plan = plan_for(intake)
            manifest_region = next(r for r in plan["regions"] if r["path"] == "submission.json")
            plan["regions"] = [{"id": f"step{i}", "path": "proof.md", "start_line": i, "end_line": i,
                                "purpose": "Check the next step using full context."} for i in range(1, 11)] + [manifest_region]
            values = [plan, report()]
            responses = []
            for region in plan["regions"]:
                values.append(region_report(region, findings=[finding()]))
                responses.append({"finding_id": f'region/{region["id"]}/gap',
                                  "reason": "The unchanged evidence addresses this concern.", "evidence_refs": ["source:proof.md"]})
            values += [{"responses": responses}, {"decision": read_json(root / "simulation.json")["initial_decision"],
                       "resolutions": [{**r, "outcome": "dismissed"} for r in responses]}]
            initial = pipeline.execute(intake)
            with scripted_model(values): first = pipeline.review(intake, initial)
            self.assertEqual(len(first["data"]["reviews"]), 15)
            self.assertEqual(len(first["data"]["region_coverage"]), 11)
            self.assertEqual(first["data"]["provenance"]["allotted_completion_tokens"], 15 * 8192)
            self.assertTrue(pipeline.dossier(intake, initial, first)["data"]["judgment"]["valid"])
