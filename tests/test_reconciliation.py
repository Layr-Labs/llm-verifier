"""Current authoring/check gates and atomic prior reuse share one contract."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier import pipeline, scaffold
from llm_verifier.checks import score_gate
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from tests.test_checks import registered, initial_run, result
from tests.test_prior_results import upstream, install, resolved_intake
from tests.test_scoring_sources import starter, verdict, judge, checker_source


class Reconciliation(unittest.TestCase):
    def test_current_claim_and_checker_scores_with_prior_submission(self):
        with upstream() as (_,_,_,bundle):
            for kind,context in [('proof',starter()),('checker',registered(config_change=checker_source))]:
                with self.subTest(kind=kind),context as (root,_,_):
                    effective=install(root,bundle);intake=resolved_intake(effective,root/'submission',bundle)
                    initial=pipeline.execute(intake) if kind=='proof' else initial_run(intake,result({'passed':True,'value':7}))
                    decision=verdict(intake,initial);decision['judgment']['evidence_refs'].append('prior:source-0')
                    first=judge(intake,initial,decision)
                    dossier=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted.json')
                    self.assertIsNone(first['data']['decision']['judgment']['scores'])
                    self.assertEqual(dossier['data']['judgment']['scores'],[0.5] if kind=='proof' else [7])
                    self.assertEqual(pipeline.export_results(dossier),read_json(root/'accepted.json'))
                    self.assertEqual(read_json(root/'accepted-results-receipt.json')['snapshot_id'],'snapshot')
                    if kind=='checker':
                        failed=initial_run(intake,result({'passed':False,'value':7}))
                        reviewed=judge(intake,failed,verdict(intake,failed))
                        self.assertIsNone(pipeline.dossier(intake,failed,reviewed)['data']['judgment']['scores'])

    def test_prior_score_or_check_pass_cannot_fill_current_missing_source(self):
        with starter() as (_,_,source):
            runs=pipeline.execute(source);first=judge(source,runs,verdict(source,runs))
            old=pipeline.dossier(source,runs,first);bundle=pipeline.export_results(old)
            for context in [starter(change=lambda m:m.update(claims=['For every real x, x² ≥ 0.'])),
                            registered(config_change=checker_source)]:
                with context as (root,_,_):
                    effective=install(root,bundle);intake=resolved_intake(effective,root/'submission',bundle)
                    initial=(initial_run(intake,result({'passed':False,'value':7})) if intake['data']['manifest'].get('checks') else pipeline.execute(intake))
                    review=judge(intake,initial,verdict(intake,initial))
                    report=pipeline.dossier(intake,initial,review)['data']
                    self.assertTrue(report['judgment']['valid'])
                    self.assertIsNone(report['judgment']['scores']);self.assertEqual(report['score_records'],[])

    def test_atomic_failure_blocks_all_scores_and_partial_sources_do_not_publish_records(self):
        def objectives(config):config['objectives'].append({**config['objectives'][0],'name':'second','score_source':{'claim':1}})
        with starter(change=lambda m:m['claims'].append('For every real x, x² ≥ 0.'),configure=objectives) as (_,_,intake):
            initial=pipeline.execute(intake)
            for valid in (True,False):
                review=judge(intake,initial,verdict(intake,initial,valid=valid))
                data=pipeline.dossier(intake,initial,review)['data']
                self.assertIsNone(data['judgment']['scores']);self.assertEqual(data['score_records'],[])
        with starter(configure=lambda c:c['objectives'].append({**c['objectives'][0],'name':'second'})) as (_,_,intake):
            initial=pipeline.execute(intake);review=judge(intake,initial,verdict(intake,initial))
            self.assertEqual(pipeline.dossier(intake,initial,review)['data']['judgment']['scores'],[0.5,0.5])

    def test_infinity_is_complete_exact_record_even_when_unscored_disallowed(self):
        with starter(change=lambda m:m['claims'][0].update(score='positive_infinity'),configure=lambda c:c.update(allow_unscored=False)) as (_,_,intake):
            initial=pipeline.execute(intake);review=judge(intake,initial,verdict(intake,initial))
            data=pipeline.dossier(intake,initial,review)['data']
            self.assertTrue(data['judgment']['valid']);self.assertIsNone(data['judgment']['scores'])
            self.assertEqual(data['score_records'][0]['record'],'positive_infinity')
        with registered(config_change=lambda c:(checker_source(c),c.update(allow_unscored=False))) as (_,_,intake):
            initial=initial_run(intake,result({'passed':False,'value':7}))
            with self.assertRaisesRegex(VerifierError,'scored_claim_required'):judge(intake,initial,verdict(intake,initial))

    def test_editable_check_and_generation_never_resolve_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'challenge';scaffold.initialize(root,'test','test-model')
            manifest=read_json(root/'submission/submission.json');manifest['prior_results']=[{'submission_id':'earlier'}]
            write_json(root/'submission/submission.json',manifest)
            with patch('llm_verifier.accepted.resolve') as resolver:
                self.assertTrue(scaffold.check(root)['valid']);scaffold.github(root,'owner/verifier','b'*40)
                self.assertTrue(scaffold.check(root)['valid']);resolver.assert_not_called()
            caller=(root/'.github/workflows/benchmark.yml').read_text()
            self.assertEqual(caller.count('      yukon_accepted_results:'),2)
            self.assertIn('id-token: write',caller)
            self.assertTrue((root/'.github/workflows/checker-tests.yml').exists())
            self.assertFalse(any('definition' in str(p) or p.name=='SKILL.md' for p in root.rglob('*')))
