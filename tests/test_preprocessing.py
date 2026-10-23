"""Opt-in manifest repairs, exact domains, provenance, and early package preflight."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from llm_verifier import pipeline, scaffold
from llm_verifier.artifacts import intake_snapshot, preflight_snapshot, verify_intake
from llm_verifier.cli import main
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, snapshot, write_json
from llm_verifier.input_schema import validate_schema, validate_value
from llm_verifier.jsonutil import canonical, digest, sha
from llm_verifier.judgment import response_schema
from llm_verifier.preprocessing import execution_files, preprocess
from llm_verifier.prior import original_manifest, validate_bundle
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier.scoring import score_value
from llm_verifier.submission import followup_plan, preflight_manifest
from tests.support import direct, example, reseal, scripted_model
from tests.test_prior_results import consuming_decision, install, resolved_intake


def enable(config):
    config['manifest_preprocessor'] = 'curvesmash-v1'


def manifest_edit(root, edit):
    manifest = read_json(root/'submission/submission.json')
    edit(manifest)
    write_json(root/'submission/submission.json', manifest)
    return manifest


def enum_experiment(manifest, schema=None, value=163):
    manifest['experiments'] = [{
        'id': 'enum-probe', 'source': 'probe.py', 'purpose': 'Check a selected finite domain member.',
        'input_schema': {'type': 'object', 'properties': {
            'curve': schema or {'type': 'integer', 'enum': [163, 331, 787]}},
            'required': ['curve'], 'additionalProperties': False},
        'initial_inputs': [{'curve': value}]}]


class IntegerEnums(unittest.TestCase):
    def test_exact_membership_in_initial_followup_and_provider_schema(self):
        with example('computation') as (root, effective, _):
            definition = {'type': 'integer', 'enum': [163, 331, 787], 'minimum': 163, 'maximum': 787}
            (root/'submission/probe.py').write_text('print("{}")')
            manifest_edit(root, lambda m: enum_experiment(m, definition))
            intake = intake_snapshot(effective, root/'submission')
            schema = response_schema(effective['config'], intake['data']['manifest'])
            self.assertEqual(schema['properties']['requests']['items']['anyOf'][0]['properties']['inputs']['properties']['curve'], definition)
            for value in (163, 331, 787):
                validate_value(definition, value)
            for value in (164, True, 163.0, '163', 788):
                with self.subTest(value=value), self.assertRaises(VerifierError):
                    validate_value(definition, value)
                with self.assertRaises(VerifierError):
                    followup_plan([{'experiment': 'enum-probe', 'inputs': {'curve': value}, 'reason': 'probe'}],
                                  intake['data']['manifest'], effective['config']['limits'], 1)
                manifest_edit(root, lambda m: m['experiments'][0].update(initial_inputs=[{'curve': value}]))
                with self.assertRaises(VerifierError):
                    intake_snapshot(effective, root/'submission')

    def test_invalid_enums_and_number_enums_stay_rejected(self):
        for enum in ([], [1, 1], [True], [1.0], ['1'], [None], list(range(65)), [101]):
            with self.subTest(enum=enum), self.assertRaises(VerifierError):
                validate_schema({'type': 'integer', 'minimum': 0, 'maximum': 100, 'enum': enum})
        with self.assertRaises(VerifierError):
            validate_schema({'type': 'number', 'minimum': 0, 'maximum': 1, 'enum': [0.5]})
        with self.assertRaises(VerifierError):
            validate_schema({'type': 'integer', 'enum': [1, 2]})


class ManifestPreprocessing(unittest.TestCase):
    def test_default_does_not_repair_or_change_records(self):
        with example('scored-proof') as (root, effective, intake):
            files = intake['data']['files']
            unchanged, audit = preprocess(files, effective['config'])
            self.assertIs(unchanged, files)
            self.assertIsNone(audit)
            self.assertNotIn('preprocessing', intake['data'])
            manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': '2', 'denominator': '4'}))
            with self.assertRaisesRegex(VerifierError, 'reduced_rational_required'):
                intake_snapshot(effective, root/'submission')
            with self.assertRaisesRegex(VerifierError, 'reduced_rational_required'):
                score_value({'numerator': '2', 'denominator': '4'})
            self.assertEqual(effective['config']['provider']['response_format'], 'json_schema')

    def test_unknown_and_candidate_selected_adapters_rejected(self):
        with example() as (root, effective, _):
            for selection in (None, False, {}, 'module:run', 'curvesmash-v2'):
                with self.subTest(selection=selection), self.assertRaisesRegex(VerifierError, 'unknown_manifest_preprocessor'):
                    normalize({**effective['config'], 'manifest_preprocessor': selection})
            manifest_edit(root, lambda m: m.update(manifest_preprocessor='curvesmash-v1'))
            with self.assertRaisesRegex(VerifierError, 'object_fields'):
                intake_snapshot(effective, root/'submission')

    def test_fraction_repairs_are_exact_and_leave_original_bytes(self):
        cases = [('130815246', '1000000', '65407623', '500000'),
                 ('0', '991', '0', '1'), ('-12', '8', '-3', '2')]
        for n, d, rn, rd in cases:
            with self.subTest(n=n), example('scored-proof', mutate=enable) as (root, effective, _):
                effective['config']['objectives'][0].pop('score_minimum')
                effective['config']['objectives'][0].pop('score_maximum')
                original = manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': n, 'denominator': d}))
                raw = (root/'submission/submission.json').read_bytes()
                intake = intake_snapshot(effective, root/'submission')
                data = verify_intake(intake)
                self.assertEqual(data['manifest']['claims'][0]['score'], {'numerator': rn, 'denominator': rd})
                self.assertEqual(json.loads(data['files']['submission.json']['text']), original)
                self.assertEqual(data['files']['submission.json']['text'].encode(), raw)
                self.assertEqual((root/'submission/submission.json').read_bytes(), raw)
                self.assertEqual(intake['bindings']['submission'], digest(data['files']))
                audit = data['preprocessing']
                self.assertEqual(audit['adapter'], 'curvesmash-v1')
                self.assertEqual(audit['original_sha256'], sha(raw))
                self.assertEqual(audit['effective_sha256'], sha(canonical(data['manifest'])))
                self.assertEqual(audit['changes'], [{'op': 'replace', 'path': ['claims', 0, 'score'],
                    'before': {'numerator': n, 'denominator': d}, 'after': {'numerator': rn, 'denominator': rd}}])

    def test_adapter_does_not_relax_score_syntax_bounds_or_infinity(self):
        bad_scores = [{'numerator': '01', 'denominator': '2'}, {'numerator': '-0', 'denominator': '2'},
                      {'numerator': '2', 'denominator': '0'}, {'numerator': '2', 'denominator': '-4'},
                      {'numerator': 2, 'denominator': '4'}, {'numerator': '2', 'denominator': '4', 'extra': 1},
                      {'numerator': '9'*257, 'denominator': '9'*257},
                      {'numerator': '4', 'denominator': '2'}, 'positive_infinity']
        for score in bad_scores:
            with self.subTest(score=score), example('scored-proof', mutate=enable) as (root, effective, _):
                manifest_edit(root, lambda m: m['claims'][0].update(score=score))
                with self.assertRaises(VerifierError):
                    intake_snapshot(effective, root/'submission')
        with example('scored-proof', mutate=enable) as (_, effective, _):
            config = copy.deepcopy(effective['config'])
            config['objectives'][0]['score_minimum'] = {'numerator': '0', 'denominator': '2'}
            with self.assertRaisesRegex(VerifierError, 'reduced_rational_required'):
                normalize(config)

    def test_enum_bounds_nested_arrays_and_strings_preserve_values(self):
        with example('computation', mutate=enable) as (root, effective, _):
            (root/'submission/probe.py').write_text('print("{}")')
            nested = {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'properties': {
                'mode': {'type': 'string', 'enum': ['all', 'toy', 'curve', 'accounting']},
                'curve': {'type': 'integer', 'enum': [163, 331, 787], 'minimum': 0}},
                'required': ['mode', 'curve'], 'additionalProperties': False}}
            value = [{'mode': 'all', 'curve': 163}]
            original = manifest_edit(root, lambda m: enum_experiment(m, nested, value))
            intake = intake_snapshot(effective, root/'submission')
            props = intake['data']['manifest']['experiments'][0]['input_schema']['properties']['curve']['items']['properties']
            self.assertEqual(props['mode']['maxLength'], 10)
            self.assertEqual(props['curve'], {'type': 'integer', 'enum': [163, 331, 787], 'minimum': 0, 'maximum': 787})
            self.assertEqual(intake['data']['manifest']['experiments'][0]['initial_inputs'], original['experiments'][0]['initial_inputs'])
            audit = intake['data']['preprocessing']
            self.assertEqual(len(audit['changes']), 2)
            self.assertTrue(all(change['op'] == 'add' for change in audit['changes']))
            _, again = preprocess(execution_files(intake['data']), effective['config'])
            self.assertEqual(again['changes'], [])

    def test_no_guessed_bounds_invalid_enums_or_relaxed_existing_bounds(self):
        schemas = [{'type': 'integer'}, {'type': 'string'},
                   {'type': 'integer', 'enum': [163, 331], 'maximum': 200},
                   {'type': 'string', 'enum': ['all', 'accounting'], 'maxLength': 3},
                   {'type': 'integer', 'enum': []}, {'type': 'integer', 'enum': [True]},
                   {'type': 'integer', 'enum': [1, 1]}, {'type': 'integer', 'enum': [1.0]},
                   {'type': 'integer', 'enum': [10**101]}, {'type': 'string', 'enum': ['x'*4097]},
                   {'type': 'integer', 'enum': [1], 'default': 1}]
        for schema in schemas:
            with self.subTest(schema=schema), example('computation', mutate=enable) as (root, effective, _):
                (root/'submission/probe.py').write_text('print("{}")')
                manifest_edit(root, lambda m: enum_experiment(m, schema))
                with self.assertRaises(VerifierError):
                    intake_snapshot(effective, root/'submission')

    def test_original_and_effective_limits_both_enforced(self):
        with example('computation', mutate=enable) as (root, effective, _):
            (root/'submission/probe.py').write_text('print("{}")')
            manifest = manifest_edit(root, enum_experiment)
            # Exact raw cap is valid but inserted bounds exceed the same cap.
            raw = canonical(manifest)
            (root/'submission/submission.json').write_bytes(raw)
            files = snapshot(root/'submission', effective['config']['limits'])
            effective['config']['limits']['max_file_bytes'] = max(len(raw), max(r['size'] for r in files.values()))
            with self.assertRaisesRegex(VerifierError, 'integer_range|file_byte_limit'):
                intake_snapshot(effective, root/'submission')
            effective['config']['limits']['max_file_bytes'] = 65536
            effective['config']['limits']['max_submission_bytes'] = sum(r['size'] for r in files.values())
            with self.assertRaisesRegex(VerifierError, 'submission_byte_limit'):
                intake_snapshot(effective, root/'submission')

    def test_cache_files_and_symlinks_fail_before_adapter(self):
        with example('scored-proof', mutate=enable) as (root, effective, _):
            cache = root/'submission/__pycache__'
            cache.mkdir(); bytecode = cache/'experiment.cpython-312.pyc'
            bytecode.write_bytes(b'\xff\x00binary')
            with patch('llm_verifier.preprocessing._curvesmash_v1') as adapter:
                with self.assertRaisesRegex(VerifierError, r'utf8_required.*__pycache__/experiment.cpython-312.pyc'):
                    intake_snapshot(effective, root/'submission')
                adapter.assert_not_called()
            bytecode.unlink(); (cache/'link').symlink_to(root/'rules.md')
            with patch('llm_verifier.preprocessing._curvesmash_v1') as adapter, self.assertRaises(VerifierError):
                intake_snapshot(effective, root/'submission')
            adapter.assert_not_called()

    def test_tampered_effective_manifest_audit_and_original_reject(self):
        with example('scored-proof', mutate=enable) as (root, effective, _):
            manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': '2', 'denominator': '4'}))
            intake = intake_snapshot(effective, root/'submission')
            mutations = [lambda d: d['manifest']['claims'][0].update(statement='A different claim'),
                         lambda d: d['manifest']['claims'][0].update(score={'numerator': '3', 'denominator': '4'}),
                         lambda d: d['preprocessing'].update(adapter='curvesmash-v2'),
                         lambda d: d['preprocessing'].update(changes=[]),
                         lambda d: d['preprocessing'].update(original_sha256='0'*64),
                         lambda d: d['preprocessing'].update(effective_sha256='0'*64),
                         lambda d: d.pop('preprocessing'),
                         lambda d: d['files']['submission.json'].update(text='{}'),
                         lambda d: d['effective']['config'].pop('manifest_preprocessor')]
            for mutate in mutations:
                changed = copy.deepcopy(intake); mutate(changed['data'])
                with self.subTest(mutate=mutate), self.assertRaises(VerifierError):
                    verify_intake(reseal(changed))
            initial, review = direct(root, intake)
            manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': '1', 'denominator': '2'}))
            fresh = intake_snapshot(effective, root/'submission')
            self.assertNotEqual(intake['bindings']['submission'], fresh['bindings']['submission'])
            with self.assertRaisesRegex(VerifierError, 'artifact_binding'):
                pipeline.verify_initial_review(fresh, initial, review)

    def test_review_initial_followup_and_export_share_effective_material(self):
        with example('computation', mutate=enable) as (root, effective, _):
            (root/'submission/probe.py').write_text('print("{}")')
            original = manifest_edit(root, enum_experiment)
            raw = (root/'submission/submission.json').read_bytes()
            intake = intake_snapshot(effective, root/'submission')
            requested = {'kind': 'followup', 'judgment': None, 'requests': [
                {'experiment': 'enum-probe', 'inputs': {'curve': 331}, 'reason': 'Check another allowed curve.'}]}
            final = read_json(root/'simulation.json')['final_decision']
            with patch.object(DockerRunner, 'run', return_value=output_record('completed', 0, b'{}', b'')) as runner, scripted_model([requested, final]):
                initial = pipeline.execute(intake)
                first = pipeline.review(intake, initial)
                followup = pipeline.execute(intake, first, initial)
                last = pipeline.review(intake, initial, first, followup)
            effective_files = execution_files(intake['data'])
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                self.assertEqual(call.args[0], effective_files)
                self.assertEqual(json.loads(call.args[0]['submission.json']['text']), intake['data']['manifest'])
                self.assertEqual(call.args[0]['probe.py'], intake['data']['files']['probe.py'])
            for report in (initial, followup):
                self.assertEqual(report['data']['runs'][0]['provenance']['bundle_sha256'], digest(effective_files))
            messages, _, _ = pipeline.review_material(intake, initial)
            shown = json.loads(messages[1]['content'].split('\n', 1)[1])
            self.assertEqual(shown['submission'], intake['data']['manifest'])
            self.assertEqual(shown['original_submission_manifest']['text'].encode(), raw)
            self.assertEqual(shown['manifest_preprocessing'], intake['data']['preprocessing'])
            dossier = pipeline.dossier(intake, initial, first, followup, last)
            bundle = pipeline.export_results(dossier)
            self.assertEqual(bundle['source']['manifest']['text'].encode(), raw)
            self.assertEqual(original_manifest(bundle['source']['manifest']), original)
            # Existing native source format stays closed and unchanged.
            self.assertEqual(set(bundle['source']), {'evaluation_id', 'bindings', 'submission_sha256', 'manifest',
                'intake_sha256', 'intake_created_at_ns', 'finalized_at_ns', 'review_sha256', 'dossier_sha256', 'judgment_sha256'})

    def test_unreduced_original_export_reused_with_adapter_disabled(self):
        with example('scored-proof', mutate=enable) as (root, effective, _):
            manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': '2', 'denominator': '4'}))
            raw = (root/'submission/submission.json').read_bytes()
            intake = intake_snapshot(effective, root/'submission')
            initial, review = direct(root, intake)
            dossier = pipeline.dossier(intake, initial, review)
            self.assertEqual(dossier['data']['yukon_projection']['scores'], [0.5])
            bundle = pipeline.export_results(dossier)
            validate_bundle(bundle)
            self.assertEqual(bundle['source']['manifest']['text'].encode(), raw)
        with example() as (root, _, _):
            effective = install(root, bundle)
            self.assertNotIn('manifest_preprocessor', effective['config'])
            consumer = resolved_intake(effective, root/'submission', bundle)
            initial, review = direct(root, consumer, consuming_decision(root))
            exported = pipeline.export_results(pipeline.dossier(consumer, initial, review))
            self.assertEqual(exported['ancestors'], [{'submission_id': 'source-0'}])
            messages, _, _ = pipeline.review_material(consumer, initial)
            prior = json.loads(messages[1]['content'].split('\n', 1)[1])['prior_results'][0]
            self.assertEqual(prior['manifest']['text'].encode(), raw)

    @unittest.skipUnless(os.environ.get('LLMV_DOCKER_INTEGRATION') == '1', 'requires pinned Docker image')
    def test_actual_runner_reads_the_effective_manifest(self):
        with example('computation', mutate=enable) as (root, effective, _):
            (root/'submission/probe.py').write_text(
                'import json\nfrom pathlib import Path\n'
                'm=json.loads(Path("/snapshot/files/submission.json").read_text())\n'
                'print(json.dumps(m["experiments"][0]["input_schema"]["properties"]["curve"]))\n')
            manifest_edit(root, enum_experiment)
            intake = intake_snapshot(effective, root/'submission')
            report = pipeline.execute(intake)
            self.assertEqual(report['data']['runs'][0]['execution']['status'], 'completed', report['data']['runs'][0]['execution'])
            self.assertEqual(report['data']['runs'][0]['execution']['result'],
                             {'type': 'integer', 'enum': [163, 331, 787], 'minimum': 163, 'maximum': 787})


class PreflightDiagnostics(unittest.TestCase):
    def test_preflight_check_and_intake_reject_optional_inputs_before_any_execution(self):
        with example('computation') as (root, effective, _):
            manifest_edit(root, lambda m: m['experiments'][0]['input_schema'].update(required=[]))
            raw = (root/'submission/submission.json').read_bytes()
            with patch.object(DockerRunner, 'run') as runner, patch('llm_verifier.provider.HttpTransport.request') as network:
                for check in (lambda: preflight_snapshot(effective, root/'submission'),
                              lambda: intake_snapshot(effective, root/'submission'), lambda: scaffold.check(root)):
                    with self.assertRaisesRegex(VerifierError, r'optional_inputs_require_json_object.*experiments\[0\].input_schema.properties.n'):
                        check()
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(main(['preflight', '--root', str(root)]), 2)
                self.assertIn('properties.n', stderr.getvalue())
                runner.assert_not_called(); network.assert_not_called()
            self.assertEqual((root/'submission/submission.json').read_bytes(), raw)

    def test_json_mode_preserves_omission_and_no_silent_default(self):
        with example('computation', mutate=enable) as (root, effective, _):
            manifest = manifest_edit(root, lambda m: m['experiments'][0].update(initial_inputs=[{}]))
            manifest['experiments'][0]['input_schema']['required'] = []
            write_json(root/'submission/submission.json', manifest)
            with self.assertRaisesRegex(VerifierError, 'optional_inputs_require_json_object'):
                intake_snapshot(effective, root/'submission')
            effective['config']['provider']['response_format'] = 'json_object'
            intake = intake_snapshot(effective, root/'submission')
            self.assertEqual(intake['data']['preprocessing']['changes'], [])
            self.assertEqual(intake['data']['manifest'], manifest)
            with patch.object(DockerRunner, 'run', return_value=output_record('completed', 0, b'{}', b'')) as runner:
                pipeline.execute(intake)
            self.assertEqual(runner.call_args.args[2], {})
            effective['config']['provider']['response_format'] = 'json_schema'
            effective['config']['limits']['max_followups'] = 0
            preflight_snapshot(effective, root/'submission')

    def test_nested_schema_diagnostic_lists_missing_and_unknown_fields(self):
        with example('computation') as (root, effective, _):
            nested = {'type': 'object', 'properties': {'curve': {'type': 'integer', 'default': 3}},
                      'required': ['curve'], 'additionalProperties': False}
            manifest_edit(root, lambda m: m['experiments'][0].update(input_schema=nested, initial_inputs=[]))
            with self.assertRaises(VerifierError) as caught:
                preflight_snapshot(effective, root/'submission')
            error = caught.exception
            self.assertEqual(error.args, ('object_fields',))
            self.assertIn('experiments[0].input_schema.properties.curve', str(error))
            self.assertEqual(error.missing, ('maximum', 'minimum'))
            self.assertEqual(error.unexpected, ('default',))

    def test_field_diagnostics_are_escaped_bounded_and_exclude_values(self):
        with example('computation') as (root, effective, _):
            name = 'field\n\x1b[31m'
            nested = {'type': 'object', 'properties': {name: {'type': 'integer', 'SECRET_VALUE': 'never-print-me'}},
                      'required': [name], 'additionalProperties': False}
            manifest_edit(root, lambda m: m['experiments'][0].update(input_schema=nested, initial_inputs=[]))
            with self.assertRaises(VerifierError) as caught:
                preflight_snapshot(effective, root/'submission')
            rendered = str(caught.exception)
            self.assertNotIn('\n', rendered); self.assertNotIn('\x1b', rendered)
            self.assertNotIn('never-print-me', rendered)
            self.assertIn('\\n', rendered)
            self.assertLessEqual(len(rendered), 2048)

    def test_preflight_cli_selected_subtree_and_audit_without_output_or_network(self):
        with example('scored-proof', mutate=enable) as (root, _, _):
            manifest_edit(root, lambda m: m['claims'][0].update(score={'numerator': '2', 'denominator': '4'}))
            (root/'submission').rename(root/'selected')
            output = io.StringIO()
            with redirect_stdout(output), patch.object(DockerRunner, 'run') as runner, patch('llm_verifier.provider.HttpTransport.request') as network:
                self.assertEqual(main(['preflight', '--root', str(root), '--submission', 'selected']), 0)
                runner.assert_not_called(); network.assert_not_called()
            result = json.loads(output.getvalue().splitlines()[0])
            self.assertTrue(result['valid'])
            self.assertEqual(result['preprocessing']['adapter'], 'curvesmash-v1')
            self.assertEqual(len(result['preprocessing']['changes']), 1)
            self.assertFalse((root/'.yukon').exists())
