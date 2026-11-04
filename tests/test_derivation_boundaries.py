from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from llm_verifier import derivation, derivation_schema, retained_result
from llm_verifier.artifacts import seal
from llm_verifier.derivation_transport import GitHubArtifacts, read_archive, storage_url
from llm_verifier.errors import VerifierError, InfrastructureError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest, sha
from llm_verifier.provider import HttpResponse, HttpTransport
from tests.derivation_support import case, config, decision, prepare, uid
from tests.support import completion


class Schema(unittest.TestCase):
    def wrapped(self, value, definitions=None):
        return {"type": "object", "properties": {"value": value}, "required": ["value"],
                "additionalProperties": False, **({"$defs": definitions} if definitions else {})}

    def test_local_refs_and_exclusive_union_validate_structured_output(self):
        union = {"oneOf": [{"$ref": "#/$defs/rational"}, {"type": "null"}]}
        rational = {"type": "object", "additionalProperties": False, "required": ["n", "d"], "properties": {
            "n": {"type": "string", "maxLength": 256, "pattern": "^(0|-?[1-9][0-9]*)$"},
            "d": {"type": "string", "maxLength": 256, "pattern": "^[1-9][0-9]*$"}}}
        schema = derivation_schema.compile_schema(self.wrapped(union, {"rational": rational}))
        derivation_schema.validate_value(schema, {"value": {"n": "-2", "d": "3"}})
        derivation_schema.validate_value(schema, {"value": None})
        for value in ({"n": "02", "d": "3"}, {"n": "2", "d": "0"}, {"n": "2", "d": "3", "extra": 1}):
            with self.assertRaises(VerifierError):
                derivation_schema.validate_value(schema, {"value": value})
        wire = derivation_schema.provider_schema(schema)
        self.assertIn("anyOf", wire["properties"]["value"])
        self.assertNotIn("$ref", canonical(wire).decode())

    def test_remote_recursive_open_unbounded_and_expensive_regex_schemas_fail(self):
        cases = [self.wrapped({"$ref": "https://untrusted.invalid/schema"}),
                 self.wrapped({"$ref": "#/$defs/r"}, {"r": {"$ref": "#/$defs/r"}}),
                 self.wrapped({"type": "array", "items": {"type": "boolean"}}),
                 self.wrapped({"type": "string", "maxLength": 30, "pattern": "^(a+)+$"}),
                 self.wrapped({"type": "object", "properties": {}, "required": [], "additionalProperties": True})]
        for value in cases:
            with self.subTest(schema=value), self.assertRaises(VerifierError):
                derivation_schema.compile_schema(value)

    def test_exclusive_union_and_constant_types_are_checked_locally(self):
        schema = derivation_schema.compile_schema(self.wrapped({"oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 10},
            {"type": "integer", "minimum": 5, "maximum": 20}]}))
        with self.assertRaises(VerifierError):
            derivation_schema.validate_value(schema, {"value": 7})
        schema = derivation_schema.compile_schema(self.wrapped({"const": True}))
        with self.assertRaises(VerifierError):
            derivation_schema.validate_value(schema, {"value": 1})

    def test_property_names_are_not_rewritten_as_schema_keywords(self):
        schema = {"type": "object", "required": ["oneOf", "const"], "additionalProperties": False,
                  "properties": {"oneOf": {"type": "boolean"}, "const": {"type": "null"}}}
        self.assertEqual(derivation_schema.provider_schema(schema), schema)


