"""Acceptance, exact proof identity and deterministic projection end to end."""
from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import unittest

from llm_verifier import pipeline, scaffold
from llm_verifier.artifacts import intake_snapshot
from llm_verifier.checks import score_gate
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.scoring import score_value
from tests.support import reseal, scripted_model
from tests.test_checks import registered, initial_run, result, retry


@contextmanager
def starter(change=None, configure=None):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)/'challenge'; scaffold.initialize(root,'test','explicit/model')
        if change:
            submission=read_json(root/'submission/submission.json'); change(submission)
            write_json(root/'submission/submission.json',submission)
        if configure:
            config=read_json(root/'verifier.json');configure(config);write_json(root/'verifier.json',config)
        effective=load_config(root,'verifier.json','submission')
        yield root,effective,intake_snapshot(effective,root/'submission')


def verdict(intake,initial,accepted=None,scores=None,followup=None):
    bound=score_gate(intake['data'],initial,followup)['bindings']
    return {'kind':'judgment','requests':[],'judgment':{
        'valid':True,'scores':scores,'reason':'The exact scoped statement follows from the supplied evidence.',
        'accepted_claims':[b['claim_id'] for b in bound] if accepted is None else accepted,
        'assumptions':[],'unresolved_obligations':[],'evidence_refs':['source:proof.md'],
        'review_standard':intake['data']['effective']['config']['review_standard']}}


def judge(intake,initial,decision):
    with scripted_model([decision]):return pipeline.review(intake,initial)


def checker_source(c):
    c['objectives'][0]['score_source']={'check':'witness','field':'value'}
    output=c['checkers'][0]['output_schema'];output['properties']['value']={'type':'integer','minimum':0,'maximum':1000}
    output['required'].append('value')


