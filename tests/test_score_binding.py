"""Acceptance, exact-record retention and fail-closed numeric projection."""
import copy
import unittest

from llm_verifier import pipeline
from llm_verifier.config import normalize
from llm_verifier.errors import VerifierError
from llm_verifier.files import write_json
from llm_verifier.score_binding import validate_record
from tests.support import reseal, scripted_model
from tests.test_checks import registered, initial_run, result, decision, retry

CLAIM = 'claim:' + 'a' * 64
FINITE = {'kind': 'finite', 'claim_id': CLAIM, 'numerator': '31', 'denominator': '10'}
TERMINAL = {'kind': 'terminal', 'claim_id': CLAIM, 'terminal': 'positive_infinity'}


def bind(config):
    config['objectives'][0].update(direction='+', score_binding={'version': 1, 'check': 'witness', 'field': 'record'})
    schema = config['checkers'][0]['output_schema']
    schema['properties']['record'] = {
        'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['finite', 'terminal'], 'maxLength': 8},
            'claim_id': {'type': 'string', 'maxLength': 70},
            'numerator': {'type': 'string', 'maxLength': 256},
            'denominator': {'type': 'string', 'maxLength': 256},
            'terminal': {'type': 'string', 'maxLength': 32}},
        'required': ['kind', 'claim_id'], 'additionalProperties': False}
    schema['required'].append('record')


def review(root, intake, initial, scores=(3.1,), accepted=True):
    d = decision(root, scores=scores)
    if accepted:
        d['judgment']['accepted_claims'].append(CLAIM)
    with scripted_model([d]):
        return pipeline.review(intake, initial)


