from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from llm_verifier import derivation, pipeline, retained_result
from llm_verifier.artifacts import seal
from llm_verifier.cli import main
from llm_verifier.config import select_evaluation
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest, sha
from llm_verifier.provider import OpenAI, HttpTransport
from llm_verifier.simulation import ScriptedProvider
from tests.derivation_support import case, prepare, decision, uid
from tests.support import completion, scripted_model


class Derivation(unittest.TestCase):
    def test_native_provider_path_derives_without_full_review_or_execution(self):
        with case() as f:
            intake = prepare(f)
            before = canonical(derivation.source_material(intake))
            expected = decision(intake)
            with patch.object(pipeline, "review", side_effect=AssertionError("full review")), \
                 patch.object(pipeline, "execute", side_effect=AssertionError("execution")), \
                 patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}), \
                 patch.object(HttpTransport, "request", return_value=completion(expected)) as request:
                result = derivation.review(intake)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(derivation.verify_derivation(result)["decision"], expected)
            self.assertFalse(result["simulation"])
            self.assertNotIn("valid", result["data"]["decision"])
            self.assertEqual(before, canonical(derivation.source_material(intake)))
            with self.assertRaises(VerifierError):
                pipeline.export_results(result)

    def test_false_validity_and_protected_unscored_plan_never_construct_provider(self):
        for valid in (False, True):
            with self.subTest(valid=valid), case(valid=valid, action="not_applicable") as f:
                intake = prepare(f)
                with patch("llm_verifier.provider.OpenAI", side_effect=AssertionError("provider")):
                    result = derivation.review(intake)
                self.assertEqual(result["data"]["decision"]["status"], "not_applicable")
                self.assertIsNone(result["data"]["decision"]["payload"])
                self.assertEqual(result["data"]["provenance"]["provider"], "none")

    def test_simulation_is_contagious_and_cannot_be_checked_for_publication(self):
        with case() as f:
            intake = prepare(f)
            provider = ScriptedProvider(intake["data"]["config"]["provider"], decision(intake))
            result = derivation.review(intake, provider=provider)
            self.assertTrue(result["simulation"])
            path = f["root"] / "result.json"
            write_json(path, result)
            self.assertEqual(main(["derivation-check", "--derivation", str(path), "--require-publishable"]), 2)

    def test_original_policy_and_full_manifest_are_preserved(self):
        for policy in ("atomic-claims-v1", "exploratory-v1"):
            with self.subTest(policy=policy), case(policy=policy) as f:
                intake = prepare(f)
                material = derivation.source_material(intake)
                self.assertEqual(material["accepted_results"]["policy_id"], policy)
                self.assertEqual(material["manifest"], f["old"]["data"]["manifest"])
                messages, _ = derivation.review_material(intake)
                self.assertIn(policy, messages[0]["content"])
                self.assertIn("plausible_not_refuted", messages[0]["content"])

    def test_changed_candidate_fails_before_credential_or_archive_read(self):
        with case() as f:
            (f["root"] / "submission/proof.md").write_text("changed")
            with patch("llm_verifier.derivation_transport.read_artifact", side_effect=AssertionError("archive")):
                with self.assertRaisesRegex(VerifierError, "derivation_changed_candidate"):
                    prepare(f)

    def test_same_candidate_different_selection_or_destination_cannot_swap(self):
        for path in (("packet_id",), ("validity", "result_id"), ("score", "result_id")):
            with self.subTest(path=path), case() as f:
                target = f["evaluation"]["retained_result"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = uid(999)
                with self.assertRaisesRegex(VerifierError, "derivation_unmatched_retained_result"):
                    prepare(f)
        with case() as f:
            intake = prepare(f)
            result = derivation.review(intake, provider=ScriptedProvider(intake["data"]["config"]["provider"], decision(intake)))
            result["data"]["intake"]["data"]["evaluation"]["retained_result"]["destination"]["job_id"] = uid(999)
            with self.assertRaises(VerifierError):
                derivation.verify_derivation(result)

    def test_score_origin_may_differ_but_validity_commit_must_match(self):
        with case() as f:
            selected = f["evaluation"]["retained_result"]
            selected["score"]["commit_sha"] = "later-score"
            f["plan"]["entries"][0]["origin"]["score"] = deepcopy(selected["score"])
            write_json(f["root"] / "plan.json", f["plan"])
            prepare(f)
            selected["validity"]["commit_sha"] = "wrong"
            f["plan"]["entries"][0]["origin"]["validity"] = deepcopy(selected["validity"])
            write_json(f["root"] / "plan.json", f["plan"])
            with self.assertRaisesRegex(VerifierError, "derivation_origin_validity_commit"):
                prepare(f)

    def test_citations_require_exact_claim_text_hash_lines_and_at_least_one_claim(self):
        with case() as f:
            intake = prepare(f)
            original = decision(intake)
            mutations = [lambda d: d["source_refs"][0].update(sha256="0" * 64),
                         lambda d: d["source_refs"][0].update(quote="invented"),
                         lambda d: d["source_refs"][0].update(start_line=0),
                         lambda d: d["source_refs"][0].update(end_line=9999),
                         lambda d: d.update(source_refs=[]),
                         lambda d: d.update(valid=True),
                         lambda d: d["payload"].update(extra="bad")]
            for mutate in mutations:
                changed = deepcopy(original); mutate(changed)
                with self.subTest(value=changed), self.assertRaises(VerifierError):
                    derivation.validate_decision(intake, changed)
            content = derivation.sources(intake)["file:proof.md"]
            d = deepcopy(original)
            d["source_refs"] = [{"source": "file:proof.md", "sha256": sha(content.encode()), "start_line": 1,
                                 "end_line": len(content.split("\n")), "quote": content}]
            with self.assertRaisesRegex(VerifierError, "derivation_claim_support_required"):
                derivation.validate_decision(intake, d)

    def test_unsupported_is_a_derivation_outcome_not_failed_validity(self):
        with case() as f:
            intake = prepare(f)
            result = derivation.review(intake, provider=ScriptedProvider(intake["data"]["config"]["provider"], decision(intake, "unsupported")))
            self.assertEqual(result["data"]["decision"]["status"], "unsupported")
            self.assertIsNone(result["data"]["decision"]["payload"])
            self.assertTrue(f["evaluation"]["retained_result"]["validity"]["valid"])

    def test_transport_failure_is_not_unsupported_and_records_diagnostic(self):
        with case() as f:
            intake = prepare(f)
            failure = f["root"] / "failure.json"
            with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}), \
                 patch.object(HttpTransport, "request", side_effect=InfrastructureError("provider_timeout")):
                with self.assertRaises(InfrastructureError):
                    derivation.review(intake, failure_out=failure)
            self.assertEqual(read_json(failure)["stage"], "review_failure")
            self.assertFalse(read_json(failure)["data"]["publishable"])

    def test_retained_wire_is_closed_score_only_and_ordinary_path_rejects_it(self):
        with case() as f:
            retained_result.evaluation(f["evaluation"])
            with self.assertRaises(VerifierError):
                select_evaluation(f["old"]["data"]["effective"]["config"], f["evaluation"])
            mutations = [lambda e: e.pop("retained_result"), lambda e: e["fields"].update(validity=True),
                         lambda e: e["policies"]["scoring"].pop("retained_result"),
                         lambda e: e["retained_result"].update(approved=True),
                         lambda e: e["retained_result"].update(candidate_key="b" * 64)]
            for mutate in mutations:
                changed = deepcopy(f["evaluation"]); mutate(changed)
                with self.subTest(value=changed), self.assertRaises(VerifierError):
                    retained_result.evaluation(changed)

    def test_real_cli_prepare_simulate_and_stale_output_cleanup(self):
        with case() as f:
            root = f["root"]
            args = ["derivation-prepare", "--root", str(root), "--archives", str(f["archives"]),
                    "--evaluation", json.dumps(f["evaluation"]), "--out", str(root / "intake.json")]
            self.assertEqual(main(args), 0)
            intake = read_json(root / "intake.json")
            write_json(root / "script.json", decision(intake))
            process = subprocess.run([sys.executable, "-m", "llm_verifier", "simulate-derivation",
                "--intake", str(root / "intake.json"), "--script", str(root / "script.json"),
                "--out", str(root / "simulation.json")], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(read_json(root / "simulation.json")["simulation_only"])
            write_json(root / "stale.json", {"scores": [100]})
            (root / "intake.json").write_text("malformed")
            self.assertEqual(main(["derive-score", "--intake", str(root / "intake.json"), "--out", str(root / "stale.json")]), 2)
            self.assertFalse((root / "stale.json").exists())
