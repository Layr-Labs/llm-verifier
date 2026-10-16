"""Atomic source export/reuse and immutable phase boundaries, all offline."""
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier import pipeline
from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.cli import main
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import InfrastructureError, VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest, sha
from llm_verifier.prior import FORMAT, MAX_BUNDLE_BYTES, intake_imports, validate_bundle
from llm_verifier.simulation import simulate
from tests.support import direct, example, reseal, scripted_model

CONTEXT = {'version':1,'api':'https://yukon.invalid','audience':'yukon:accepted-results:test',
           'submission_id':'consumer','benchmark_id':'benchmark','challenge_id':None,
           'evaluation_id':'evaluation','job_id':'job'}


def rehash(bundle):
    bundle['sha256']=digest({k:v for k,v in bundle.items() if k!='sha256'})
    return bundle


@contextmanager
def upstream():
    with example() as (root,_,intake):
        initial,first=direct(root,intake)
        result=pipeline.dossier(intake,initial,first)
        yield root,intake,result,pipeline.export_results(result)


def platform_snapshot(bundles, raw_texts=None):
    sources,refs,ancestors=[],[],set()
    for i,bundle in enumerate(bundles):
        sid=f'source-{i}'
        raw=(raw_texts[i].encode() if raw_texts else canonical(bundle))
        sources.append({'submission_id':sid,'benchmark_id':'benchmark','challenge_id':None,
            'evaluation_id':'evaluation','validity_result_id':f'validity-{i}',
            'verifier_evaluation_id':bundle['source']['evaluation_id'],'accepted':True,
            'submitted_at':'2020-01-01T00:00:00Z','completed_at':'2026-10-16T00:00:00Z',
            'bundle':{'format':FORMAT,'sha256':sha(raw),'bytes':len(raw),'text':raw.decode()}})
        refs.append({'submission_id':sid,'bundle_sha256':sha(raw)})
        ancestors.update(a['submission_id'] for a in bundle['ancestors'])
    ancestors-={s['submission_id'] for s in sources}
    return {'version':1,'snapshot_id':'snapshot','consumer':{k:CONTEXT[k] for k in ('submission_id','benchmark_id','challenge_id','evaluation_id','job_id')},
            'references':refs,'sources':sources,'ancestors':[{'submission_id':a,'accepted':True} for a in sorted(ancestors)]}


def declare(root, snapshot):
    manifest=read_json(root/'submission/submission.json')
    manifest['prior_results']=[{'submission_id':r['submission_id']} for r in snapshot['references']]
    write_json(root/'submission/submission.json',manifest)


def install(root,bundle):
    declare(root,platform_snapshot([bundle]))
    return load_config(root,'verifier.json','submission')


def resolved_intake(effective,candidate,bundle,snapshot=None,simulation=False):
    snapshot=platform_snapshot([bundle]) if snapshot is None else snapshot
    with patch('llm_verifier.accepted.resolve',return_value=snapshot):
        return intake_snapshot(effective,candidate,simulation=simulation,accepted_results_context=CONTEXT)


def consuming_decision(root, *_):
    decision=read_json(root/'simulation.json')['initial_decision']
    decision['judgment']['reason']='Every original declared claim follows under its stated assumptions; prior results supply only their declared claims.'
    decision['judgment']['evidence_refs']+=['prior:'+r['submission_id'] for r in read_json(root/'submission/submission.json').get('prior_results',[])]
    return decision


@contextmanager
def downstream():
    with upstream() as (_,_,_,bundle),example() as (root,_,_):
        effective=install(root,bundle)
        yield root,effective,resolved_intake(effective,root/'submission',bundle),bundle,consuming_decision(root)


