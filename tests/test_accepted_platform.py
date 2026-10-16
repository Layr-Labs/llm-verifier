"""Offline platform boundaries. Network/model fixtures never assert real acceptance."""

import copy
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from llm_verifier import accepted, pipeline
from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.cli import main
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest, sha
from llm_verifier.prior import intake_imports, references
from tests.support import example, direct, reseal
from tests.test_prior_results import CONTEXT, upstream, platform_snapshot, install, resolved_intake, declare, consuming_decision


class PlatformSnapshots(unittest.TestCase):
    def test_same_native_evaluator_across_distinct_submissions_is_eligible(self):
        with upstream() as (_, _, _, bundle), example() as (root, _, _):
            effective = install(root, bundle)
            snapshot = platform_snapshot([bundle])
            snapshot['sources'][0]['evaluation_id'] = CONTEXT['evaluation_id']
            intake = resolved_intake(effective, root/'submission', bundle, snapshot=snapshot)
            verify_intake(intake)
            self.assertEqual(intake['data']['prior_snapshot'], snapshot)

    def test_self_rejected_but_rejudgment_times_and_nonces_are_not_reference_order(self):
        with upstream() as (_, _, _, bundle), example() as (root, _, _):
            effective = install(root, bundle)
            snapshot=platform_snapshot([bundle]);snapshot['sources'][0]['submission_id']=CONTEXT['submission_id']
            with self.assertRaisesRegex(VerifierError,'self_or_cyclic'):
                resolved_intake(effective,root/'submission',bundle,snapshot=snapshot)
            snapshot=platform_snapshot([bundle]);snapshot['sources'][0]['completed_at']='2999-01-01T00:00:00Z'
            resolved_intake(effective,root/'submission',bundle,snapshot=snapshot)
            intake_imports(snapshot,read_json(root/'submission/submission.json'),bundle['source']['evaluation_id'],1)

    def test_snapshot_binding_rejects_substitution_extra_and_missing_sources(self):
        with upstream() as (_, _, _, bundle), example() as (root, _, _):
            effective = install(root, bundle)
            mutations = [lambda s: s['sources'][0].update(verifier_evaluation_id='f'*32),
                         lambda s: s['references'][0].update(bundle_sha256='f'*64),
                         lambda s: s['references'][0].update(submission_id='other'),
                         lambda s: s['sources'][0].update(submission_id='other'),
                         lambda s: s.update(sources=[]), lambda s: s.update(references=[]),
                         lambda s: s['sources'].append(copy.deepcopy(s['sources'][0])),
                         lambda s: s['sources'][0]['bundle'].update(bytes=True),
                         lambda s: s['sources'][0]['bundle'].update(format='unknown-v2'),
                         lambda s: s['sources'][0].update(accepted=False)]
            for mutate in mutations:
                snapshot = platform_snapshot([bundle]); mutate(snapshot)
                with self.subTest(mutate=mutate), self.assertRaises(VerifierError):
                    resolved_intake(effective, root/'submission', bundle, snapshot=snapshot)

    def test_every_transitive_ancestor_must_have_current_attestation(self):
        from tests.test_prior_results import rehash
        with upstream() as (_, _, _, original), example() as (root, _, _):
            bundle=copy.deepcopy(original)
            bundle['ancestors']=[{'submission_id':'ancestor'}]
            rehash(bundle); effective=install(root,bundle)
            snapshot=platform_snapshot([bundle])
            verify_intake(resolved_intake(effective,root/'submission',bundle,snapshot=snapshot))
            for mutate in (lambda s:s.update(ancestors=[]),lambda s:s['ancestors'][0].update(accepted=False),
                           lambda s:s['ancestors'][0].update(submission_id=CONTEXT['submission_id']),
                           lambda s:s['ancestors'].append(copy.deepcopy(s['ancestors'][0]))):
                changed=copy.deepcopy(snapshot);mutate(changed)
                with self.assertRaises(VerifierError):resolved_intake(effective,root/'submission',bundle,snapshot=changed)

    def test_raw_text_and_large_integer_precision_preserved(self):
        with upstream() as (_, _, _, bundle), example() as (root, _, _):
            effective=install(root,bundle)
            raw=canonical(bundle).decode()+'\n\t'
            snapshot=platform_snapshot([bundle],raw_texts=[raw])
            intake=resolved_intake(effective,root/'submission',bundle,snapshot=snapshot)
            self.assertGreater(bundle['source']['finalized_at_ns'],2**53)
            self.assertEqual(intake['data']['prior_snapshot']['sources'][0]['bundle']['text'],raw)
            bad=copy.deepcopy(intake)
            bad['data']['prior_snapshot']['sources'][0]['bundle']['text']=canonical(bundle).decode()
            with self.assertRaisesRegex(VerifierError,'prior_import_pin'):verify_intake(reseal(bad))

    def test_no_context_no_fallback_and_no_service_call_without_reference_or_context(self):
        with example() as (root,effective,_):
            with patch('llm_verifier.accepted.resolve') as resolver:
                intake_snapshot(effective,root/'submission')
                resolver.assert_not_called()
            m=read_json(root/'submission/submission.json');m['prior_results']=[{'submission_id':'earlier'}]
            write_json(root/'submission/submission.json',m)
            with self.assertRaisesRegex(VerifierError,'platform_context_required'):intake_snapshot(effective,root/'submission')

    def test_empty_reference_receipt_and_invalid_publication(self):
        with example() as (root,effective,_):
            snapshot=platform_snapshot([])
            with patch('llm_verifier.accepted.resolve',return_value=snapshot) as resolver:
                intake=intake_snapshot(effective,root/'submission',accepted_results_context=CONTEXT)
            resolver.assert_called_once_with(CONTEXT,[])
            d=read_json(root/'simulation.json')['initial_decision'];d['judgment'].update(valid=False)
            initial,first=direct(root,intake,d)
            pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted-results.json')
            self.assertFalse((root/'accepted-results.json').exists())
            self.assertEqual(read_json(root/'accepted-results-receipt.json'),{'version':1,'snapshot_id':snapshot['snapshot_id']})
            with self.assertRaisesRegex(VerifierError,'receipt_output_required'):
                pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json')
            self.assertFalse((root/'score.json').exists())

    def test_cli_automatic_context_and_safe_failure_clear_stale_intake(self):
        with upstream() as (_,_,_,bundle),example() as (root,_,_):
            install(root,bundle);snapshot=platform_snapshot([bundle])
            with patch.dict(os.environ,{'YUKON_ACCEPTED_RESULTS':canonical(CONTEXT).decode()}),patch('llm_verifier.accepted.resolve',return_value=snapshot):
                self.assertEqual(main(['intake','--root',str(root),'--out',str(root/'intake.json')]),0)
            value=read_json(root/'intake.json');verify_intake(value)
            raw=canonical(value).decode()
            self.assertNotIn(CONTEXT['api'],raw);self.assertNotIn(CONTEXT['audience'],raw)
            with patch.dict(os.environ,{'YUKON_ACCEPTED_RESULTS':canonical(CONTEXT).decode()}),patch('llm_verifier.accepted.resolve',side_effect=InfrastructureError('prior_dependency_changed')):
                self.assertEqual(main(['intake','--root',str(root),'--out',str(root/'intake.json')]),2)
            self.assertFalse((root/'intake.json').exists())

    def test_replay_checks_snapshot_consumer_and_output_receipt_alias(self):
        with example() as (root,effective,_):
            snapshot=platform_snapshot([])
            with patch('llm_verifier.accepted.resolve',return_value=snapshot):
                intake=intake_snapshot(effective,root/'submission',accepted_results_context=CONTEXT)
            bad=copy.deepcopy(intake);bad['data']['prior_snapshot']['consumer']['job_id']='different'
            with self.assertRaisesRegex(VerifierError,'consumer_binding'):verify_intake(reseal(bad))
            write_json(root/'intake.json',intake)
            self.assertEqual(main(['finalize','--intake',str(root/'intake.json'),'--initial',str(root/'initial.json'),
                '--initial-review',str(root/'review.json'),'--score',str(root/'score.json'),'--dossier',str(root/'dossier.json'),
                '--receipt',str(root/'intake.json')]),2)
            self.assertEqual(read_json(root/'intake.json'),intake)