class Transport(unittest.TestCase):
    def test_exact_archive_and_file_pins_and_unsafe_members(self):
        with case() as f:
            ref = f["plan"]["entries"][0]["artifacts"]["intake"]
            raw = (f["archives"] / "101.zip").read_bytes()
            self.assertEqual(json.loads(read_archive(raw, ref)), f["old"])
            for field in ("archive_sha256", "file_sha256"):
                changed = {**ref, field: "f" * 64}
                with self.assertRaises(VerifierError):
                    read_archive(raw, changed)
            for name, mode in (("../intake.json", None), ("/intake.json", None), ("intake.json", stat.S_IFLNK)):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as bundle:
                    info = zipfile.ZipInfo(name)
                    if mode:
                        info.create_system = 3
                        info.external_attr = (mode | 0o777) << 16
                    bundle.writestr(info, b"ignored")
                value = output.getvalue()
                with self.assertRaises(VerifierError):
                    read_archive(value, {**ref, "archive_sha256": sha(value)})

    def test_metadata_is_bound_to_validity_not_later_score(self):
        with case() as f:
            ref = f["plan"]["entries"][0]["artifacts"]["intake"]
            raw = (f["archives"] / "101.zip").read_bytes()
            run = {"id": 100, "run_attempt": 1, "head_sha": "a" * 40,
                   "status": "completed", "conclusion": "success"}
            info = {"id": 101, "name": "intake", "expired": False, "size_in_bytes": len(raw),
                    "digest": "sha256:" + sha(raw), "workflow_run": {"id": 100, "head_sha": "a" * 40}}
            client = GitHubArtifacts(token="offline-test-token")
            for field, value in (("id", 999), ("head_sha", "different"), ("run_attempt", 2)):
                with self.subTest(field=field), patch.object(client, "metadata", return_value={**run, field: value}), \
                     patch.object(client, "request") as request:
                    with self.assertRaises(VerifierError):
                        client.fetch(ref, f["evaluation"]["retained_result"]["validity"], f["root"] / "new")
                    request.assert_not_called()
            with patch.object(client, "metadata", side_effect=[run, info]), \
                 patch.object(client, "request", return_value=(raw, None)):
                client.fetch(ref, f["evaluation"]["retained_result"]["validity"], f["root"] / "new")
            self.assertEqual((f["root"] / "new/101.zip").read_bytes(), raw)

    def test_redirect_uses_no_auth_and_rejects_unknown_storage(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}
        opener = Mock()
        opener.open.return_value = Response(b"data")
        client = GitHubArtifacts(token="secret-not-for-storage", opener=opener)
        client.request("https://fixture.blob.core.windows.net/a", 100)
        self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
        for value in ("https://127.0.0.1/a", "http://fixture.blob.core.windows.net/a",
                      "https://fixture.blob.core.windows.net.evil.invalid/a",
                      "https://user:secret@fixture.blob.core.windows.net/a", "https://fixture.blob.core.windows.net/a#fragment"):
            with self.subTest(value=value), self.assertRaises(VerifierError):
                storage_url(value)


class RetainedReceipts(unittest.TestCase):
    def context(self, f):
        selected = f["evaluation"]["retained_result"]
        return {"version": 1, "api": "https://yukon.invalid", "audience": "yukon:accepted-results:test",
                "benchmark_id": selected["benchmark_id"], "submission_id": selected["submission_id"],
                "challenge_id": None, "evaluation_id": selected["destination"]["evaluation_id"],
                "job_id": selected["destination"]["job_id"]}

    def snapshot(self, context):
        return {"version": 1, "snapshot_id": uid(500), "consumer": {k: v for k, v in context.items()
                if k not in {"version", "api", "audience"}}, "references": [], "sources": [], "ancestors": []}

    def test_receipt_only_resolves_empty_and_binds_new_job_never_original_job(self):
        with case() as f:
            context = self.context(f)
            snap = self.snapshot(context)
            with patch("llm_verifier.accepted.resolve", return_value=snap) as resolve:
                intake = derivation.prepare(f["root"], "derivation.json", "submission", f["evaluation"],
                    f["context"], f["archives"], accepted_results_context=context)
            resolve.assert_called_once_with(context, [])
            self.assertEqual(derivation.receipt(intake), {"version": 1, "snapshot_id": uid(500)})
            self.assertEqual(derivation.source_material(intake)["accepted_results"], f["bundle"])
            self.assertNotIn("accepted_results", derivation.receipt(intake))
            wrong = {**context, "job_id": f["evaluation"]["retained_result"]["validity"]["job_id"]}
            with patch("llm_verifier.accepted.resolve") as resolve, self.assertRaisesRegex(VerifierError, "consumer_binding"):
                derivation.prepare(f["root"], "derivation.json", "submission", f["evaluation"], f["context"],
                                   f["archives"], accepted_results_context=wrong)
            resolve.assert_not_called()

    def test_no_self_import_or_dependency_list_can_enter_receipt_snapshot(self):
        with case() as f:
            context = self.context(f)
            for field in ("references", "sources", "ancestors"):
                snap = self.snapshot(context); snap[field] = [{"submission_id": context["submission_id"]}]
                with patch("llm_verifier.accepted.resolve", return_value=snap), self.assertRaisesRegex(VerifierError, "must_be_empty"):
                    derivation.prepare(f["root"], "derivation.json", "submission", f["evaluation"], f["context"],
                                       f["archives"], accepted_results_context=context)

    def test_wire_unicode_length_uses_utf16_and_rejects_unpaired_surrogates(self):
        for value in (" ", "\0", "😀" * 512, "x" * 1024):
            retained_result.bounded_ref(value)
        for value in ("", "😀" * 513, "x" * 1025, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(VerifierError):
                retained_result.bounded_ref(value)

    def test_origin_file_policy_and_manifest_links_cannot_be_resealed_away(self):
        with case() as f:
            intake = prepare(f)
            original = deepcopy(intake)
            for mutate in (lambda d: d["current_files"]["proof.md"].update(text="changed"),
                           lambda d: d["original"]["intake"].update(sha256="a" * 64),
                           lambda d: d["plan_entry"]["origin"]["validity"].update(valid=False)):
                changed = deepcopy(original)
                mutate(changed["data"])
                changed = seal(changed["stage"], changed["bindings"], changed["data"])
                with self.assertRaises(VerifierError):
                    derivation.verify_intake(changed)

    def test_token_budget_bounds_retries_and_no_format_repair_call(self):
        with case() as f:
            cfg = f["config"]
            cfg["provider"]["max_attempts"] = 3
            cfg["limits"]["total_provider_requests"] = 3
            raw = canonical(cfg) + b"\n"
            (f["root"] / "derivation.json").write_bytes(raw)
            f["plan"]["config_sha256"] = sha(raw)
            write_json(f["root"] / "plan.json", f["plan"])
            intake = prepare(f)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}), \
                 patch.object(HttpTransport, "request", return_value=HttpResponse(503, b"")) as request:
                with self.assertRaises(InfrastructureError):
                    derivation.review(intake)
            self.assertEqual(request.call_count, 1)  # 256-token total budget admits only one call.
            with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}), \
                 patch.object(HttpTransport, "request", return_value=completion({"valid": True})) as request:
                with self.assertRaises(VerifierError):
                    derivation.review(intake)
            self.assertEqual(request.call_count, 1)


