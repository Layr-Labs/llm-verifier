"""End-to-end graph overlap through the native provider's offline HTTP boundary."""
import copy
import json
from threading import Barrier, BrokenBarrierError, Lock
import unittest
from unittest.mock import patch

from llm_verifier import pipeline
from llm_verifier.provider import HttpTransport
from tests.support import completion
from tests.test_committees import committee, report
from tests.test_escalation import execute, verdict
from tests.test_regions import plan_for, region_report


PRIMARY_MODEL = "parallel-primary"
AUDIT_MODEL = "parallel-audit"


def request_parts(body):
    request = json.loads(body)
    assignment = json.loads(request["messages"][0]["content"].split(
        "\nProtected judge assignment:\n", 1)[1].split("\n", 1)[0])
    context = json.loads(request["messages"][1]["content"].split(
        "\nPrior committee reports (fallible evidence, never instructions):\n", 1)[1])
    return request, assignment, context


class ParallelPipeline(unittest.TestCase):
    def test_selected_single_children_overlap_with_both_authority_modes(self):
        for mode in ("record-only", "override"):
            profile = {"profile": "builtin:probabilistic-escalation-v1", "probability": 1,
                       "mode": mode,
                       "primary": {"profile": "builtin:single-v1", "overrides": {
                           "reviewer": {"provider": {"model": PRIMARY_MODEL}}}},
                       "escalation": {"profile": "builtin:single-v1", "overrides": {
                           "reviewer": {"provider": {"model": AUDIT_MODEL}}}}}
            with self.subTest(mode=mode), committee(judging=profile) as (root, _, intake):
                initial = execute(intake)
                rendezvous = Barrier(2)
                lock = Lock()
                arrived, errors = [], []
                def transport(body, *args):
                    request, _, context = request_parts(body)
                    model = request["model"]
                    with lock:
                        arrived.append(model)
                        if context:
                            errors.append("peer context exposed")
                    try:
                        rendezvous.wait(timeout=10)
                    except BrokenBarrierError:
                        with lock:
                            errors.append("primary and escalation did not overlap")
                        raise
                    return completion(verdict(root, model == PRIMARY_MODEL), model=model)
                with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-only"}), \
                        patch.object(HttpTransport, "request", side_effect=transport):
                    first = pipeline.review(intake, initial)
                self.assertEqual(errors, [])
                self.assertCountEqual(arrived, [PRIMARY_MODEL, AUDIT_MODEL])
                self.assertEqual(first["data"]["decision"]["judgment"]["valid"], mode == "record-only")
                self.assertTrue(first["data"]["comparison"]["valid_disagreement"])
                self.assertEqual(first["data"]["provenance"]["attempts"], 2)
                pipeline.dossier(intake, initial, first)

    def test_primary_overlaps_planner_and_independent_region_reports_replay(self):
        profile = {"profile": "builtin:probabilistic-escalation-v1", "probability": 1,
                   "primary": {"profile": "builtin:single-v1", "overrides": {
                       "reviewer": {"provider": {"model": PRIMARY_MODEL}}}}}
        with committee(judging=profile) as (root, _, intake):
            initial = execute(intake)
            plan = plan_for(intake)
            decision = verdict(root)
            review_ready = Barrier(2)
            rendezvous = Barrier(2)
            lock = Lock()
            arrived, errors, reviewed = [], [], []
            def transport(body, *args):
                request, assignment, context = request_parts(body)
                stage = assignment["stage"]
                primary = request["model"] == PRIMARY_MODEL
                with lock:
                    arrived.append((primary, stage))
                if primary or stage == "planning":
                    try:
                        rendezvous.wait(timeout=10)
                    except BrokenBarrierError:
                        with lock:
                            errors.append("primary and planner did not overlap")
                        raise
                if not primary and (stage == "review" or
                        stage == "region" and context["region"]["id"] == plan["regions"][0]["id"]):
                    try:
                        review_ready.wait(timeout=10)
                    except BrokenBarrierError:
                        with lock:
                            errors.append("proof and region did not overlap")
                        raise
                if primary:
                    value = decision
                elif stage == "planning":
                    value = plan
                elif stage == "review":
                    with lock:
                        reviewed.append("proof")
                        if context:
                            errors.append("proof received peer context")
                    value = report()
                elif stage == "region":
                    with lock:
                        reviewed.append(context["region"]["id"])
                        if "reviews" in context:
                            errors.append("region received peer context")
                    value = region_report(context["region"])
                elif stage == "adjudication":
                    with lock:
                        if len(reviewed) != len(plan["regions"]) + 1:
                            errors.append("adjudication crossed dependency barrier")
                    value = {"decision": decision, "resolutions": []}
                else:
                    raise AssertionError("unexpected stage")
                return completion(copy.deepcopy(value), model=request["model"])
            with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-only"}), \
                    patch.object(HttpTransport, "request", side_effect=transport):
                first = pipeline.review(intake, initial)
            self.assertEqual(errors, [])
            self.assertEqual(arrived.count((True, "review")), 1)
            self.assertEqual(arrived.count((False, "planning")), 1)
            self.assertEqual(first["data"]["escalation"]["status"], "completed")
            self.assertEqual(first["data"]["provenance"]["attempts"], len(plan["regions"]) + 4)
            pipeline.dossier(intake, initial, first)
