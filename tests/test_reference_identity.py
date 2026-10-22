"""UUID aliases identify one submission without rewriting authenticated evidence."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from llm_verifier import accepted, pipeline
from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.cli import main
from llm_verifier.config import load_config
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, sha
from llm_verifier.judgment import validate_judgment
from llm_verifier.prior import intake_imports, references, source_bundle, validate_bundle
from tests.support import direct, example, reseal
from tests.test_prior_results import reference_evaluation, rehash

FIXTURE = Path(__file__).parent / 'fixtures/uppercase-reference.json'
SID = 'a099c9c8-0fff-4a0f-b8a4-0fdcf0cfc200'
OTHER = 'bbbbbbbb-bbbb-4bbb-abbb-bbbbbbbbbbbb'
ANCESTOR = 'dddddddd-dddd-4ddd-addd-dddddddddddd'
ENV = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://oidc.invalid/token',
       'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'offline-fixture-only'}


def native_snapshot():
    return json.loads(FIXTURE.read_bytes())['snapshot']


def context_for(snapshot):
    return {**snapshot['consumer'], 'version': 1, 'api': 'https://fixture.invalid',
            'audience': 'yukon:accepted-results:test'}


def declarations(*ids):
    return {'prior_results': [{'submission_id': sid} for sid in ids]}


def imports(snapshot, *ids):
    return intake_imports(snapshot, declarations(*ids), 'e'*32, 1)


def worker_for(snapshot):
    worker = Mock(returncode=0)
    worker.communicate.return_value = (b'200\n' + canonical(snapshot), b'')
    worker.poll.return_value = 0
    return worker


def replace_bundle(snapshot, index, bundle):
    raw = canonical(rehash(bundle))
    snapshot['sources'][index]['bundle'].update(text=raw.decode(), bytes=len(raw), sha256=sha(raw))
    snapshot['references'][index]['bundle_sha256'] = sha(raw)


class ReferenceIdentity(unittest.TestCase):
    def test_native_uppercase_and_mixed_cli_retry_preserve_original_bytes_through_export(self):
        fixture_bytes = FIXTURE.read_bytes()
        snapshot = native_snapshot()
        wire = json.loads(fixture_bytes)
        original_source = source_bundle(snapshot['sources'][0])['source']['manifest']
        spellings = [SID, wire['requested'][0]['submission_id'], SID[:18].upper() + SID[18:]]
        for spelling in spellings:
            with self.subTest(spelling=spelling), example() as (root, _, _):
                manifest = read_json(root/'submission/submission.json')
                manifest.update(declarations(spelling))
                raw = (' \n' + json.dumps(manifest, indent=3) + '\n\n').encode()
                (root/'submission/submission.json').write_bytes(raw)
                env = {**ENV, 'YUKON_ACCEPTED_RESULTS': canonical(context_for(snapshot)).decode(),
                       'YUKON_EVALUATION': canonical(reference_evaluation(root)).decode()}
                for attempt in range(2):
                    target = root/'intake.json'
                    target.write_text('stale')
                    worker = worker_for(snapshot)
                    with patch('llm_verifier.accepted.subprocess.Popen', return_value=worker), \
                            patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(['intake', '--root', str(root), '--out', str(target)]), 0)
                    intake = read_json(target)
                    verify_intake(intake)
                    self.assertEqual(intake['data']['manifest'], manifest)
                    self.assertEqual(intake['data']['files']['submission.json']['text'].encode(), raw)
                    self.assertEqual(intake['data']['prior_snapshot'], snapshot)
                    payload = json.loads(worker.communicate.call_args_list[0].args[0])
                    self.assertEqual(json.loads(payload['body'])['references'], declarations(SID)['prior_results'])
                    for evidence in (spelling, SID):
                        decision = read_json(root/'simulation.json')['initial_decision']
                        decision['judgment']['evidence_refs'] = ['prior:' + evidence]
                        initial, reviewed = direct(root, intake, decision)
                        messages, _, refs = pipeline.review_material(intake, initial)
                        self.assertIn('prior:' + SID, refs)
                        shown = json.loads(messages[1]['content'].split('\n', 1)[1])['prior_results'][0]
                        self.assertEqual(shown['manifest'], original_source)
                        dossier = pipeline.finalize(intake, initial, reviewed, root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
                        exported = read_json(root/'accepted.json')
                        self.assertEqual(exported['source']['manifest']['text'].encode(), raw)
                        self.assertEqual(exported['source']['manifest']['sha256'], sha(raw))
                        self.assertEqual(exported['source']['submission_sha256'], intake['bindings']['submission'])
                        self.assertEqual(exported['ancestors'], declarations(SID)['prior_results'])
                        self.assertEqual(dossier['data']['judgment']['evidence_refs'], ['prior:' + evidence])
                        self.assertEqual(read_json(root/'accepted-results-receipt.json'), {'version': 1, 'snapshot_id': snapshot['snapshot_id']})
        self.assertEqual(FIXTURE.read_bytes(), fixture_bytes)

    def test_uuid_alias_duplicates_fail_before_resolver_or_transport(self):
        values = declarations(SID, SID.upper())['prior_results']
        before = copy.deepcopy(values)
        with example() as (root, _, _):
            manifest = read_json(root/'submission/submission.json')
            manifest['prior_results'] = values
            write_json(root/'submission/submission.json', manifest)
            effective = load_config(root, 'verifier.json', 'submission', reference_evaluation(root))
            with patch('llm_verifier.accepted.resolve') as resolver, self.assertRaisesRegex(VerifierError, 'duplicate_prior_reference'):
                intake_snapshot(effective, root/'submission', accepted_results_context=context_for(native_snapshot()))
            resolver.assert_not_called()
        with patch.object(accepted.PlatformTransport, 'request') as transport, self.assertRaisesRegex(VerifierError, 'duplicate_prior_reference'):
            accepted.resolve(context_for(native_snapshot()), values)
        transport.assert_not_called()
        self.assertEqual(values, before)
        original = declarations(SID.upper())['prior_results']
        normalized = references(original)
        self.assertEqual(normalized, declarations(SID)['prior_results'])
        self.assertEqual(original, declarations(SID.upper())['prior_results'])
        self.assertIsNot(normalized[0], original[0])

    def test_source_pin_aliases_preserve_snapshot_and_provenance(self):
        snapshot = native_snapshot()
        snapshot['sources'][0]['submission_id'] = SID.upper()
        before = copy.deepcopy(snapshot)
        records, ancestors = imports(snapshot, SID.upper())
        self.assertEqual(records[0]['submission_id'], SID)
        self.assertEqual(records[0]['provenance']['source']['submission_id'], SID.upper())
        self.assertEqual(ancestors, declarations(SID)['prior_results'])
        self.assertEqual(snapshot, before)
        for field, error in [('references', 'duplicate_prior_reference'), ('sources', 'duplicate_prior_submission')]:
            duplicate = copy.deepcopy(snapshot)
            second = copy.deepcopy(duplicate[field][0])
            second['submission_id'] = SID if second['submission_id'] != SID else SID.upper()
            duplicate[field].append(second)
            with self.subTest(field=field), self.assertRaisesRegex(VerifierError, error):
                imports(duplicate, SID)

    def test_consumer_alias_binding_preserves_raw_context_and_snapshot(self):
        snapshot = native_snapshot()
        snapshot.update(references=[], sources=[], ancestors=[])
        context = context_for(snapshot)
        context['submission_id'] = context['submission_id'].upper()
        before = copy.deepcopy(snapshot)
        with example() as (root, effective, _):
            with patch.dict(os.environ, ENV), patch.object(accepted.PlatformTransport, 'request', return_value=snapshot):
                intake = intake_snapshot(effective, root/'submission', accepted_results_context=context)
            verify_intake(intake)
            self.assertEqual(intake['data']['prior_snapshot'], before)
            self.assertEqual(intake['data']['context']['accepted_results']['submission_id'], context['submission_id'])
            for field in ('benchmark_id', 'evaluation_id', 'job_id'):
                bad = copy.deepcopy(intake)
                bad['data']['prior_snapshot']['consumer'][field] = bad['data']['prior_snapshot']['consumer'][field].upper()
                with self.subTest(field=field), self.assertRaisesRegex(VerifierError, 'consumer_binding'):
                    verify_intake(reseal(bad))
            bad_context = {**context, 'job_id': context['job_id'].upper()}
            with patch.dict(os.environ, ENV), patch.object(accepted.PlatformTransport, 'request', return_value=snapshot), self.assertRaisesRegex(VerifierError, 'consumer_binding'):
                accepted.resolve(bad_context, [])
        self.assertEqual(snapshot, before)

    def test_self_and_consumer_ancestor_aliases_are_rejected(self):
        for location in ('source', 'ancestor-self', 'ancestor-consumer'):
            snapshot = native_snapshot()
            if location == 'source':
                snapshot['consumer']['submission_id'] = SID.upper()
            else:
                bundle = source_bundle(snapshot['sources'][0])
                sid = SID if location == 'ancestor-self' else snapshot['consumer']['submission_id']
                bundle['ancestors'] = declarations(sid.upper())['prior_results']
                replace_bundle(snapshot, 0, bundle)
            with self.subTest(location=location), self.assertRaisesRegex(VerifierError, 'self_or_cyclic_import'):
                imports(snapshot, SID)

    def test_cycle_graph_joins_uuid_aliases(self):
        snapshot = native_snapshot()
        second = copy.deepcopy(snapshot['sources'][0])
        second['submission_id'] = OTHER
        snapshot['sources'].append(second)
        snapshot['references'].append({'submission_id': OTHER.upper(), 'bundle_sha256': second['bundle']['sha256']})
        for index, ancestor in [(0, OTHER.upper()), (1, SID.upper())]:
            bundle = source_bundle(snapshot['sources'][index])
            bundle['ancestors'] = declarations(ancestor)['prior_results']
            replace_bundle(snapshot, index, bundle)
        with self.assertRaisesRegex(VerifierError, 'self_or_cyclic_import'):
            imports(snapshot, SID.upper(), OTHER)

    def test_declared_and_attested_ancestor_aliases_bind_without_rewriting_source(self):
        snapshot = native_snapshot()
        bundle = source_bundle(snapshot['sources'][0])
        manifest = json.loads(bundle['source']['manifest']['text'])
        manifest.update(declarations(ANCESTOR.upper()))
        raw = (json.dumps(manifest, indent=3) + '\n\n').encode()
        bundle['source']['manifest'] = {'text': raw.decode(), 'size': len(raw), 'sha256': sha(raw)}
        bundle['ancestors'] = declarations(ANCESTOR)['prior_results']
        replace_bundle(snapshot, 0, bundle)
        snapshot['ancestors'] = [{'submission_id': ANCESTOR.upper(), 'accepted': True}]
        before = copy.deepcopy(snapshot)
        records, ancestors = imports(snapshot, SID.upper())
        self.assertEqual(records[0]['manifest']['text'].encode(), raw)
        self.assertEqual({a['submission_id'] for a in ancestors}, {SID, ANCESTOR})
        self.assertEqual(snapshot, before)
        snapshot['ancestors'].append({'submission_id': ANCESTOR, 'accepted': True})
        with self.assertRaisesRegex(VerifierError, 'duplicate_prior_reference'):
            imports(snapshot, SID)
        bundle['ancestors'].append({'submission_id': ANCESTOR.upper()})
        with self.assertRaisesRegex(VerifierError, 'duplicate_prior_reference'):
            validate_bundle(rehash(bundle))

    def test_only_native_uuid_dialect_aliases_and_opaque_ids_remain_case_sensitive(self):
        for version in '12345678':
            sid = SID[:14] + version + SID[15:]
            self.assertEqual(references(declarations(sid.upper())['prior_results']), declarations(sid)['prior_results'])
        opaque = ['Opaque-Id', SID.replace('-', ''), SID[:14]+'9'+SID[15:], SID[:19]+'7'+SID[20:],
                  'ffffffff-ffff-ffff-ffff-ffffffffffff']
        for sid in opaque:
            with self.subTest(sid=sid):
                ids = [sid.lower(), sid.upper()]
                self.assertEqual({r['submission_id'] for r in references(declarations(*ids)['prior_results'])}, set(ids))
                snapshot = native_snapshot()
                snapshot['references'][0]['submission_id'] = ids[0]
                snapshot['sources'][0]['submission_id'] = ids[0]
                with self.assertRaisesRegex(VerifierError, 'reference_snapshot_binding'):
                    imports(snapshot, ids[1])
                snapshot['sources'][0]['submission_id'] = ids[1]
                with self.assertRaisesRegex(VerifierError, 'reference_snapshot_binding'):
                    imports(snapshot, ids[0])
        for sid in ('{' + SID + '}', 'urn:uuid:' + SID):
            with self.subTest(sid=sid), self.assertRaisesRegex(VerifierError, 'platform_identity'):
                references(declarations(sid)['prior_results'])

    def test_prior_evidence_alias_membership_and_duplicates_preserve_other_namespaces(self):
        with example() as (root, effective, _):
            judgment = read_json(root/'simulation.json')['initial_decision']['judgment']
            for given, allowed in [(SID, SID.upper()), (SID.upper(), SID)]:
                judgment['evidence_refs'] = ['prior:' + given]
                before = copy.deepcopy(judgment)
                self.assertEqual(validate_judgment(judgment, effective['config'], ['prior:' + allowed]), before)
            judgment['evidence_refs'] = ['prior:' + SID, 'prior:' + SID.upper()]
            with self.assertRaisesRegex(VerifierError, 'duplicate_judgment_item'):
                validate_judgment(judgment, effective['config'], ['prior:' + SID])
            for given, allowed in [('prior:Opaque', 'prior:opaque'), ('source:' + SID.upper(), 'source:' + SID), (SID.upper(), SID)]:
                judgment['evidence_refs'] = [given]
                with self.subTest(given=given), self.assertRaisesRegex(VerifierError, 'unknown_evidence_reference'):
                    validate_judgment(judgment, effective['config'], [allowed])
                judgment['evidence_refs'] = [given, allowed]
                validate_judgment(judgment, effective['config'], [given, allowed])
