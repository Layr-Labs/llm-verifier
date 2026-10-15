"""Author a directory, validate it, and exercise the actual checker adapter."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from llm_verifier import scaffold
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.runner import DockerRunner
from tests.support import example
from tests.test_checks import result


class Authoring(unittest.TestCase):
    def test_editable_starter_and_normal_working_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'challenge'
            scaffold.initialize(root, 'new-challenge', 'explicit/model')
            self.assertEqual(len([p for p in root.rglob('*') if p.is_file()]), 6)
            self.assertEqual(read_json(root/'verifier.json')['provider']['model'], 'explicit/model')
            (root/'rules.md').write_text('A revised challenge specification.')
            (root/'.venv').mkdir(); (root/'.venv/cache').write_text('irrelevant')
            (root/'notes.md').write_text('Author notes')
            self.assertTrue(scaffold.check(root)['valid'])
            self.assertFalse((root/'yukon-scaffold.json').exists())
            self.assertFalse((root/'.github').exists())

    def test_init_preserves_existing_paths_and_rejects_bad_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'; root.mkdir(); (root/'keep').write_text('original')
            with self.assertRaisesRegex(VerifierError, 'already_exists'):
                scaffold.initialize(root, 'test', 'a/b')
            self.assertEqual((root/'keep').read_text(), 'original')
            for model in ('ORGANIZER/MODEL', 'invalid model'):
                with self.assertRaises(VerifierError):
                    scaffold.initialize(Path(tmp)/'bad', 'test', model)
                self.assertFalse((Path(tmp)/'bad').exists())
            (root/'link').symlink_to(tmp, target_is_directory=True)
            with self.assertRaises(VerifierError):
                scaffold.initialize(root/'link/unsafe', 'test', 'a/b')

    def test_declared_proof_and_checker_files_stay_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'challenge'; scaffold.initialize(root, 'test', 'a/b')
            (root/'submission/proof.md').unlink()
            with self.assertRaisesRegex(VerifierError, 'claim_evidence_file_missing'):
                scaffold.check(root)
            (root/'submission/proof.md').symlink_to(root/'rules.md')
            with self.assertRaises(VerifierError):
                scaffold.check(root)

    def test_hosted_files_refresh_from_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'challenge'; scaffold.initialize(root, 'test', 'a/b')
            scaffold.github(root, 'owner/verifier', 'a'*40)
            workflow=(root/'.github/workflows/benchmark.yml').read_text()
            self.assertEqual(workflow.count('a'*40), 2)
            checker_workflow=(root/'.github/workflows/checker-tests.yml').read_text()
            self.assertEqual(checker_workflow.count('a'*40),2)
            self.assertIn('on: [push, pull_request, workflow_dispatch]',checker_workflow)
            self.assertIn('owner/verifier/.github/workflows/test-checkers.yml@',checker_workflow)
            self.assertNotIn('OPENROUTER_API_KEY',checker_workflow)
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
            with patch.dict(os.environ,{'OPENROUTER_API_KEY':'test-only'}), self.assertRaisesRegex(VerifierError,'credential_free'):
                scaffold.test_checkers(root,runner=runner)
            runner.run.assert_not_called()
