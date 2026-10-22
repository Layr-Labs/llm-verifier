"""Admission is immutable candidate policy, separate from mathematical acceptance."""
import copy
import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier import pipeline, scaffold, workflow
from llm_verifier.artifacts import intake_snapshot, verify_intake
from llm_verifier.cli import main
from llm_verifier.config import load_config, normalize, select_evaluation
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.jsonutil import canonical, digest
from llm_verifier.simulation import simulate
from tests.support import REPO, direct, example, fill_toy, reseal
from tests.test_prior_results import CONTEXT, consuming_decision, declare, platform_snapshot, reference_evaluation, upstream

MISSING = object()
ON = {'version': 1, 'submissionReferencesEnabled': True}
OFF = {'version': 1, 'submissionReferencesEnabled': False}


def evaluation(root, admission=MISSING, fields=None):
    value = reference_evaluation(root)
    if admission is MISSING:
        value.pop('admission')
    else:
        value['admission'] = copy.deepcopy(admission)
    if fields is not None:
        value['fields'] = fields
    return value


def effective(root, admission=MISSING, fields=None):
    return load_config(root, 'verifier.json', 'submission', evaluation(root, admission, fields))


class SubmissionAdmission(unittest.TestCase):
    def test_exact_public_dialect_rejects_unknowns_even_without_references(self):
        malformed = [True, False, 1, 1.0, 'true', [], {}, {'version': 1},
                     {'submissionReferencesEnabled': True}, {**ON, 'future': False}]
        malformed += [{**ON, 'version': value} for value in (True, False, None, '1', 1.0, 0, 2, -1, [], {})]
        malformed += [{**ON, 'submissionReferencesEnabled': value} for value in (None, 0, 1, 1.0, 'true', 'false', [], {})]
        with example() as (root, original, _), patch('llm_verifier.accepted.resolve') as resolver:
            for admission in malformed:
                with self.subTest(admission=admission), self.assertRaisesRegex(VerifierError, '^unsupported_admission_policy$'):
                    effective(root, admission)
            for key in ('submissionAdmission', 'submissionReferencesEnabled', 'admission'):
                with self.subTest(key=key), self.assertRaises(VerifierError):
                    normalize({**original['config'], key: ON})
        resolver.assert_not_called()

    def test_absence_null_and_false_keep_distinct_provenance_and_allow_empty_receipts(self):
        hashes = set()
        with example() as (root, _, _):
            for admission in (MISSING, None, OFF, ON):
                selected = evaluation(root, admission)
                config = effective(root, admission)
                self.assertEqual(config['evaluation'], selected)
                hashes.add(digest(config))
                snapshot = platform_snapshot([])
                with patch('llm_verifier.accepted.resolve', return_value=snapshot) as resolver:
                    intake = intake_snapshot(config, root/'submission', accepted_results_context=CONTEXT)
                resolver.assert_called_once_with(CONTEXT, [])
                self.assertEqual(intake['data']['effective']['evaluation'], selected)
                initial, first = direct(root, intake)
                dossier = pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
                self.assertTrue(dossier['data']['judgment']['valid'])
                self.assertEqual(read_json(root/'accepted-results-receipt.json'), {'version': 1, 'snapshot_id': 'snapshot'})
                self.assertEqual(read_json(root/'accepted.json')['ancestors'], [])
                self.assertEqual(read_json(root/'accepted.json')['source']['manifest'], intake['data']['files']['submission.json'])
        self.assertEqual(len(hashes), 4)

    def test_nonempty_disabled_is_refused_before_resolver_in_every_intake_mode(self):
        with upstream() as (_, _, _, source), example() as (root, _, _):
            snapshot = platform_snapshot([source]); declare(root, snapshot)
            for admission in (MISSING, None, OFF):
                for fields in ({'scores': True, 'validity': True}, {'scores': True, 'validity': False}, {'scores': False, 'validity': True}):
                    for simulation in (False, True):
                        for context in (None, CONTEXT):
                            with self.subTest(admission=admission, fields=fields, simulation=simulation, context=context), patch('llm_verifier.accepted.resolve') as resolver:
                                with self.assertRaisesRegex(VerifierError, '^submission_references_disabled$'):
                                    intake_snapshot(effective(root, admission, fields), root/'submission', simulation=simulation, accepted_results_context=context)
                                resolver.assert_not_called()
            with self.assertRaisesRegex(VerifierError, 'prior_platform_context_required'):
                intake_snapshot(effective(root, ON), root/'submission')

    def test_cli_refusal_clears_stale_intake_and_simulation_without_work(self):
        with example() as (root, _, _):
            declare(root, {'references': [{'submission_id': 'earlier'}]})
            for command in ('intake', 'simulate'):
                target = root/'out.json'; target.write_text('stale')
                argv = [command, '--root', str(root), '--out', str(target)]
                if command == 'simulate':
                    argv += ['--script', str(root/'simulation.json')]
                stderr = io.StringIO()
                with patch.dict(os.environ, {'YUKON_EVALUATION': canonical(evaluation(root, OFF)).decode(),
                                              'YUKON_ACCEPTED_RESULTS': canonical(CONTEXT).decode()}), \
                        patch('llm_verifier.accepted.resolve') as resolver, \
                        patch('llm_verifier.simulation.ScriptedRunner.run') as runner, \
                        patch('llm_verifier.simulation.ScriptedProvider.review') as reviewer, contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(argv), 2)
                self.assertIn('submission_references_disabled', stderr.getvalue())
                self.assertNotIn('prior_forbidden', stderr.getvalue())
                self.assertFalse(target.exists())
                resolver.assert_not_called(); runner.assert_not_called(); reviewer.assert_not_called()

    def test_resealed_intake_cannot_bypass_gate_before_any_later_work(self):
        with upstream() as (_, _, _, source), example() as (root, _, _):
            snapshot = platform_snapshot([source]); declare(root, snapshot)
            with patch('llm_verifier.accepted.resolve', return_value=snapshot):
                good = intake_snapshot(effective(root, ON), root/'submission', accepted_results_context=CONTEXT)
            for admission in (MISSING, None, OFF, {**ON, 'version': True}):
                changed = copy.deepcopy(good)
                changed['data']['effective']['evaluation'] = evaluation(root, admission)
                changed['bindings']['config'] = digest(changed['data']['effective'])
                changed = reseal(changed)  # Valid hashes do not grant admission.
                reason = 'unsupported_admission_policy' if admission is not MISSING and admission is not None and admission.get('version') is True else 'submission_references_disabled'
                with patch('llm_verifier.accepted.resolve') as resolver, patch('llm_verifier.runner.DockerRunner.run') as runner, patch('llm_verifier.provider.HttpTransport.request') as model:
                    for action in (lambda: verify_intake(changed), lambda: pipeline.execute(changed), lambda: workflow.prepare_runtime(changed)):
                        with self.assertRaisesRegex(VerifierError, reason): action()
                    resolver.assert_not_called(); runner.assert_not_called(); model.assert_not_called()
            stale = copy.deepcopy(good)
            stale['data']['effective']['evaluation']['admission'] = OFF
            with self.assertRaises(VerifierError): verify_intake(reseal(stale))

    def test_original_enabled_admission_survives_current_setting_and_environment_changes(self):
        with upstream() as (_, _, _, source), example() as (root, _, _):
            snapshot = platform_snapshot([source]); declare(root, snapshot)
            original = evaluation(root, ON)
            with patch('llm_verifier.accepted.resolve', return_value=snapshot):
                intake = intake_snapshot(load_config(root, 'verifier.json', 'submission', original), root/'submission', accepted_results_context=CONTEXT)
            original['admission'] = OFF
            benchmark = read_json(root/'benchmark.json'); benchmark['tracks'][0]['submissionAdmission'] = OFF
            write_json(root/'benchmark.json', benchmark)
            with patch.dict(os.environ, {'YUKON_EVALUATION': canonical(evaluation(root, OFF)).decode(), 'YUKON_ACCEPTED_RESULTS': 'invalid-current-context'}), patch('llm_verifier.accepted.resolve') as resolver:
                initial, first = direct(root, intake, consuming_decision(root))
                final = pipeline.finalize(intake, initial, first, root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
                self.assertEqual(pipeline.export_results(final), read_json(root/'accepted.json'))
                self.assertEqual(intake['data']['effective']['evaluation']['admission'], ON)
                self.assertEqual(read_json(root/'accepted.json')['ancestors'], [{'submission_id': 'source-0'}])
                resolver.assert_not_called()

    def test_retry_and_score_only_reorg_use_original_admission_not_live_manifest_or_env(self):
        with upstream() as (_, _, _, source), example() as (root, _, _):
            snapshot = platform_snapshot([source]); declare(root, snapshot)
            fields = {'scores': True, 'validity': False}
            benchmark = read_json(root/'benchmark.json'); benchmark['tracks'][0]['submissionAdmission'] = ON
            write_json(root/'benchmark.json', benchmark)
            for original in (MISSING, None, OFF, ON):
                args = SimpleNamespace(checkout=root, verifier_checkout=REPO, candidate_revision='a'*40, verifier_revision='b'*40,
                    root_dir='.', config='verifier.json', submission='submission', score_path='.yukon/score.json',
                    out=root/'intake.json', run_id='123:2', evaluation=canonical(evaluation(root, original, fields)).decode(),
                    accepted_results_context=canonical(CONTEXT).decode())
                with patch.dict(os.environ, {'YUKON_EVALUATION': canonical(evaluation(root, ON)).decode()}), \
                        patch('llm_verifier.workflow.checkout_revision'), patch('llm_verifier.accepted.resolve', return_value=snapshot) as resolver:
                    if original is not ON:
                        with self.assertRaisesRegex(VerifierError, 'submission_references_disabled'): workflow.prepare(args)
                        resolver.assert_not_called()
                    else:
                        intake = workflow.prepare(args)
                        self.assertEqual(intake['data']['effective']['evaluation'], evaluation(root, ON, fields))
                        initial, first = direct(root, intake, consuming_decision(root))
                        self.assertEqual(pipeline.dossier(intake, initial, first)['data']['yukon_projection'], {'scores': None})

    def test_enabled_simulation_uses_snapshot_but_cannot_publish(self):
        with upstream() as (_, _, _, source), example() as (root, _, _):
            snapshot = platform_snapshot([source]); declare(root, snapshot)
            script = read_json(root/'simulation.json')
            script['initial_decision'] = consuming_decision(root)
            with patch('llm_verifier.accepted.resolve', return_value=snapshot):
                report = simulate(effective(root, ON), root/'submission', script, root/'simulated.json', accepted_results_context=CONTEXT)
            self.assertTrue(report['simulation'])
            self.assertEqual(report['data']['phases'][0]['data']['effective']['evaluation']['admission'], ON)
            with self.assertRaisesRegex(VerifierError, 'final_accepted_required'): pipeline.export_results(report)
            with self.assertRaisesRegex(VerifierError, 'simulation_cannot_publish'):
                pipeline.finalize(*report['data']['phases'][:3], root/'score.json', root/'dossier.json', results_path=root/'accepted.json')
            self.assertFalse((root/'score.json').exists()); self.assertFalse((root/'accepted.json').exists())

    def test_authoring_preserves_admission_and_checks_structure_without_enforcing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'; scaffold.initialize(root, 'admission-test', 'test-model'); fill_toy(root)
            declare(root, {'references': [{'submission_id': 'earlier'}]})
            with patch('llm_verifier.accepted.resolve') as resolver:
                scaffold.github(root, 'owner/verifier', 'a'*40)
                self.assertEqual(read_json(root/'benchmark.json')['tracks'][0]['submissionAdmission'], OFF)
                for admission in (ON, OFF, None):
                    benchmark = read_json(root/'benchmark.json'); benchmark['tracks'][0]['submissionAdmission'] = admission
                    write_json(root/'benchmark.json', benchmark)
                    self.assertTrue(scaffold.check(root)['valid'])
                    scaffold.github(root, 'owner/verifier', 'b'*40)
                    self.assertEqual(read_json(root/'benchmark.json')['tracks'][0]['submissionAdmission'], admission)
                benchmark = read_json(root/'benchmark.json'); benchmark['tracks'][0].pop('submissionAdmission')
                write_json(root/'benchmark.json', benchmark)
                self.assertTrue(scaffold.check(root)['valid'])
                scaffold.github(root, 'owner/verifier', 'c'*40)
                self.assertEqual(read_json(root/'benchmark.json')['tracks'][0]['submissionAdmission'], OFF)
                for malformed in ({}, {**ON, 'version': 2}, {**ON, 'extra': True}):
                    benchmark['tracks'][0]['submissionAdmission'] = malformed
                    write_json(root/'benchmark.json', benchmark); before = (root/'benchmark.json').read_bytes()
                    with self.assertRaisesRegex(VerifierError, 'unsupported_admission_policy'): scaffold.check(root)
                    with self.assertRaisesRegex(VerifierError, 'unsupported_admission_policy'): scaffold.github(root, 'owner/verifier', 'd'*40)
                    self.assertEqual((root/'benchmark.json').read_bytes(), before)
                resolver.assert_not_called()