class ScoringSources(unittest.TestCase):
    def test_proof_once_in_prompt_and_score_derived_at_finalization(self):
        with starter() as (root,_,intake):
            initial=pipeline.execute(intake)
            messages,schema,_=pipeline.review_material(intake,initial)
            self.assertEqual(messages[1]['content'].count('The identity 1 + ... + n'),1)
            self.assertEqual(schema['properties']['judgment']['properties']['scores'],{'type':'null'})
            first=judge(intake,initial,verdict(intake,initial))
            report=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
            self.assertIsNone(first['data']['decision']['judgment']['scores'])
            self.assertEqual(read_json(root/'score.json')['scores'],[0.5])
            self.assertEqual(report['data']['score_records'][0]['record'],{'numerator':'1','denominator':'2'})

    def test_model_cannot_choose_scores_or_substitute_a_lemma(self):
        with starter() as (_,_,intake):
            initial=pipeline.execute(intake)
            with self.assertRaisesRegex(VerifierError,'bound_scores_are_derived'):
                judge(intake,initial,verdict(intake,initial,scores=[999]))
            first=judge(intake,initial,verdict(intake,initial,accepted=['Only an auxiliary lemma.']))
            report=pipeline.dossier(intake,initial,first)
            self.assertIsNone(report['data']['judgment']['scores'])
            self.assertEqual(report['data']['score_records'],[])
        with starter(configure=lambda c:c.update(allow_unscored=False)) as (_,_,intake):
            initial=pipeline.execute(intake)
            with self.assertRaisesRegex(VerifierError,'scored_claim_required'):
                judge(intake,initial,verdict(intake,initial,accepted=['Only a lemma.']))

    def test_proof_changes_invalidate_acceptance_and_resealed_numeric_reviews(self):
        with starter() as (root,effective,intake):
            initial=pipeline.execute(intake); decision=verdict(intake,initial)
            first=judge(intake,initial,decision)
            first['data']['decision']['judgment']['scores']=[999]
            with self.assertRaisesRegex(VerifierError,'bound_scores_are_derived'):
                pipeline.dossier(intake,initial,reseal(first))
            (root/'submission/proof.md').write_text('Changed proof.')
            newer=intake_snapshot(effective,root/'submission'); runs=pipeline.execute(newer)
            self.assertNotEqual(verdict(newer,runs)['judgment']['accepted_claims'],decision['judgment']['accepted_claims'])
            review=judge(newer,runs,decision)
            self.assertIsNone(pipeline.dossier(newer,runs,review)['data']['judgment']['scores'])

    def test_long_proofs_use_snapshot_limits_without_chunking(self):
        with starter() as (root,effective,_):
            (root/'submission/proof.md').write_text('A proof line.\n'*3000)
            intake=intake_snapshot(effective,root/'submission')
            initial=pipeline.execute(intake)
            first=judge(intake,initial,verdict(intake,initial))
            self.assertEqual(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'],[0.5])

    def test_terminal_and_large_finite_are_distinct(self):
        for value,expected in [('positive_infinity',None),({'numerator':'9'*256,'denominator':'1'},[1e256])]:
            with self.subTest(value=str(value)[:30]),starter(change=lambda s:s['claims'][0].update(score=value)) as (_,_,intake):
                initial=pipeline.execute(intake);first=judge(intake,initial,verdict(intake,initial))
                report=pipeline.dossier(intake,initial,first)['data']
                self.assertEqual(report['judgment']['scores'],expected)
                self.assertEqual(report['score_records'][0]['record'],value)
        with self.assertRaisesRegex(VerifierError,'terminal_requires_maximization'):
            with starter(change=lambda s:s['claims'][0].update(score='positive_infinity'),configure=lambda c:c['objectives'][0].update(direction='-')) as (_,_,intake):
                pipeline.review_material(intake,pipeline.execute(intake))

    def test_canonical_numbers_and_evidence_paths(self):
        for value in ({'numerator':'2','denominator':'4'},{'numerator':'1','denominator':'0'},
                      {'numerator':True,'denominator':'1'},{'numerator':'01','denominator':'1'},
                      {'numerator':'9'*257,'denominator':'1'},'Infinity'):
            with self.subTest(value=str(value)[:30]),self.assertRaises(VerifierError):score_value(value)
        for paths in ([],['../rules.md'],['missing.md'],['submission.json'],['proof.md','proof.md']):
            with self.subTest(paths=paths),self.assertRaises(VerifierError):
                with starter(change=lambda s:s['claims'][0].update(evidence=paths)):pass
        self.assertEqual(score_value({'numerator':'-3','denominator':'2'}),-1.5)

    def test_checker_score_requires_pass_and_exact_acceptance(self):
        with registered(config_change=checker_source) as (_,_,intake):
            for output in ({'passed':False,'value':5},{'passed':True,'value':5}):
                initial=initial_run(intake,result(output));d=verdict(intake,initial)
                if not output['passed']:
                    with self.assertRaisesRegex(VerifierError,'accepted_score_source_unavailable'):judge(intake,initial,d)
                else:
                    first=judge(intake,initial,d)
                    self.assertEqual(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'],[5])
                    with self.assertRaisesRegex(VerifierError,'bound_scores_are_derived'):
                        judge(intake,initial,verdict(intake,initial,scores=[999]))

    def test_conflicting_canonical_retry_and_different_input_diagnostic(self):
        with registered(config_change=checker_source) as (root,_,intake):
            initial=initial_run(intake,result({'passed':True,'value':5}))
            for n in (1,2):
                first,extra=retry(root,intake,initial,{'n':n},result({'passed':True,'value':6}))
                d=verdict(intake,initial,followup=extra)
                if n==1:
                    with scripted_model([d]),self.assertRaisesRegex(VerifierError,'accepted_score_source_unavailable'):
                        pipeline.review(intake,initial,first,extra)
                else:
                    with scripted_model([d]):last=pipeline.review(intake,initial,first,extra)
                    self.assertEqual(pipeline.dossier(intake,initial,first,extra,last)['data']['judgment']['scores'],[5])

    def test_score_sources_are_closed_and_cannot_use_optional_checks(self):
        with registered(config_change=checker_source) as (_,effective,_):
            for change in (lambda c:c['checkers'][0].update(required_for_score=False),
                           lambda c:c['objectives'][0]['score_source'].update(field='passed'),
                           lambda c:c['objectives'].append({'name':'other','direction':'+','unit':'x','semantics':'Other.'}),
                           lambda c:c['objectives'][0].update(score_source={'claim':True})):
                c=copy.deepcopy(effective['config']);change(c)
                with self.assertRaises(VerifierError):normalize(c)