class ScoreBinding(unittest.TestCase):
    def test_finite_equality_claim_acceptance_and_exact_record(self):
        with registered(config_change=bind) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': FINITE}))
            for scores, accepted, error in [([3.2], True, 'score_binding_mismatch'),
                                             ([3.1], False, 'score_claim_not_accepted')]:
                with self.subTest(error=error), self.assertRaisesRegex(VerifierError, error):
                    review(root, intake, initial, scores, accepted)
            first = review(root, intake, initial)
            report = pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
            self.assertEqual(report['data']['score_records'][0]['record'], FINITE)
            self.assertEqual(report['data']['yukon_projection']['scores'], [3.1])
            lemma = review(root, intake, initial, None, False)
            self.assertEqual(pipeline.dossier(intake, initial, lemma)['data']['score_records'], [])

    def test_terminal_is_distinct_from_lemma_and_never_numeric(self):
        with registered(config_change=bind) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': TERMINAL}))
            for score in (0, 3.1, 1e100):
                with self.assertRaisesRegex(VerifierError, 'terminal_requires_null_scores'):
                    review(root, intake, initial, [score])
            first = review(root, intake, initial, None)
            report = pipeline.dossier(intake, initial, first)
            self.assertIsNone(report['data']['yukon_projection']['scores'])
            self.assertEqual(report['data']['score_records'][0]['record'], TERMINAL)
            lemma = review(root, intake, initial, None, False)
            self.assertEqual(pipeline.dossier(intake, initial, lemma)['data']['score_records'], [])

    def test_resealed_review_and_hidden_scores_cannot_bypass(self):
        with registered(config_change=bind) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': FINITE}))
            first = review(root, intake, initial)
            first['data']['decision']['judgment']['scores'] = [4]
            first = reseal(first)
            write_json(root/'score.json', {'scores': [9]})
            with self.assertRaisesRegex(VerifierError, 'score_binding_mismatch'):
                pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
            self.assertFalse((root/'score.json').exists())
        evaluation = {'policies': {'scoring': {'version': 'lemma-unscored-v1'},
                                  'validity': {'version': 'scoped-claims-v1'}},
                      'fields': {'scores': False, 'validity': True}}
        with registered(config_change=bind, evaluation=evaluation) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': FINITE}))
            with self.assertRaisesRegex(VerifierError, 'score_binding_mismatch'):
                review(root, intake, initial, [5])

    def test_bad_records_stay_evidence_without_unlocking_score(self):
        bad = [dict(FINITE, numerator='62', denominator='20'), dict(FINITE, denominator='0'),
               dict(FINITE, numerator='-0'), dict(FINITE, numerator=True),
               dict(TERMINAL, numerator='0'), dict(TERMINAL, terminal='unknown'),
               dict(FINITE, claim_id='hostile instructions')]
        with registered(config_change=bind) as (root, _, intake):
            for record in bad:
                with self.subTest(record=record):
                    initial = initial_run(intake, result({'passed': True, 'record': record}))
                    with self.assertRaises(VerifierError):
                        review(root, intake, initial)
                    first = review(root, intake, initial, None, False)
                    self.assertEqual(pipeline.dossier(intake, initial, first)['data']['score_records'], [])

    def test_conflicting_same_input_retry_fails_but_diagnostic_does_not(self):
        with registered(config_change=bind) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': FINITE}))
            other = dict(FINITE, numerator='32', denominator='10')
            # Reduced and conflicting.
            other.update(numerator='16', denominator='5')
            first, extra = retry(root, intake, initial, {'n': 1}, result({'passed': True, 'record': other}))
            d = decision(root, scores=[3.1]); d['judgment']['accepted_claims'].append(CLAIM)
            with scripted_model([d]), self.assertRaisesRegex(VerifierError, 'score_binding_unsatisfied'):
                pipeline.review(intake, initial, first, extra)
            first, extra = retry(root, intake, initial, {'n': 2}, result({'passed': True, 'record': other}))
            with scripted_model([d]):
                last = pipeline.review(intake, initial, first, extra)
            self.assertEqual(pipeline.dossier(intake, initial, first, extra, last)['data']['score_records'][0]['record'], FINITE)

    def test_binding_config_is_closed_required_and_versioned(self):
        with registered(config_change=bind) as (_, effective, _):
            for mutate in (lambda c: c['objectives'][0]['score_binding'].update(version=True),
                           lambda c: c['objectives'][0]['score_binding'].update(check='other'),
                           lambda c: c['objectives'][0]['score_binding'].update(field='passed'),
                           lambda c: c['checkers'][0].update(required_for_score=False)):
                c = copy.deepcopy(effective['config']); mutate(c)
                with self.assertRaises(VerifierError):
                    normalize(c)

    def test_precision_extremes_invalid_scores_and_mixed_terminal(self):
        self.assertEqual(validate_record(dict(FINITE, numerator='1', denominator='1'+'0'*255)), 1e-255)
        self.assertEqual(validate_record(dict(FINITE, numerator='9'*256, denominator='1')), float(int('9'*256)))
        # Adjacent exact integers may tie in the transport; the records differ.
        self.assertEqual(validate_record(dict(FINITE, numerator='9007199254740992', denominator='1')),
                         validate_record(dict(FINITE, numerator='9007199254740993', denominator='1')))
        with registered(config_change=bind) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': FINITE}))
            d = decision(root, scores=[3.1], valid=False)
            with scripted_model([d]), self.assertRaisesRegex(VerifierError, 'score_claim_not_accepted'):
                pipeline.review(intake, initial)
        def mixed(c):
            bind(c)
            c['objectives'].append({'name': 'other', 'direction': '+', 'unit': 'units', 'semantics': 'Other objective.'})
        with registered(config_change=mixed) as (root, _, intake):
            initial = initial_run(intake, result({'passed': True, 'record': TERMINAL}))
            with self.assertRaisesRegex(VerifierError, 'terminal_requires_null_scores'):
                review(root, intake, initial, [0, 8])
            first = review(root, intake, initial, None)
            report = pipeline.dossier(intake, initial, first)
            self.assertEqual(report['data']['score_records'][0]['projection']['status'], 'unsupported')
            self.assertIsNone(report['data']['judgment']['scores'])

    def test_canonical_rational_and_binary64_transport(self):
        for numerator, denominator, expected in [('0', '1', 0), ('-3', '2', -1.5), ('1', '3', 1/3)]:
            self.assertEqual(validate_record(dict(FINITE, numerator=numerator, denominator=denominator)), expected)
        with self.assertRaises(VerifierError):
            validate_record(dict(FINITE, numerator='01'))


if __name__ == '__main__':
    unittest.main()