class HostedContract(unittest.TestCase):
    def test_ordinary_baseline_accepts_marker_policy_without_retained_descriptor(self):
        from llm_verifier.config import normalize, select_evaluation
        with case() as f:
            raw = deepcopy(f["old"]["data"]["effective"]["config"])
            policy = {"version": "scoring-v2", "retained_result": retained_result.VERSION}
            raw["policies"]["scoring"] = policy
            raw["supported_policies"]["scoring"] = [{"selector": policy, "instructions": "Ordinary baseline review."}]
            config = normalize(raw)
            selected, instructions = select_evaluation(config)
            self.assertEqual(selected["fields"], {"scores": True, "validity": True})
            self.assertEqual(selected["policies"]["scoring"], policy)
            self.assertEqual(instructions["scoring"], "Ordinary baseline review.")
            self.assertNotIn("retained_result", selected)

    def test_hosted_workflow_keeps_credentials_execution_and_artifacts_separate(self):
        import re
        from tests.support import REPO
        workflow = (REPO / ".github/workflows/derive-score.yml").read_text()
        prepare, review = workflow.split("  derive:", 1)
        self.assertNotIn("OPENAI_API_KEY: ${{ secrets.", prepare)
        self.assertNotIn("actions/checkout", review)
        self.assertNotIn("GITHUB_TOKEN:", review)
        self.assertNotIn("id-token:", review)
        self.assertNotIn("experiments --", workflow)
        self.assertNotIn("review-initial", workflow)
        self.assertNotIn("finalize --", workflow)
        self.assertIn("permissions: {}", review)
        self.assertIn("name: verifier-accepted-results", prepare)
        self.assertIn("/receipt/accepted-results-receipt.json", prepare)
        self.assertNotIn("/accepted-results.json", workflow)
        for action in re.findall(r"uses: (actions/[^\s]+)", workflow):
            self.assertRegex(action, r"^actions/[a-z-]+@[a-f0-9]{40}$")
        in_run = False
        for line in workflow.splitlines():
            if line == "        run: |":
                in_run = True
            elif in_run and line and not line.startswith("          "):
                in_run = False
            if in_run:
                self.assertNotIn("${{", line)

    def test_hosted_bootstrap_executes_offline_for_exact_pin_and_rejects_wrong_pin(self):
        import subprocess
        from tests.support import REPO
        workflow = (REPO / ".github/workflows/derive-score.yml").read_text().splitlines()
        start = workflow.index("        run: |") + 1
        block = []
        for line in workflow[start:]:
            if line and not line.startswith("          "):
                break
            block.append(line[10:])
        for same in (True, False):
            env = {**os.environ, "VERIFIER_REVISION": "a" * 40, "VERIFIER_REPOSITORY": "owner/verifier",
                   "CALLED_WORKFLOW": json.dumps({"workflow_repository": "owner/verifier", "workflow_sha": ("a" if same else "b") * 40})}
            result = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", "\n".join(block)], env=env, capture_output=True)
            self.assertEqual(result.returncode == 0, same)
