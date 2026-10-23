"""Explicit protected exploratory acceptance reuses the standard phase pipeline."""
import unittest
from llm_verifier import pipeline
from llm_verifier.config import normalize
from llm_verifier.evidence import POLICY, EXPLORATORY
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json
from tests.test_scoring_sources import starter, judge, verdict


class ExploratoryPolicy(unittest.TestCase):
    def test_explicit_policy_reaches_initial_final_and_bound_score_instructions(self):
        with starter(configure=lambda c: c.update(acceptance_policy='exploratory-v1')) as (root, _, intake):
            initial = pipeline.execute(intake)
            for final in (False, True):
                previous = {'data': {'decision': {'kind': 'followup'}}} if final else None
                messages, _, _ = pipeline.review_material(intake, initial, previous)
                system = messages[0]['content']
                self.assertIn(EXPLORATORY, system)
                self.assertNotIn(POLICY, system)
                self.assertNotIn('ALL original submission.claims are established as written', system)
                self.assertIn('plausible_not_refuted', system)
                self.assertIn('remaining uncertainty', system)
            decision = verdict(intake, initial)
            decision['judgment']['reason'] = 'Plausible and supported; remaining uncertainty is recorded. Fixture only.'
            first = judge(intake, initial, decision)
            pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
            self.assertEqual(read_json(root/'score.json')['scores'], [0.5])

    def test_exploratory_rejection_does_not_emit_claim_score(self):
        with starter(configure=lambda c: c.update(acceptance_policy='exploratory-v1')) as (root, _, intake):
            initial = pipeline.execute(intake)
            first = judge(intake, initial, verdict(intake, initial, valid=False))
            pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
            self.assertEqual(read_json(root/'score.json')['scores'], None)
            self.assertFalse(read_json(root/'score.json')['valid'])

    def test_unknown_policy_fails_closed(self):
        with starter() as (_, effective, _):
            for value in ('accept_everything', None, [], {}):
                with self.subTest(value=value), self.assertRaises(VerifierError):
                    normalize({**effective['config'], 'acceptance_policy': value})

    def test_review_standard_label_alone_cannot_enable_exploratory_policy(self):
        with starter(configure=lambda c: c.update(review_standard='exploratory-v1')) as (_, _, intake):
            initial = pipeline.execute(intake)
            system = pipeline.review_material(intake, initial)[0][0]['content']
            self.assertIn(POLICY, system)
            self.assertNotIn(EXPLORATORY, system)

    def test_export_and_reuse_preserve_exploratory_qualification(self):
        from tests.support import example
        from tests.test_prior_results import install, resolved_intake
        with starter(configure=lambda c: c.update(acceptance_policy='exploratory-v1')) as (root, _, intake):
            initial = pipeline.execute(intake)
            first = judge(intake, initial, verdict(intake, initial))
            exported = pipeline.export_results(pipeline.dossier(intake, initial, first))
            self.assertEqual(exported['policy_id'], 'exploratory-v1')
            self.assertEqual(intake['data']['effective']['config']['evidence_policy'], 'exploratory-v1')
        with example() as (root, _, _):
            effective = install(root, exported)
            downstream = resolved_intake(effective, root/'submission', exported)
            initial = pipeline.execute(downstream)
            system = pipeline.review_material(downstream, initial)[0][0]['content']
            self.assertIn('Exploratory acceptance alone cannot establish a theorem', system)
            self.assertIn(POLICY, system)

    def test_mismatched_normalized_policy_is_rejected(self):
        with starter() as (_, effective, _):
            with self.assertRaisesRegex(VerifierError, 'mandatory_evidence_policy'):
                normalize({**effective['config'], 'acceptance_policy': 'exploratory-v1'})