class PlatformTransportTests(unittest.TestCase):
    def test_request_contract_automatic_oidc_and_consumer_binding(self):
        snapshot=platform_snapshot([])
        env={'ACTIONS_ID_TOKEN_REQUEST_URL':'https://actions.invalid/token?api-version=2','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'workflow-key'}
        with patch.dict(os.environ,env),patch.object(accepted.PlatformTransport,'request',return_value=snapshot) as request:
            self.assertEqual(accepted.resolve(CONTEXT,[]),snapshot)
        args=request.call_args.args
        self.assertEqual(args[1],canonical({'job_id':CONTEXT['job_id'],'references':[]}))
        wrong=copy.deepcopy(snapshot);wrong['consumer']['job_id']='wrong'
        with patch.dict(os.environ,env),patch.object(accepted.PlatformTransport,'request',return_value=wrong):
            with self.assertRaisesRegex(VerifierError,'consumer_binding'):accepted.resolve(CONTEXT,[])
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(VerifierError,'identity_unavailable'):accepted.resolve(CONTEXT,[])

    def test_oidc_exchange_uses_separate_header_tokens_and_no_redirect(self):
        response=canonical(platform_snapshot([]))
        replies=[(200,canonical({'value':'oidc-token'})),(200,response)]
        with patch('llm_verifier.accepted._http',side_effect=replies) as http:
            status,raw=accepted._exchange(CONTEXT,b'{}','https://actions.invalid/token?audience=old','request-key',15)
        self.assertEqual((status,raw),(200,response))
        first,second=http.call_args_list
        self.assertIn('audience=yukon%3Aaccepted-results%3Atest',first.args[0])
        self.assertEqual(first.args[2]['Authorization'],'Bearer request-key')
        self.assertEqual(second.args[2]['Authorization'],'Bearer oidc-token')
        self.assertEqual(second.args[0],CONTEXT['api']+'/api/internal/accepted-results/resolve')
        with patch('llm_verifier.accepted._http',return_value=(302,b'')) as http:
            with self.assertRaisesRegex(InfrastructureError,'identity_unavailable'):
                accepted._exchange(CONTEXT,b'{}','https://actions.invalid/token','key',15)
            self.assertEqual(http.call_count,1)

    def test_only_explicit_service_unavailable_retries_with_identical_request(self):
        success=canonical(platform_snapshot([]))
        replies=[(200,canonical({'value':'oidc-token'})),(503,canonical({'error':{'code':'service_unavailable'}})),(200,success)]
        with patch('llm_verifier.accepted._http',side_effect=replies) as http,patch('llm_verifier.accepted.time.sleep') as sleep:
            self.assertEqual(accepted._exchange(CONTEXT,b'{}','https://actions.invalid/token','key',15),(200,success))
        self.assertEqual(http.call_count,3)
        self.assertEqual(http.call_args_list[1],http.call_args_list[2])
        sleep.assert_called_once_with(0.5)
        for status,code in [(409,'dependency_changed'),(409,'conflict'),(403,'forbidden'),(404,'unavailable'),(422,'unsupported_representation'),(503,'forbidden')]:
            replies=[(200,canonical({'value':'oidc-token'})),(status,canonical({'error':{'code':code}}))]
            with patch('llm_verifier.accepted._http',side_effect=replies) as http,patch('llm_verifier.accepted.time.sleep') as sleep:
                self.assertEqual(accepted._exchange(CONTEXT,b'{}','https://actions.invalid/token','key',15)[0],status)
            self.assertEqual(http.call_count,2);sleep.assert_not_called()

    def test_retry_attempts_are_bounded_by_count_and_shared_deadline(self):
        replies=[(200,canonical({'value':'oidc-token'}))]+[(503,canonical({'error':{'code':'service_unavailable'}}))]*4
        with patch('llm_verifier.accepted._http',side_effect=replies) as http,patch('llm_verifier.accepted.time.sleep') as sleep:
            self.assertEqual(accepted._exchange(CONTEXT,b'{}','https://actions.invalid/token','key',15)[0],503)
        self.assertEqual(http.call_count,5);self.assertEqual(sleep.call_count,3)
        self.assertEqual({call.args[-2] for call in http.call_args_list}, {http.call_args_list[0].args[-2]})

    def test_atomic_discovery_cli_prints_json_without_import_authority(self):
        import contextlib,io,json
        with upstream() as (_,_,_,bundle),example() as (root,_,_):
            source=platform_snapshot([bundle])['sources'][0];value={'version':1,**source}
            write_json(root/'discovery.json',value)
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(['inspect-results','--discovery',str(root/'discovery.json')]),0)
            shown=json.loads(stdout.getvalue())
            self.assertFalse(shown['authorizes_intake'])
            self.assertEqual(shown['manifest'],bundle['source']['manifest'])

    def test_endpoint_and_candidate_reference_validation(self):
        for url in ['http://host','https://user:secret@host','https://host/path','https://host?query=1','https://host/#frag','https://host\n']:
            context={**CONTEXT,'api':url}
            with self.subTest(url=url),self.assertRaises(VerifierError):accepted.context_identity(context)
        for refs in [['result:'+'a'*64],[{'submission_id':'https://host','result_id':'result:'+'a'*64}],
                     [{'submission_id':'id','result_id':'result:'+'a'*64,'path':'claim.json'}]]:
            with self.assertRaises(VerifierError):references(refs)

    def test_worker_deadline_is_killable_and_credentials_not_in_argv_env(self):
        real_popen=subprocess.Popen;children=[]
        def spawn(*args,**kwargs):
            self.assertNotIn('request-key',str(args));self.assertNotIn('request-key',str(kwargs.get('env')))
            self.assertEqual(set(kwargs['env']),{'PATH','LANG'})
            child=real_popen(*args,**kwargs);children.append(child);return child
        start=time.monotonic()
        with patch('llm_verifier.accepted._BOOTSTRAP','import time; time.sleep(30)'),patch('llm_verifier.accepted.TIMEOUT',0.05),patch('llm_verifier.accepted.subprocess.Popen',side_effect=spawn):
            with self.assertRaisesRegex(InfrastructureError,'transport_timeout'):
                accepted.PlatformTransport().request(CONTEXT,b'{}','https://actions.invalid/token','request-key')
        self.assertLess(time.monotonic()-start,2)
        self.assertIsNotNone(children[0].poll())
