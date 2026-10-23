"""Author a directory, validate it, and exercise the actual checker adapter."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from llm_verifier import scaffold
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.runner import DockerRunner
from tests.support import example, fill_toy
from tests.test_checks import result


class Authoring(unittest.TestCase):
    def test_init_writes_a_form_that_check_refuses_until_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            report = scaffold.initialize(root, 'new-challenge', 'test-model')
            self.assertEqual(len([p for p in root.rglob('*') if p.is_file()]), 7)
            config = read_json(root/'verifier.json')
            self.assertEqual(config['provider']['model'], 'test-model')
            # A model without qualified settings gets no effort or budget default.
            self.assertNotIn('reasoning_effort', config['provider'])
            self.assertNotIn('total_review_seconds', config['limits'])
            self.assertEqual(config['review_standard'], 'new-challenge-review-v1')
            self.assertIn('rules.md:1', report['unfilled'])
            self.assertTrue(any(p.startswith('submission/') for p in report['unfilled']))
            with self.assertRaisesRegex(VerifierError, r'unfilled_todo: rules\.md:1'):
                scaffold.check(root)
            fill_toy(root)
            (root/'.venv').mkdir(); (root/'.venv/cache').write_text('irrelevant')
            (root/'notes.md').write_text('Author notes')
            self.assertTrue(scaffold.check(root)['valid'])
            self.assertFalse((root/'yukon-scaffold.json').exists())
            self.assertFalse((root/'.github').exists())

    def test_qualified_model_gets_its_reviewer_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            scaffold.initialize(root, 'astra-challenge', 'gpt-6-astra')
            config = read_json(root/'verifier.json')
            provider, limits = scaffold.KNOWN_REVIEWERS['gpt-6-astra']
            self.assertEqual({k: config['provider'][k] for k in provider}, provider)
            self.assertEqual(config['limits']['total_review_seconds'], limits['total_review_seconds'])
            fill_toy(root)
            self.assertTrue(scaffold.check(root)['valid'])

    def test_placeholders_count_only_where_the_starter_put_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            scaffold.initialize(root, 'test', 'test-model'); fill_toy(root)
            # Participants edit submission/ and check runs on their pull requests,
            # so their own text may say TODO without being a starter placeholder.
            proof = root/'submission/proof.md'
            proof.write_text(proof.read_text() + '\nTODO: tighten the constant in a later submission.\n')
            self.assertTrue(scaffold.check(root)['valid'])
            proof.write_text((scaffold.TEMPLATES/'starter/submission/proof.md').read_text())
            with self.assertRaisesRegex(VerifierError, r'unfilled_todo: submission/proof\.md:1'):
                scaffold.check(root)
            fill_toy(root)
            # An organizer's own note is not a starter placeholder either.
            rules = (root/'rules.md').read_text()
            (root/'rules.md').write_text(rules + '\nTODO: a weighted version may follow as a separate challenge.\n')
            self.assertTrue(scaffold.check(root)['valid'])
            (root/'rules.md').write_text(rules + '\nTODO: known invalid arguments, each written as a rule.\n')
            with self.assertRaisesRegex(VerifierError, r'unfilled_todo: rules\.md:'):
                scaffold.check(root)

    def test_placeholders_are_found_in_configured_rules_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            scaffold.initialize(root, 'test', 'test-model'); fill_toy(root)
            config = read_json(root/'verifier.json')
            (root/'rules.md').rename(root/'spec.md'); config['rules_path'] = 'spec.md'
            write_json(root/'verifier.json', config)
            self.assertTrue(scaffold.check(root)['valid'])
            (root/'spec.md').write_text((scaffold.TEMPLATES/'starter/rules.md').read_text())
            with self.assertRaisesRegex(VerifierError, r'unfilled_todo: spec\.md:1'):
                scaffold.check(root)
            fill_toy(root); (root/'rules.md').rename(root/'spec.md')
            starter = read_json(scaffold.TEMPLATES/'starter/verifier.json')
            config['allow_unscored'] = starter['allow_unscored']
            write_json(root/'verifier.json', config)
            with self.assertRaisesRegex(VerifierError, r'unfilled_todo: verifier\.json:'):
                scaffold.check(root)

    def test_github_refuses_an_unfilled_form_and_the_scan_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            scaffold.initialize(root, 'test', 'test-model')
            with self.assertRaisesRegex(VerifierError, 'unfilled_todo'):
                scaffold.github(root, 'owner/verifier', 'a'*40)
            self.assertFalse((root/'benchmark.json').exists())
            fill_toy(root)
            # A large participant file is skipped by the scan and left to intake's own size limits.
            (root/'submission/large.md').write_text('TODO: a complete proof of the starting claim.\n' + 'x' * scaffold.SCAN_BYTES)
            self.assertEqual(scaffold.unfilled(root), [])
            (root/'submission/large.md').unlink()
            # Deeply nested participant JSON falls back to line matching instead of crashing.
            for depth in (950, 5000):
                (root/'submission/data.json').write_text('[' * depth + '0' + ']' * depth)
                self.assertEqual(scaffold.unfilled(root), [])
            (root/'submission/data.json').unlink()
            # A symlinked submission root is not followed.
            outside = Path(tmp)/'outside'; shutil.copytree(scaffold.TEMPLATES/'starter/submission', outside)
            shutil.rmtree(root/'submission'); (root/'submission').symlink_to(outside, target_is_directory=True)
            self.assertEqual(scaffold.unfilled(root), [])

    def test_init_preserves_existing_paths_and_rejects_bad_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'; root.mkdir(); (root/'keep').write_text('original')
            with self.assertRaisesRegex(VerifierError, 'already_exists'):
                scaffold.initialize(root, 'test', 'test-model')
            self.assertEqual((root/'keep').read_text(), 'original')
            for model in ('SELECT_OPENAI_MODEL', 'invalid model'):
                with self.assertRaises(VerifierError):
                    scaffold.initialize(Path(tmp)/'bad', 'test', model)
                self.assertFalse((Path(tmp)/'bad').exists())
            (root/'link').symlink_to(tmp, target_is_directory=True)
            with self.assertRaises(VerifierError):
                scaffold.initialize(root/'link/unsafe', 'test', 'test-model')

    def test_declared_proof_and_checker_files_stay_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'challenge'; scaffold.initialize(root, 'test', 'test-model'); fill_toy(root)
            (root/'submission/proof.md').unlink()
            with self.assertRaisesRegex(VerifierError, 'claim_evidence_file_missing'):
                scaffold.check(root)
            (root/'submission/proof.md').symlink_to(root/'rules.md')
            with self.assertRaises(VerifierError):
                scaffold.check(root)

    def test_hosted_files_refresh_from_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'challenge'; scaffold.initialize(root, 'test', 'test-model'); fill_toy(root)
            scaffold.github(root, 'owner/verifier', 'a'*40)
            workflow=(root/'.github/workflows/benchmark.yml').read_text()
            self.assertEqual(workflow.count('a'*40), 2)
            checker_workflow=(root/'.github/workflows/checker-tests.yml').read_text()
            self.assertEqual(checker_workflow.count('a'*40),2)
            self.assertIn('on: [push, pull_request, workflow_dispatch]',checker_workflow)
            self.assertIn('owner/verifier/.github/workflows/test-checkers.yml@',checker_workflow)
            self.assertNotIn('OPENAI_API_KEY',checker_workflow)
            benchmark=read_json(root/'benchmark.json'); benchmark['tracks'][0]['description']='Human description'
            write_json(root/'benchmark.json',benchmark)
            self.assertTrue(scaffold.check(root)['valid'])
            config=read_json(root/'verifier.json'); config['objectives'][0]['name']='changed'
            write_json(root/'verifier.json',config)
            with self.assertRaisesRegex(VerifierError, 'stale_benchmark'):
                scaffold.check(root)
            scaffold.github(root,'owner/verifier','b'*40)
            self.assertTrue(scaffold.check(root)['valid'])
            self.assertEqual((root/'.github/workflows/checker-tests.yml').read_text().count('b'*40),2)
            for repo, revision in [('owner/repo;bad', 'a'*40), ('owner/repo','master')]:
                with self.assertRaisesRegex(VerifierError, 'immutable_verifier_pin'):
                    scaffold.github(root,repo,revision)

    def test_checker_tests_use_the_production_runner_and_limits(self):
        with example('collision') as (root, effective, _):
            cases=[{'id':'valid-pair','check':'collision','inputs':{'left':'m3','right':'m17'},
                    'expected':{'passed':True,'total_bytes':5}}]
            write_json(root/'checker-tests.json',cases)
            output=result({'passed':True,'target_bits':8,'digest_hex':'bd','total_bytes':5})
            with patch.object(DockerRunner,'run',return_value=output) as run:
                self.assertEqual(scaffold.test_checkers(root),{'passed':1})
            files, source, inputs, seconds = run.call_args.args
            self.assertEqual(set(files),set(effective['config']['checkers'][0]['files']))
            self.assertNotIn('proof.md',files)
            self.assertEqual(seconds,effective['config']['limits']['run_seconds'])
            for bad in (result({'passed':True}),result(status='timeout'),result({'passed':False,'target_bits':8,'digest_hex':'bd','total_bytes':5})):
                with patch.object(DockerRunner,'run',return_value=bad), self.assertRaises(VerifierError):
                    scaffold.test_checkers(root)

    def test_missing_cases_invalid_inputs_and_credentials_fail_before_execution(self):
        with example('collision') as (root, effective, _):
            (root/'checker-tests.json').unlink(missing_ok=True)
            with self.assertRaisesRegex(VerifierError,'checker_tests_required'):
                scaffold.checker_cases(root,effective)
            write_json(root/'checker-tests.json',[{'id':'bad','check':'collision','inputs':{'left':'x','right':'x','extra':1},'expected':{'passed':False}}])
            runner=Mock()
            with self.assertRaises(VerifierError):scaffold.test_checkers(root,runner=runner)
            runner.run.assert_not_called()
            with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}), self.assertRaisesRegex(VerifierError,'credential_free'):
                scaffold.test_checkers(root,runner=runner)
            runner.run.assert_not_called()
