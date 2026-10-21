"""Consume actual Yukon route-test snapshots with an installed verifier, offline.

Only the HTTP worker and model response are mocked. The real decoding, intake,
review, phase replay, export and publication receipt code runs for each source.
The test consumer explicitly opts in; historical resolver fixtures do not establish
the original candidate admission. Native evaluation fixtures test that separately.
"""
import argparse
import json
from pathlib import Path
from unittest.mock import Mock, patch

from llm_verifier import pipeline
from llm_verifier.artifacts import intake_snapshot, verify_intake, implementation
from llm_verifier.config import load_config
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import sha
from llm_verifier.prior import intake_imports
from tests.support import example, direct
from tests.test_prior_results import consuming_decision, reference_evaluation


def verify_pair(fixtures):
    from llm_verifier.prior import source_bundle
    pairs=[]
    for name in ('atomic-finite','atomic-finite-reaccepted'):
        root=Path(fixtures)/name
        snapshot=json.loads((root/'snapshot.json').read_bytes())
        refs=json.loads((root/'references.json').read_bytes())
        assert len(snapshot['sources'])==1 and len(snapshot['references'])==1 and len(refs)==1
        source=snapshot['sources'][0]
        sid=source['submission_id']
        assert source['accepted'] is True
        assert snapshot['references'][0]['submission_id']==sid==refs[0]['submission_id']
        assert snapshot['references'][0]['bundle_sha256']==source['bundle']['sha256']
        bundle=source_bundle(source)
        pairs.append((snapshot,source,bundle))
    before,after=pairs
    a,b=before[1],after[1]
    original,renewed=before[2]['source'],after[2]['source']
    assert a['submission_id']==b['submission_id'], 'Different immutable submission IDs: this is not a stable-reference reacceptance pair'
    assert a['benchmark_id']==b['benchmark_id'] and a['challenge_id']==b['challenge_id']
    assert a['submitted_at']==b['submitted_at']
    assert original['submission_sha256']==renewed['submission_sha256']
    assert original['manifest']==renewed['manifest'], 'Original manifest record changed'
    assert original['manifest']['text'].encode('utf-8')==renewed['manifest']['text'].encode('utf-8')
    assert a['evaluation_id']!=b['evaluation_id'], 'Native accepting evaluation did not change'
    assert a['validity_result_id']!=b['validity_result_id'], 'Native accepting validity result did not change'
    assert a['verifier_evaluation_id']!=b['verifier_evaluation_id'], 'Verifier accepting execution did not change'
    assert original['evaluation_id']!=renewed['evaluation_id']
    assert a['bundle']['sha256']!=b['bundle']['sha256']
    return {'submission_id':a['submission_id'],'source_sha256':original['submission_sha256'],
        'manifest_sha256':original['manifest']['sha256'],'raw_manifest_bytes_equal':True,
        'native_evaluations':[a['evaluation_id'],b['evaluation_id']],
        'validity_results':[a['validity_result_id'],b['validity_result_id']],
        'verifier_evaluations':[a['verifier_evaluation_id'],b['verifier_evaluation_id']],
        'artifact_sha256':[a['bundle']['sha256'],b['bundle']['sha256']],
        'response_sha256':[sha((Path(fixtures)/name/'snapshot.json').read_bytes())
            for name in ('atomic-finite','atomic-finite-reaccepted')]}


def verify(fixtures, raw_sources=None):
    pair=verify_pair(fixtures)
    captured={str(p.relative_to(fixtures)):sha(p.read_bytes()) for p in sorted(Path(fixtures).rglob('*.json'))}
    report={'runtime':implementation()['content_sha256'],'model_called':False,
            'admission_scope':'Explicit enabled test consumer; not historical native admission verification','cases':{}}
    cases=sorted(p for p in Path(fixtures).iterdir() if p.is_dir() and (p/'snapshot.json').is_file())
    assert cases, 'Actual server-produced current-format snapshots are required'
    for root in cases:
        response_bytes=(root/'snapshot.json').read_bytes()
        snapshot=read_json(root/'snapshot.json');context=read_json(root/'context.json');refs=read_json(root/'references.json')
        with example() as (candidate,_,_):
            manifest=read_json(candidate/'submission/submission.json');manifest['prior_results']=refs
            write_json(candidate/'submission/submission.json',manifest)
            effective=load_config(candidate,'verifier.json','submission',reference_evaluation(candidate))
            worker=Mock(returncode=0);worker.communicate.return_value=(b'200\n'+response_bytes,b'');worker.poll.return_value=0
            with patch('llm_verifier.accepted.subprocess.Popen',return_value=worker),patch.dict('os.environ',{
                'ACTIONS_ID_TOKEN_REQUEST_URL':'https://oidc.invalid/token','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'offline-fixture-only'}):
                intake=intake_snapshot(effective,candidate/'submission',accepted_results_context=context)
            verify_intake(intake)
            records,ancestors=intake_imports(snapshot,manifest,intake['bindings']['evaluation_id'],intake['data']['created_at_ns'])
            initial,first=direct(candidate,intake,consuming_decision(candidate))
            dossier=pipeline.finalize(intake,initial,first,candidate/'score.json',candidate/'dossier.json',results_path=candidate/'accepted-results.json')
            for record,source in zip(records,sorted(snapshot['sources'],key=lambda s:s['submission_id'])):
                from llm_verifier.prior import source_bundle
                bundle=source_bundle(source)
                assert record['manifest']==bundle['source']['manifest']
                raw=source['bundle']['text'].encode()
                if raw_sources:
                    assert any(p.read_bytes()==raw for p in Path(raw_sources).glob('*.json')), 'Source bytes must match original exported artifact'
            exported=read_json(candidate/'accepted-results.json')
            assert exported['source']['manifest']==intake['data']['files']['submission.json']
            assert read_json(candidate/'accepted-results-receipt.json')=={'version':1,'snapshot_id':snapshot['snapshot_id']}
            report['cases'][root.name]={'response_sha256':sha(response_bytes),'raw_sha256':snapshot['sources'][0]['bundle']['sha256'],
                'byte_roundtrip':True,'response_decoded':True,'ancestry_count':len(ancestors),
                'submission_ids':[r['submission_id'] for r in records],
                'source_digests':[r['provenance']['original_source']['submission_sha256'] for r in records],
                'manifest_sha256':[r['manifest']['sha256'] for r in records],
                'downstream_valid':dossier['data']['judgment']['valid'],'receipt_checked':True}
    assert pair==verify_pair(fixtures)
    assert captured=={str(p.relative_to(fixtures)):sha(p.read_bytes()) for p in sorted(Path(fixtures).rglob('*.json'))}, 'Evidence changed during verification'
    for name,index in [('atomic-finite',0),('atomic-finite-reaccepted',1)]:
        assert report['cases'][name]['submission_ids']==[pair['submission_id']]
        assert report['cases'][name]['source_digests']==[pair['source_sha256']]
        assert report['cases'][name]['manifest_sha256']==[pair['manifest_sha256']]
        assert report['cases'][name]['response_sha256']==pair['response_sha256'][index]
    report['stable_reacceptance_pair']=pair
    report['input_file_sha256']=captured
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures',required=True);parser.add_argument('--raw-sources');parser.add_argument('--out',required=True)
    args=parser.parse_args();write_json(args.out,verify(args.fixtures,args.raw_sources))
    print('Actual Yukon snapshots passed installed intake, review, export and receipt checks.')
