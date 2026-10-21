"""Regression tests for the single atomic response and mandatory evidence prompt."""
import copy
import unittest

from llm_verifier import pipeline
from llm_verifier.config import normalize
from llm_verifier.errors import VerifierError
from llm_verifier.evidence import POLICY, POLICY_ID
from llm_verifier.files import read_json
from llm_verifier.judgment import JUDGMENT_FIELDS, response_schema
from tests.support import example, direct
from tests.test_checks import registered, initial_run, retry, result


class AtomicEvidencePolicy(unittest.TestCase):
    def test_minimal_response_has_no_alternate_claim_or_condition_catalog(self):
        self.assertEqual(JUDGMENT_FIELDS,{'valid','scores','reason','evidence_refs','review_standard'})
        with example() as (root,_,intake):
            schema=response_schema(intake['data']['effective']['config'],intake['data']['manifest'],final=True)
            self.assertEqual(set(schema['properties']['judgment']['properties']),JUDGMENT_FIELDS)
            decision=read_json(root/'simulation.json')['initial_decision']
            for field,value in [('accepted_claims',['Only the first claim.']),('assumptions',['Added condition']),
                                ('unresolved_obligations',[]),('evidence_report',{})]:
                changed=copy.deepcopy(decision);changed['judgment'][field]=value
                with self.assertRaises(VerifierError):direct(root,intake,changed)

    def test_atomic_scope_and_external_evidence_in_every_review(self):
        required=['ALL declared claims exactly as written','Never accept only a subset',
                  'nonstandard material external result','reproduced and validated',
                  'STANDARD mathematical results','There is no web/PDF retrieval',
                  'Incidental assertions','only the claims','Do not import missing meaning',
                  'Generic context-dependent labels','prior reason prose']
        with registered() as (root,_,intake):
            initial=initial_run(intake)
            first,extra=retry(root,intake,initial,{'n':1},result())
            for args in [(intake,initial),(intake,initial,first,extra)]:
                messages,_,_=pipeline.review_material(*args)
                for text in required:
                    self.assertIn(text.lower(),messages[0]['content'].lower())
                self.assertIn(POLICY_ID,messages[0]['content'])
            self.assertIn('structural checks cannot guarantee',POLICY.lower())

    def test_current_policy_is_locked_and_old_representation_is_not_a_config_mode(self):
        with example() as (_,effective,_):
            self.assertEqual(effective['config']['evidence_policy'],POLICY_ID)
            for policy in ('scientific-evidence-v1','optional',''):
                config=copy.deepcopy(effective['config']);config['evidence_policy']=policy
                with self.assertRaises(VerifierError):normalize(config)
