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
from tests.support import fill_toy, reseal, scripted_model
from tests.test_checks import registered, initial_run, result, retry


@contextmanager
def starter(change=None, configure=None):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)/'challenge'; scaffold.initialize(root,'test','test-model'); fill_toy(root)
        if change:
            submission=read_json(root/'submission/submission.json'); change(submission)
            write_json(root/'submission/submission.json',submission)
        if configure:
            config=read_json(root/'verifier.json');configure(config);write_json(root/'verifier.json',config)
        effective=load_config(root,'verifier.json','submission')
        yield root,effective,intake_snapshot(effective,root/'submission')


def verdict(intake,initial,scores=None,followup=None,valid=True):
    return {'kind':'judgment','requests':[],'judgment':{
        'valid':valid,'scores':scores,'reason':'Every original declared claim is established as written.' if valid else 'One declared claim is unsupported.',
        'evidence_refs':['source:proof.md'],
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

    def test_model_cannot_choose_scores_or_select_subset(self):
        with starter() as (_,_,intake):
            initial=pipeline.execute(intake)
            with self.assertRaisesRegex(VerifierError,'bound_scores_are_derived'):
                judge(intake,initial,verdict(intake,initial,scores=[999]))
            partial=verdict(intake,initial)
            partial['judgment']['accepted_claims']=['Only an auxiliary lemma.']
            with self.assertRaises(VerifierError):judge(intake,initial,partial)
            first=judge(intake,initial,verdict(intake,initial,valid=False))
            report=pipeline.dossier(intake,initial,first)
            self.assertIsNone(report['data']['judgment']['scores'])
            self.assertEqual(report['data']['score_records'],[])
        with starter(change=lambda s:s.update(claims=['Only a lemma.']), configure=lambda c:c.update(allow_unscored=False)) as (_,_,intake):
            initial=pipeline.execute(intake)
            with self.assertRaisesRegex(VerifierError,'scored_claim_required'):
                judge(intake,initial,verdict(intake,initial))

    def test_proof_changes_bind_source_and_cannot_replay_old_review(self):
        with starter() as (root,effective,intake):
            initial=pipeline.execute(intake);first=judge(intake,initial,verdict(intake,initial))
            modified=copy.deepcopy(first);modified['data']['decision']['judgment']['scores']=[999]
            with self.assertRaisesRegex(VerifierError,'bound_scores_are_derived'):
                pipeline.dossier(intake,initial,reseal(modified))
            (root/'submission/proof.md').write_text('Changed proof.')
            newer=intake_snapshot(effective,root/'submission');runs=pipeline.execute(newer)
            self.assertNotEqual(score_gate(intake['data'],initial)['bindings'][0]['source_sha256'],
                                score_gate(newer['data'],runs)['bindings'][0]['source_sha256'])
            with self.assertRaisesRegex(VerifierError,'artifact_binding'):
                pipeline.verify_initial_review(newer,runs,first)

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
                    first=judge(intake,initial,d)
                    self.assertIsNone(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'])
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
                    with scripted_model([d]):last=pipeline.review(intake,initial,first,extra)
                    report=pipeline.dossier(intake,initial,first,extra,last)['data']
                    self.assertTrue(report['judgment']['valid'])
                    self.assertIsNone(report['judgment']['scores'])
                    self.assertEqual(report['score_records'],[])
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


class ScoreMinimum(unittest.TestCase):
    @staticmethod
    def zero(config):
        config['objectives'][0]['score_minimum']={'numerator':'0','denominator':'1'}

    def test_negative_rejected_by_check_intake_and_consumed_artifacts(self):
        from llm_verifier.artifacts import verify_intake
        from llm_verifier.jsonutil import digest
        with starter() as (root,_,_):
            config=read_json(root/'verifier.json'); self.zero(config)
            write_json(root/'verifier.json',config)
            submission=read_json(root/'submission/submission.json')
            submission['claims'][0]['score']={'numerator':'-1','denominator':'1'}
            write_json(root/'submission/submission.json',submission)
            with self.assertRaisesRegex(VerifierError,'score_below_minimum'):
                scaffold.check(root)
            effective=load_config(root,'verifier.json','submission')
            with self.assertRaisesRegex(VerifierError,'score_below_minimum'):
                intake_snapshot(effective,root/'submission')
        # Exercise the consuming boundary independently of the intake constructor.
        with starter(change=lambda s:s['claims'][0].update(score={'numerator':'-1','denominator':'1'})) as (_,_,intake):
            self.zero(intake['data']['effective']['config'])
            intake['bindings']['config']=digest(intake['data']['effective'])
            constrained=reseal(intake)
            with self.assertRaisesRegex(VerifierError,'score_below_minimum'):
                verify_intake(constrained)
            with self.assertRaisesRegex(VerifierError,'score_below_minimum'):
                pipeline.execute(constrained)

    def test_zero_positive_and_infinity_keep_exact_records(self):
        for score,expected in [({'numerator':'0','denominator':'1'},[0.0]),
                               ({'numerator':'31','denominator':'10'},[3.1]),
                               ('positive_infinity',None)]:
            with self.subTest(score=score),starter(change=lambda s:s['claims'][0].update(score=score),configure=self.zero) as (_,_,intake):
                initial=pipeline.execute(intake)
                first=judge(intake,initial,verdict(intake,initial))
                data=pipeline.dossier(intake,initial,first)['data']
                self.assertEqual(data['judgment']['scores'],expected)
                self.assertEqual(data['score_records'][0]['record'],score)
        with starter(change=lambda s:s.update(claims=['An unscored partial lemma.']),configure=self.zero) as (_,_,intake):
            initial=pipeline.execute(intake)
            first=judge(intake,initial,verdict(intake,initial))
            self.assertIsNone(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'])

    def test_minimum_is_exact_and_unconstrained_negative_scores_still_work(self):
        minimum={'numerator':'9007199254740993','denominator':'9007199254740992'}
        self.assertEqual(score_value(minimum),1.0)
        with self.assertRaisesRegex(VerifierError,'score_below_minimum'):
            with starter(change=lambda s:s['claims'][0].update(score={'numerator':'1','denominator':'1'}),
                         configure=lambda c:c['objectives'][0].update(score_minimum=minimum)):
                pass
        with starter(change=lambda s:s['claims'][0].update(score={'numerator':'-1','denominator':'1'})) as (_,_,intake):
            initial=pipeline.execute(intake)
            first=judge(intake,initial,verdict(intake,initial))
            self.assertEqual(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'],[-1.0])

    def test_below_minimum_checker_acceptance_cannot_score(self):
        def configure(c):
            checker_source(c)
            c['objectives'][0]['score_minimum']={'numerator':'6','denominator':'1'}
        with registered(config_change=configure) as (_,_,intake):
            for value in (5,6):
                initial=initial_run(intake,result({'passed':True,'value':value}))
                decision=verdict(intake,initial)
                if value==5:
                    self.assertEqual(score_gate(intake['data'],initial)['bindings'][0]['status'],'below_minimum')
                    first=judge(intake,initial,decision)
                    self.assertIsNone(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'])
                else:
                    first=judge(intake,initial,decision)
                    self.assertEqual(pipeline.dossier(intake,initial,first)['data']['judgment']['scores'],[6])

    def test_invalid_minimum_configuration(self):
        with starter() as (_,effective,_):
            for minimum in (0,True,None,'positive_infinity',{'numerator':'2','denominator':'4'},
                            {'numerator':'0','denominator':'1','extra':True}):
                with self.subTest(minimum=minimum):
                    config=copy.deepcopy(effective['config'])
                    config['objectives'][0]['score_minimum']=minimum
                    with self.assertRaises(VerifierError):normalize(config)
            config=copy.deepcopy(effective['config']);self.zero(config)
            del config['objectives'][0]['score_source']
            with self.assertRaisesRegex(VerifierError,'score_minimum_requires_source'):normalize(config)


class ScoreMaximum(unittest.TestCase):
    @staticmethod
    def cap(config):
        config['objectives'][0]['score_maximum'] = {'numerator': '31', 'denominator': '10'}

    def test_upper_bound_rejects_at_check_intake_and_consumption(self):
        from llm_verifier.artifacts import verify_intake
        from llm_verifier.jsonutil import digest
        for value in ({'numerator': '4', 'denominator': '1'}, 'positive_infinity'):
            with self.subTest(value=value), starter(
                    change=lambda s: s['claims'][0].update(score=value)) as (root, effective, intake):
                self.cap(effective['config']); write_json(root/'verifier.json', effective['config'])
                with self.assertRaisesRegex(VerifierError, 'score_above_maximum'):
                    scaffold.check(root)
                with self.assertRaisesRegex(VerifierError, 'score_above_maximum'):
                    intake_snapshot(effective, root/'submission')
                self.cap(intake['data']['effective']['config'])
                intake['bindings']['config'] = digest(intake['data']['effective'])
                with self.assertRaisesRegex(VerifierError, 'score_above_maximum'):
                    verify_intake(reseal(intake))
        value = {'numerator': '9007199254740993', 'denominator': '9007199254740992'}
        self.assertEqual(score_value(value), 1.0)
        with self.assertRaisesRegex(VerifierError, 'score_above_maximum'):
            with starter(change=lambda s: s['claims'][0].update(score=value),
                         configure=lambda c: c['objectives'][0].update(score_maximum={'numerator': '1', 'denominator': '1'})):
                pass

    def test_inclusive_interval_preserves_exact_records_and_unscored_lemmas(self):
        def configure(c):
            ScoreMinimum.zero(c); self.cap(c)
        for value, expected in (({'numerator': '0', 'denominator': '1'}, [0]),
                                ({'numerator': '31', 'denominator': '10'}, [3.1])):
            with self.subTest(value=value), starter(
                    change=lambda s: s['claims'][0].update(score=value), configure=configure) as (root, _, intake):
                initial = pipeline.execute(intake)
                first = judge(intake, initial, verdict(intake, initial))
                dossier = pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
                self.assertEqual(read_json(root/'score.json')['scores'], expected)
                self.assertEqual(dossier['data']['score_records'][0]['record'], value)
        with starter(change=lambda s: s.update(claims=['A partial lemma.']), configure=configure) as (_, _, intake):
            initial = pipeline.execute(intake)
            first = judge(intake, initial, verdict(intake, initial))
            self.assertIsNone(pipeline.dossier(intake, initial, first)['data']['judgment']['scores'])

    def test_checker_above_maximum_cannot_score_but_can_remain_unscored(self):
        def configure(c):
            checker_source(c)
            c['objectives'][0]['score_maximum'] = {'numerator': '5', 'denominator': '1'}
        with registered(config_change=configure) as (_, _, intake):
            for value in (5, 6):
                initial = initial_run(intake, result({'passed': True, 'value': value}))
                if value == 6:
                    self.assertEqual(score_gate(intake['data'], initial)['bindings'][0]['status'], 'above_maximum')
                    first = judge(intake, initial, verdict(intake, initial))
                    self.assertIsNone(pipeline.dossier(intake, initial, first)['data']['judgment']['scores'])
                else:
                    first = judge(intake, initial, verdict(intake, initial))
                    self.assertEqual(pipeline.dossier(intake, initial, first)['data']['judgment']['scores'], [5])

    def test_invalid_maximum_and_exact_interval_order(self):
        with starter() as (_, effective, _):
            for value in (0, True, None, 'positive_infinity', {'numerator': '2', 'denominator': '4'}):
                with self.subTest(value=value), self.assertRaises(VerifierError):
                    config = copy.deepcopy(effective['config']); config['objectives'][0]['score_maximum'] = value
                    normalize(config)
            config = copy.deepcopy(effective['config']); self.cap(config)
            del config['objectives'][0]['score_source']
            with self.assertRaisesRegex(VerifierError, 'score_maximum_requires_source'):
                normalize(config)
            config = copy.deepcopy(effective['config'])
            config['objectives'][0].update(
                score_minimum={'numerator': '9007199254740993', 'denominator': '9007199254740992'},
                score_maximum={'numerator': '1', 'denominator': '1'})
            with self.assertRaisesRegex(VerifierError, 'objective_bounds_order'):
                normalize(config)
            config['objectives'][0]['score_maximum'] = config['objectives'][0]['score_minimum']
            normalize(config)  # A singleton interval is allowed.

    def test_shared_claim_must_satisfy_each_objective_and_terminal_direction(self):
        def configure(c):
            c['objectives'].append({**c['objectives'][0], 'name': 'second',
                                   'score_maximum': {'numerator': '0', 'denominator': '1'}})
        with self.assertRaisesRegex(VerifierError, 'score_above_maximum'):
            with starter(configure=configure):
                pass
        with self.assertRaisesRegex(VerifierError, 'terminal_requires_maximization'):
            with starter(change=lambda s: s['claims'][0].update(score='positive_infinity'),
                         configure=lambda c: c['objectives'][0].update(direction='-')):
                pass
