"""Server partitions declare support availability, never theorem negations."""
import copy
import json
import unittest
from unittest.mock import patch

from llm_verifier import pipeline
from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.config import load_config
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical
from llm_verifier.prior import UNAVAILABLE_REASONS, intake_imports, validate_bundle
from tests.support import direct, example, reseal, scripted_model
from tests.test_checks import registered, initial_run, retry, result
from tests.test_prior_results import CONTEXT, declare, platform_snapshot, rehash, resolved_intake, upstream, reference_evaluation
from tests.test_scoring_sources import starter, verdict, judge


def unavailable_snapshot(bundle=None, reason='rejected'):
    snapshot=platform_snapshot([bundle] if bundle else [])
    snapshot['references'].append({'submission_id':'unavailable-A','unavailable':reason})
    return snapshot


def intake_for(root,snapshot,*,simulation=False):
    declare(root,snapshot)
    effective=load_config(root,'verifier.json','submission',reference_evaluation(root))
    return resolved_intake(effective,root/'submission',None,snapshot=snapshot,simulation=simulation)


class UnavailableReferences(unittest.TestCase):
    def test_mixed_partition_retains_original_manifest_but_only_accepted_lineage(self):
        with upstream() as (_,_,_,source),example() as (root,_,_):
            snapshot=unavailable_snapshot(source)
            intake=intake_for(root,snapshot)
            initial=pipeline.execute(intake)
            messages,_,refs=pipeline.review_material(intake,initial)
            evidence=json.loads(messages[1]['content'].split('\n',1)[1])
            self.assertEqual([r['submission_id'] for r in evidence['prior_results']],['source-0'])
            self.assertEqual(evidence['unavailable_prior_results'],[{'submission_id':'unavailable-A','unavailable':'rejected'}])
            self.assertIn('prior:source-0',refs);self.assertNotIn('prior:unavailable-A',refs)
            self.assertEqual(evidence['prior_results'][0]['manifest'],source['source']['manifest'])
            decision=read_json(root/'simulation.json')['initial_decision']
            decision['judgment']['evidence_refs'].append('prior:source-0')
            initial,first=direct(root,intake,decision)
            dossier=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted.json')
            exported=pipeline.export_results(dossier)
            self.assertEqual(exported['source']['manifest'],intake['data']['files']['submission.json'])
            self.assertEqual(exported['ancestors'],[{'submission_id':'source-0'}])
            self.assertEqual(read_json(root/'accepted-results-receipt.json'),{'version':1,'snapshot_id':snapshot['snapshot_id']})
            validate_bundle(exported)  # A declared unavailable reference is not required ancestry.

    def test_all_unavailable_statuses_allow_independent_acceptance_or_rejection(self):
        for reason in sorted(UNAVAILABLE_REASONS):
            for valid in (True,False):
                with self.subTest(reason=reason,valid=valid),example() as (root,_,_):
                    intake=intake_for(root,unavailable_snapshot(reason=reason))
                    decision=read_json(root/'simulation.json')['initial_decision']
                    decision['judgment'].update(valid=valid,reason='Independent proof establishes every claim.' if valid else 'Current evidence does not establish the claims.')
                    initial,first=direct(root,intake,decision)
                    dossier=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted.json')
                    self.assertEqual(dossier['data']['judgment']['valid'],valid)
                    self.assertEqual((root/'accepted.json').exists(),valid)
                    if valid:self.assertEqual(pipeline.export_results(dossier)['ancestors'],[])
                    self.assertEqual(read_json(root/'accepted-results-receipt.json')['snapshot_id'],'snapshot')

    def test_independently_accepted_source_is_reusable_without_old_unavailable_ancestor(self):
        with example() as (root,_,_):
            intake=intake_for(root,unavailable_snapshot())
            initial,first=direct(root,intake)
            source=pipeline.export_results(pipeline.dossier(intake,initial,first))
            self.assertIn('unavailable-A',source['source']['manifest']['text'])
            self.assertEqual(source['ancestors'],[])
            with example() as (next_root,_,_):
                next_intake=intake_for(next_root,platform_snapshot([source]))
                records,ancestors=intake_imports(next_intake['data']['prior_snapshot'],next_intake['data']['manifest'],'f'*32,1)
                self.assertEqual(len(records),1)
                self.assertEqual(ancestors,[{'submission_id':'source-0'}])

    def test_unavailable_cannot_be_named_as_accepted_evidence_or_score_support(self):
        with starter() as (root,_,_):
            intake=intake_for(root,unavailable_snapshot())
            initial=pipeline.execute(intake)
            decision=verdict(intake,initial)
            decision['judgment']['evidence_refs'].append('prior:unavailable-A')
            with self.assertRaisesRegex(VerifierError,'unknown_evidence_reference'):judge(intake,initial,decision)
            decision['judgment']['evidence_refs'].remove('prior:unavailable-A')
            reviewed=judge(intake,initial,decision)
            dossier=pipeline.dossier(intake,initial,reviewed)
            self.assertEqual(dossier['data']['judgment']['scores'],[0.5])  # Current source only.
            self.assertEqual(pipeline.export_results(dossier)['ancestors'],[])

    def test_partition_is_disjoint_exact_bounded_and_has_only_completed_statuses(self):
        good=unavailable_snapshot()
        manifest={'prior_results':[{'submission_id':'unavailable-A'}]}
        mutations=[lambda s:s['references'].append(copy.deepcopy(s['references'][0])),
                   lambda s:s['references'].append({'submission_id':'unavailable-A','bundle_sha256':'a'*64}),
                   lambda s:s['references'][0].update(bundle_sha256='a'*64),
                   lambda s:s['references'][0].update(reason='extra prose'),
                   lambda s:s['references'][0].update(submission_id='different'),
                   lambda s:s.update(references=[]),
                   lambda s:s['references'][0].update(submission_id=CONTEXT['submission_id'])]
        for status in ('pending','running','failed','cancelled','missing','service_unavailable',None,True,{},[]):
            mutations.append(lambda s,status=status:s['references'][0].update(unavailable=status))
        for mutate in mutations:
            snapshot=copy.deepcopy(good);mutate(snapshot)
            with self.subTest(snapshot=snapshot['references']),self.assertRaises(VerifierError):intake_imports(snapshot,manifest,'f'*32,1)
        good['references']=[{'submission_id':f'source-{i}','unavailable':'rejected'} for i in range(17)]
        manifest={'prior_results':[{'submission_id':r['submission_id']} for r in good['references']]}
        with self.assertRaises(VerifierError):intake_imports(good,manifest,'f'*32,1)

    def test_unavailable_source_cannot_fall_back_to_bundle_or_accepted_ancestry(self):
        with upstream() as (_,_,_,source):
            snapshot=platform_snapshot([source])
            snapshot['references']=[{'submission_id':'source-0','unavailable':'rejected'}]
            manifest={'prior_results':[{'submission_id':'source-0'}]}
            with self.assertRaisesRegex(VerifierError,'reference_snapshot_binding'):intake_imports(snapshot,manifest,'f'*32,1)
            changed=copy.deepcopy(source);changed['ancestors']=[{'submission_id':'unavailable-A'}];rehash(changed)
            snapshot=unavailable_snapshot(changed)
            manifest={'prior_results':[{'submission_id':r['submission_id']} for r in snapshot['references']]}
            with self.assertRaisesRegex(VerifierError,'unavailable_ancestor'):intake_imports(snapshot,manifest,'f'*32,1)

    def test_final_review_uses_same_partition_and_no_falsity_premise(self):
        with registered() as (root,_,_):
            intake=intake_for(root,unavailable_snapshot(reason='review_required'))
            initial=initial_run(intake);first,extra=retry(root,intake,initial,{'n':1},result())
            for args in [(intake,initial),(intake,initial,first,extra)]:
                messages,_,refs=pipeline.review_material(*args)
                self.assertIn('None of these statuses proves a theorem false',messages[0]['content'])
                self.assertIn('its old acceptance or its negation',messages[0]['content'])
                self.assertIn('awaiting required review',messages[0]['content'])
                self.assertNotIn('prior:unavailable-A',refs)
                self.assertEqual(json.loads(messages[1]['content'].split('\n',1)[1])['unavailable_prior_results'],
                                 [{'submission_id':'unavailable-A','unavailable':'review_required'}])
            decision=read_json(root/'simulation.json')['initial_decision']
            with scripted_model([decision]):last=pipeline.review(intake,initial,first,extra)
            self.assertEqual(pipeline.export_results(pipeline.dossier(intake,initial,first,extra,last))['ancestors'],[])

    def test_partition_is_phase_bound_and_failed_or_simulated_finalize_clears_outputs(self):
        for simulation in (False,True):
            with self.subTest(simulation=simulation),example() as (root,_,_):
                intake=intake_for(root,unavailable_snapshot(),simulation=simulation)
                initial,first=direct(root,intake)
                if not simulation:
                    intake=copy.deepcopy(intake)
                    intake['data']['prior_snapshot']['references'][0]['unavailable']='review_required'
                    intake=reseal(intake)
                for name in ('score.json','dossier.json','accepted.json','accepted-results-receipt.json'):write_json(root/name,{'stale':True})
                with self.assertRaises(VerifierError):pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted.json')
                self.assertFalse(any((root/name).exists() for name in ('score.json','dossier.json','accepted.json','accepted-results-receipt.json')))

    def test_pending_and_transport_failure_are_not_unavailable_judgments(self):
        with example() as (root,_,_):
            declare(root,unavailable_snapshot())
            effective=load_config(root,'verifier.json','submission',reference_evaluation(root))
            for code in ('service_unavailable','dependency_changed','unavailable'):
                with patch('llm_verifier.accepted.resolve',side_effect=InfrastructureError('prior_'+code)),self.assertRaises(InfrastructureError):
                    intake_snapshot(effective,root/'submission',accepted_results_context=CONTEXT)
