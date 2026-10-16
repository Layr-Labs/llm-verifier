import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.config import load_config, normalize, select_evaluation
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.judgment import validate_judgment, yukon
from llm_verifier.jsonutil import canonical, digest
from llm_verifier import pipeline
from llm_verifier.provider import HttpTransport, OpenAI
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier.simulation import simulate
from tests.support import example, direct, reseal, scripted_model, completion


class Contracts(unittest.TestCase):
    def test_proof_final_unscored_and_invalid(self):
        for valid in (True,False):
            with self.subTest(valid=valid), example() as (root, effective, intake):
                decision=read_json(root/'simulation.json')['initial_decision']
                if not valid:
                    decision['judgment'].update(valid=False,reason='The submitted equality is unsupported.')
                initial,first=direct(root,intake,decision)
                result=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
                self.assertEqual(read_json(root/'score.json'),{'valid':valid,'scores':None,'reason':decision['judgment']['reason']})
                self.assertFalse(result['simulation'])
                self.assertEqual(len(result['data']['phases']),3)

    def test_computation_followup_final_score(self):
        with example('computation') as (root,effective,intake):
            script=read_json(root/'simulation.json')
            with patch.object(DockerRunner,'run',side_effect=[output_record('completed',0,canonical(v),b'') for v in script['initial_results']+script['followup_results']]) as runner, scripted_model([script['initial_decision'],script['final_decision']]) as _:
                initial=pipeline.execute(intake)
                first=pipeline.review(intake,initial)
                extra=pipeline.execute(intake,first,initial)
                final=pipeline.review(intake,initial,first,extra)
            result=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',extra,final)
            self.assertEqual(read_json(root/'score.json')['scores'],[25])
            self.assertEqual(runner.call_count,2)
            self.assertEqual(extra['data']['runs'][0]['request']['inputs'],{'n':25})
            self.assertEqual(len(result['data']['phases']),5)
            self.assertIn('Check a larger',result['data']['phases'][2]['data']['decision']['requests'][0]['reason'])

    def test_computation_no_request(self):
        with example('computation') as (root,_,intake):
            decision=read_json(root/'simulation.json')['final_decision']
            decision['judgment']['scores']=[10]
            decision['judgment']['evidence_refs'].remove('run:followup:0')
            initial,first=direct(root,intake,decision)
            pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
            self.assertEqual(read_json(root/'score.json')['scores'],[10])
            with self.assertRaises(VerifierError):
                pipeline.execute(intake,first,initial)

    def test_followup_rounds_and_incomplete_chain(self):
        with example('computation') as (root,_,intake):
            script=read_json(root/'simulation.json')
            initial,first=direct(root,intake)
            with self.assertRaisesRegex(VerifierError,'followup_incomplete'):
                pipeline.dossier(intake,initial,first)
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{"checked_n":25}',b'')):
                extra=pipeline.execute(intake,first,initial)
            with scripted_model([script['initial_decision']]), self.assertRaisesRegex(VerifierError,'final_judgment_required'):
                pipeline.review(intake,initial,first,extra)
            with scripted_model([script['final_decision']]):
                final=pipeline.review(intake,initial,first,extra)
            with self.assertRaises(VerifierError):
                pipeline.execute(intake,final,initial)

    def test_followup_disabled_invalid_inputs_and_budget(self):
        for mode in ('disabled','parameters','too_many','aggregate','extra_key'):
            with self.subTest(mode=mode), example('computation',mutate=(lambda c:c['limits'].update(max_followups=0)) if mode=='disabled' else None) as (root,_,intake):
                script=read_json(root/'simulation.json')
                decision=script['initial_decision']
                if mode=='parameters':decision['requests'][0]['inputs']['n']=100001
                if mode=='too_many':decision['requests']*=4
                if mode=='aggregate':decision['requests']*=3 # 4 x 20 > total 60
                if mode=='extra_key':decision['requests'][0]['source']='new.py'
                with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{}',b'')):
                    initial=pipeline.execute(intake)
                with scripted_model([decision]), self.assertRaises(VerifierError):
                    pipeline.review(intake,initial)

    def test_failed_run_is_preserved_evidence(self):
        for status,code,out in [('runtime_error',1,b'{}'),('protocol_error',0,b'not json'),('timeout',-9,b'{'),('output_limit',-9,b'x'*65536)]:
            with self.subTest(status=status), example('computation') as (_,_,intake):
                capture=status if status in {'timeout','output_limit'} else 'completed'
                with patch.object(DockerRunner,'run',return_value=output_record(capture,code,out,b'failure details')):
                    report=pipeline.execute(intake)
                self.assertEqual(report['data']['runs'][0]['execution']['status'],status)
                self.assertEqual(report['data']['runs'][0]['execution']['stderr']['utf8'],'failure details')

    def test_infrastructure_errors_do_not_produce_evidence(self):
        with example('computation') as (_,_,intake), patch.object(DockerRunner,'run',side_effect=InfrastructureError('docker_unavailable')):
            with self.assertRaises(InfrastructureError):pipeline.execute(intake)

    def test_stale_score_cleared_on_bad_chain(self):
        with example() as (root,_,intake):
            initial,first=direct(root,intake)
            write_json(root/'score.json',{'valid':True,'scores':[100]})
            first['data']['decision']['judgment']['reason']='tampered'
            with self.assertRaises(VerifierError):pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
            self.assertFalse((root/'score.json').exists())
            self.assertFalse((root/'dossier.json').exists())

    def test_tampering_and_replays_bind_every_stage(self):
        with example() as (root,_,intake):
            initial,first=direct(root,intake)
            changes=[lambda x:x['data']['files']['proof.md'].update(text='changed'),lambda x:x['data']['effective']['config'].update(id='other'),lambda x:x['data']['implementation'].update(content_sha256='0'*64),lambda x:x['data']['context'].update(revision='other')]
            for change in changes:
                modified=copy.deepcopy(intake);change(modified);modified=reseal(modified)
                with self.assertRaises(VerifierError):verify_intake(modified)
            new_intake=intake_snapshot(intake['data']['effective'],root/'submission')
            with self.assertRaisesRegex(VerifierError,'artifact_binding'):pipeline.verify_initial_review(new_intake,initial,first)
            changed=copy.deepcopy(initial);changed['data']['allotted_seconds']=20;changed=reseal(changed)
            with self.assertRaises(VerifierError):pipeline.verify_initial_review(intake,changed,first)
            modified=copy.deepcopy(first);modified['data']['provenance']['prompt_sha256']='0'*64;modified=reseal(modified)
            with self.assertRaises(VerifierError):pipeline.dossier(intake,initial,modified)
            with self.assertRaisesRegex(VerifierError,'unexpected_intake'):verify_intake(intake,'0'*64)

    def test_source_edits_after_intake_do_not_change_snapshot(self):
        with example('computation') as (root,_,intake):
            original=intake['data']['files']['check.py']['text']
            (root/'submission/check.py').write_text('raise RuntimeError("live source")')
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{}',b'')) as runner:
                pipeline.execute(intake)
            self.assertEqual(runner.call_args.args[0]['check.py']['text'],original)

    def test_finite_scores_objective_counts_and_validity_types(self):
        with example() as (root,effective,_):
            judgment=read_json(root/'simulation.json')['initial_decision']['judgment']
            for bad in ([float('inf')],[float('nan')],[],[1,2],[True]):
                with self.subTest(bad=bad),self.assertRaises(VerifierError):validate_judgment({**judgment,'scores':bad},effective['config'],['source:proof.md'])
            for bad in (1,'true',None):
                with self.assertRaises(VerifierError):validate_judgment({**judgment,'valid':bad},effective['config'],['source:proof.md'])

    def test_requested_fields_and_score_only_claim_gate(self):
        with example() as (root,_,_):
            judgment=read_json(root/'simulation.json')['initial_decision']['judgment']
            self.assertEqual(yukon(judgment,{'fields':{'scores':True,'validity':False}}),{'scores':None})
            self.assertEqual(yukon(judgment,{'fields':{'scores':False,'validity':True}}),{'valid':True,'reason':judgment['reason']})
            with self.assertRaisesRegex(VerifierError,'score_only_claim_not_established'):yukon({**judgment,'valid':False},{'fields':{'scores':True,'validity':False}})

    def test_policy_selectors_and_admission_are_exact(self):
        with example() as (_,effective,_):
            c=effective['config']
            selected={'policies':copy.deepcopy(c['policies']),'fields':{'scores':True,'validity':False},'admission':None}
            self.assertEqual(select_evaluation(c,selected)[0],selected)
            for modify in [lambda x:x['policies']['validity'].update(version='unknown'),lambda x:x.update(admission={}),lambda x:x['fields'].update(scores=False),lambda x:x.update(ignored=1)]:
                bad=copy.deepcopy(selected);modify(bad)
                with self.assertRaises(VerifierError):select_evaluation(c,bad)

    def test_local_zero_objectives_valid_unscored(self):
        with example(mutate=lambda c:c.update(objectives=[])) as (root,_,intake):
            initial,first=direct(root,intake)
            self.assertIsNone(pipeline.dossier(intake,initial,first)['data']['yukon_projection']['scores'])

    def test_full_context_not_silently_truncated(self):
        with example(mutate=lambda c:c['limits'].update(max_prompt_bytes=1024)) as (_,_,intake):
            initial=pipeline.execute(intake)
            with patch.object(HttpTransport,'request') as transport, self.assertRaisesRegex(InfrastructureError,'context_budget'):
                pipeline.review(intake,initial)
            transport.assert_not_called()

    def test_simulation_cannot_publish_or_downgrade(self):
        with example() as (root,effective,_):
            result=simulate(effective,root/'submission',read_json(root/'simulation.json'),root/'simulation-result.json')
            intake,initial,first=result['data']['phases']
            with self.assertRaisesRegex(VerifierError,'simulation_cannot_publish'):pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
            self.assertFalse((root/'score.json').exists())
            first['simulation']=False;first=reseal(first)
            with self.assertRaisesRegex(VerifierError,'simulation_downgrade'):pipeline.dossier(intake,initial,first)

    def test_optional_inputs_require_explicit_json_object_before_provider(self):
        from llm_verifier.judgment import response_schema
        with example('computation') as (root,effective,_):
            manifest=read_json(root/'submission/submission.json')
            manifest['experiments'][0]['input_schema']['required']=[]
            write_json(root/'submission/submission.json',manifest)
            intake=intake_snapshot(effective,root/'submission')
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{}',b'')):
                initial=pipeline.execute(intake)
            with patch.object(HttpTransport,'request') as transport,self.assertRaisesRegex(VerifierError,'optional_inputs_require_json_object'):
                pipeline.review(intake,initial)
            transport.assert_not_called()
            # No unused input variants occur in final or disabled schemas.
            final=response_schema(effective['config'],manifest,final=True)
            self.assertEqual(final['properties']['requests']['maxItems'],0)
            self.assertEqual(final['properties']['requests']['items']['properties'],{})
            effective['config']['provider']['response_format']='json_object'
            intake=intake_snapshot(effective,root/'submission')
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{}',b'')):
                initial=pipeline.execute(intake)
            decision=read_json(root/'simulation.json')['initial_decision']
            with scripted_model([decision]):pipeline.review(intake,initial)

    def test_whole_evaluation_provider_request_budget(self):
        with example('computation',mutate=lambda c:c['limits'].update(total_provider_requests=1)) as (root,_,intake):
            initial,first=direct(root,intake)
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{}',b'')):
                extra=pipeline.execute(intake,first,initial)
            with patch.object(HttpTransport,'request') as transport,self.assertRaisesRegex(InfrastructureError,'review_budget_exhausted'):
                pipeline.review(intake,initial,first,extra)
            transport.assert_not_called()

    def test_yukon_reason_limit(self):
        with example() as (root,effective,_):
            judgment=read_json(root/'simulation.json')['initial_decision']['judgment']
            validate_judgment({**judgment,'reason':'x'*4000},effective['config'],['source:proof.md'])
            with self.assertRaises(VerifierError):validate_judgment({**judgment,'reason':'x'*4001},effective['config'],['source:proof.md'])

    def test_typed_json_duplicates_cannot_substitute_bool_int_or_float(self):
        with example('computation') as (root,_,intake):
            changed=copy.deepcopy(intake)
            changed['data']['manifest']['experiments'][0]['initial_inputs'][0]['n']=10.0
            with self.assertRaisesRegex(VerifierError,'manifest_binding'):verify_intake(reseal(changed))
            with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{"value":1}',b'')):
                report=pipeline.execute(intake)
            for replacement in (True,1.0):
                changed=copy.deepcopy(report)
                changed['data']['runs'][0]['execution']['result']['value']=replacement
                with self.assertRaisesRegex(VerifierError,'evidence_protocol_binding'):pipeline.verify_experiments(intake,reseal(changed))
            changed=copy.deepcopy(report);changed['data']['runs'][0]['request']['inputs']['n']=10.0
            with self.assertRaisesRegex(VerifierError,'experiment_source_binding'):pipeline.verify_experiments(intake,reseal(changed))
            for field,replacement in [('prior_runs',False),('allotted_seconds',20.0),('allotted_output_bytes',131072.0)]:
                changed=copy.deepcopy(report);changed['data'][field]=replacement
                with self.assertRaisesRegex(VerifierError,'experiment_budget_binding'):pipeline.verify_experiments(intake,reseal(changed))

    def test_yukon_utf16_reason_length_and_largest_finite_float(self):
        with example() as (root,effective,_):
            judgment=read_json(root/'simulation.json')['initial_decision']['judgment']
            validate_judgment({**judgment,'reason':'😀'*2000,'scores':[1.7976931348623157e308]},effective['config'],['source:proof.md'])
            with self.assertRaisesRegex(VerifierError,'yukon_reason_length'):validate_judgment({**judgment,'reason':'😀'*2001},effective['config'],['source:proof.md'])


    def test_production_review_rejects_old_provider_and_wrong_endpoint_provenance(self):
        with example() as (root, _, intake):
            initial, first = direct(root, intake)
            for field, value in (('provider','openrouter'), ('endpoint','https://invalid.example')):
                changed = copy.deepcopy(first)
                changed['data']['provenance'][field] = value
                with self.subTest(field=field), self.assertRaisesRegex(VerifierError, 'untrusted_provider_provenance'):
                    pipeline.verify_initial_review(intake, initial, reseal(changed))

    def test_injected_openai_transport_remains_nonpublishable(self):
        with example() as (root, effective, intake):
            initial = pipeline.execute(intake)
            decision = read_json(root/'simulation.json')['initial_decision']
            from unittest.mock import Mock
            transport = Mock(); transport.request.return_value = completion(decision)
            first = pipeline.review(intake, initial, provider=OpenAI(
                effective['config']['provider'], transport=transport, api_key='test-key'))
            self.assertTrue(first['simulation'])
            with self.assertRaisesRegex(VerifierError, 'simulation_cannot_publish'):
                pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json')
            self.assertFalse((root/'score.json').exists())


    def test_reasoning_effort_binds_intake_and_review_provenance(self):
        for model in ('gpt-6-astra', 'gpt-6.1-sol'):
            with self.subTest(model=model), example(mutate=lambda c:c['provider'].update(
                    model=model, reasoning_effort='high')) as (root, effective, intake):
                self.assertEqual(intake['bindings']['config'], digest(effective))
                without_effort = copy.deepcopy(effective)
                del without_effort['config']['provider']['reasoning_effort']
                self.assertNotEqual(intake['bindings']['config'], digest(without_effort))
                initial, first = direct(root, intake)
                config = effective['config']['provider']
                self.assertEqual(first['data']['provenance']['provider_config_sha256'], digest(config))
                pipeline.dossier(intake, initial, first)
                for effort in (None, 'medium', 'xhigh'):
                    other_config = dict(config)
                    if effort is None:
                        del other_config['reasoning_effort']
                    else:
                        other_config['reasoning_effort'] = effort
                    tampered = copy.deepcopy(first)
                    tampered['data']['provenance']['provider_config_sha256'] = digest(other_config)
                    with self.subTest(effort=effort), self.assertRaisesRegex(VerifierError, 'review_provenance_binding'):
                        pipeline.dossier(intake, initial, reseal(tampered))