class AtomicPriorResults(unittest.TestCase):
    def test_original_manifest_bytes_are_the_only_claim_set(self):
        with downstream() as (root,_,intake,bundle,decision):
            initial,first=direct(root,intake,decision)
            exported=pipeline.export_results(pipeline.dossier(intake,initial,first))
            self.assertEqual(exported['source']['manifest'],intake['data']['files']['submission.json'])
            self.assertEqual(exported['source']['submission_sha256'],intake['bindings']['submission'])
            self.assertEqual(exported['ancestors'],[{'submission_id':'source-0'}])
            self.assertEqual(set(exported['judgment']),{'valid','reason'})
            self.assertNotIn('results',exported)
            messages,_,refs=pipeline.review_material(intake,initial)
            self.assertIn('prior:source-0',refs)
            import json
            shown=json.loads(messages[1]['content'].split('\n',1)[1])['prior_results'][0]
            self.assertEqual(shown['manifest'],bundle['source']['manifest'])
            self.assertNotIn('claims',shown)

    def test_successful_reacceptance_changes_provenance_not_submission_reference(self):
        with upstream() as (root,old,result,bundle):
            effective=copy.deepcopy(old['data']['effective'])
            # Current same-format origins remain compatible across policy/model updates.
            effective['config']['provider']['model']='new-model'
            fresh=intake_snapshot(effective,root/'submission')
            initial,first=direct(root,fresh)
            newer=pipeline.export_results(pipeline.dossier(fresh,initial,first))
            self.assertEqual(bundle['source']['manifest'],newer['source']['manifest'])
            self.assertEqual(bundle['source']['submission_sha256'],newer['source']['submission_sha256'])
            self.assertNotEqual(bundle['source']['evaluation_id'],newer['source']['evaluation_id'])
            with example() as (consumer,_,_):
                config=install(consumer,bundle)
                manifest=read_json(consumer/'submission/submission.json')
                for accepted in (bundle,newer):
                    resolved=resolved_intake(config,consumer/'submission',accepted)
                    self.assertEqual(resolved['data']['manifest'],manifest)

    def test_origin_policy_is_provenance_not_current_policy_equality(self):
        with upstream() as (_,_,_,bundle),example() as (root,_,_):
            bundle['policy_id']='earlier-supported-atomic-policy';rehash(bundle)
            config=install(root,bundle)
            verify_intake(resolved_intake(config,root/'submission',bundle))

    def test_atomic_rejection_exports_nothing_even_if_one_claim_is_true(self):
        with example() as (root,effective,_):
            manifest=read_json(root/'submission/submission.json');manifest['claims']=['1 + 1 = 2.','1 + 1 = 3.']
            write_json(root/'submission/submission.json',manifest)
            intake=intake_snapshot(effective,root/'submission')
            decision=read_json(root/'simulation.json')['initial_decision']
            decision['judgment'].update(valid=False,reason='The second declared claim is false; the whole set is rejected.')
            initial,first=direct(root,intake,decision)
            for name in ('score.json','dossier.json','accepted-results.json'):write_json(root/name,{'stale':True})
            result=pipeline.finalize(intake,initial,first,root/'score.json',root/'dossier.json',results_path=root/'accepted-results.json')
            self.assertFalse(result['data']['judgment']['valid'])
            self.assertFalse((root/'accepted-results.json').exists())
            with self.assertRaisesRegex(VerifierError,'final_accepted_required'):pipeline.export_results(result)

    def test_replay_and_original_bytes_cannot_be_substituted(self):
        with downstream() as (root,effective,intake,bundle,decision):
            initial,first=direct(root,intake,decision)
            raw=intake['data']['files']['submission.json']['text']
            (root/'submission/submission.json').write_text(raw+'\n')
            fresh=resolved_intake(effective,root/'submission',bundle)
            with self.assertRaisesRegex(VerifierError,'artifact_binding'):pipeline.verify_initial_review(fresh,initial,first)
            altered=copy.deepcopy(intake);altered['data']['prior_snapshot']['sources'][0]['bundle']['text']+=' '
            with self.assertRaisesRegex(VerifierError,'prior_import_pin'):verify_intake(reseal(altered))
        with upstream() as (_,_,result,bundle):
            modified=copy.deepcopy(result);modified['data']['judgment']['reason']='Replacement theorem.'
            with self.assertRaises(VerifierError):pipeline.export_results(reseal(modified))
            modified=copy.deepcopy(bundle);modified['source']['manifest']['text']+=' '
            with self.assertRaisesRegex(VerifierError,'manifest_digest'):validate_bundle(rehash(modified))

    def test_invalid_simulation_old_format_and_unknown_fields_fail_closed(self):
        with upstream() as (_,_,_,bundle),example() as (root,_,_):
            effective=install(root,bundle)
            for mutate in (lambda b:b.update(simulation=True),lambda b:b['judgment'].update(valid=False),
                           lambda b:b.update(results=[]),lambda b:b['judgment'].update(accepted_claims=['x']),
                           lambda b:b['source'].update(submission_sha256='f'*64)):
                changed=copy.deepcopy(bundle);mutate(changed)
                with self.assertRaises(VerifierError):validate_bundle(rehash(changed))
            for old in ('accepted-results-v1','legacy-dossier-v1'):
                snapshot=platform_snapshot([bundle]);snapshot['sources'][0]['bundle']['format']=old
                with self.assertRaisesRegex(VerifierError,'unsupported_representation'):resolved_intake(effective,root/'submission',bundle,snapshot)

    def test_simulation_export_and_downgrade_are_never_reusable(self):
        with example() as (root,effective,_):
            result=simulate(effective,root/'submission',read_json(root/'simulation.json'),root/'sim.json')
            with self.assertRaisesRegex(VerifierError,'final_accepted_required'):pipeline.export_results(result)
            result['simulation']=False
            with self.assertRaises(VerifierError):pipeline.export_results(reseal(result))

    def test_no_manual_import_registry_or_solver_urls(self):
        with upstream() as (_,_,_,bundle),example() as (root,_,_):
            effective=install(root,bundle);config=copy.deepcopy(effective['config'])
            config['prior_imports']=[{'path':'prior.json'}]
            with self.assertRaises(VerifierError):normalize(config)
            manifest=read_json(root/'submission/submission.json');manifest['prior_results'][0]['url']='https://attacker.invalid'
            write_json(root/'submission/submission.json',manifest)
            with patch('llm_verifier.accepted.resolve') as resolver,self.assertRaises(VerifierError):
                intake_snapshot(effective,root/'submission',accepted_results_context=CONTEXT)
            resolver.assert_not_called()

    def test_aggregate_source_bytes_and_ancestry_remain_bounded(self):
        with upstream() as (_,_,_,original):
            for count,pad,ancestors,error in [(5,True,0,'import_byte_limit'),(2,False,64,'aggregate_limit')]:
                bundles,texts=[],[]
                for i in range(count):
                    bundle=copy.deepcopy(original)
                    bundle['ancestors']=[{'submission_id':f'ancestor-{i}-{j}'} for j in range(ancestors)]
                    rehash(bundle);raw=canonical(bundle)
                    bundles.append(bundle);texts.append((raw+b' '*(MAX_BUNDLE_BYTES-len(raw)) if pad else raw).decode())
                snapshot=platform_snapshot(bundles,texts)
                manifest={'prior_results':[{'submission_id':r['submission_id']} for r in snapshot['references']]}
                with self.assertRaisesRegex(VerifierError,error):intake_imports(snapshot,manifest,'f'*32,1)

    def test_loaded_source_cycles_rejected_acyclic_diamond_allowed(self):
        with upstream() as (_,_,_,original):
            one,two=copy.deepcopy(original),copy.deepcopy(original)
            one['ancestors']=[{'submission_id':'source-1'}]
            two['ancestors']=[{'submission_id':'source-0'}]
            snapshot=platform_snapshot([rehash(one),rehash(two)])
            manifest={'prior_results':[{'submission_id':'source-0'},{'submission_id':'source-1'}]}
            with self.assertRaisesRegex(VerifierError,'self_or_cyclic'):
                intake_imports(snapshot,manifest,'f'*32,1)
            one['ancestors']=[{'submission_id':'source-1'},{'submission_id':'shared'}]
            two['ancestors']=[{'submission_id':'shared'}]
            snapshot=platform_snapshot([rehash(one),rehash(two)])
            records,ancestors=intake_imports(snapshot,manifest,'f'*32,1)
            self.assertEqual(len(records),2)
            self.assertEqual(ancestors,[{'submission_id':'shared'},{'submission_id':'source-0'},{'submission_id':'source-1'}])

    def test_export_alias_preserves_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'dossier.json';source.write_text('{}')
            alias=root/'alias.json';alias.symlink_to(source)
            hard=root/'hard.json';os.link(source,hard)
            for target in (source,alias,hard):
                self.assertEqual(main(['export-results','--dossier',str(source),'--out',str(target)]),2)
                self.assertEqual(source.read_text(),'{}')
