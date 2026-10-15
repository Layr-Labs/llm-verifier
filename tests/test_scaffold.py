"""Generator round trips and authority boundaries in fresh temporary trees."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from llm_verifier import scaffold
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from tests.support import REPO

SELECTED = scaffold.options('Layr-Labs/llm-verifier', '5b67e8d51c3bd1f9c245c3f0978fe8f9bf2b1c8d', 'explicit/model')


def definition():
    return read_json(REPO / 'examples/collision.definition.json')


class Scaffold(unittest.TestCase):
    def test_roundtrip_preserves_bytes_pins_edit_map_and_simulation_scope(self):
        raw = definition()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'
            result = scaffold.initialize(raw, root, SELECTED)
            receipt = read_json(result['receipt'])
            self.assertEqual((root/'rules.md').read_text(), raw['files']['rules.md'])
            self.assertTrue(result['validation']['valid'])
            self.assertEqual(result['validation']['model_review'], 'not_run')
            self.assertEqual(len(result['validation']['conformance']), 2)
            self.assertEqual(scaffold.context(root, 'build', raw, receipt)['edit'], raw['edit_paths'])
            self.assertEqual(scaffold.context(root, 'validate', raw, receipt)['edit'], [])
            self.assertEqual(scaffold.context(root, 'solve')['edit'], ['submission'])
            workflow = (root/'.github/workflows/benchmark.yml').read_text()
            self.assertEqual(workflow.count(SELECTED['verifier_revision']), 2)
            public = (root/'.github/workflows/public-conformance.yml').read_text()
            self.assertNotIn('secrets.', public)
            self.assertNotIn('Layr-Labs', public)
            compile((root/'tools/public_conformance.py').read_text(), 'public_conformance.py', 'exec')
            self.assertEqual(read_json(root/'verifier.json')['provider']['model'], 'explicit/model')
            self.assertTrue(scaffold.check(root, raw, receipt)['valid'])

    def test_refuses_existing_output_and_receipt_without_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'; root.mkdir(); (root/'owned').write_text('keep')
            with self.assertRaisesRegex(VerifierError, 'exists'):
                scaffold.initialize(definition(), root, SELECTED)
            self.assertEqual((root/'owned').read_text(), 'keep')
            root = Path(tmp)/'new'; Path(str(root)+'.receipt.json').write_text('keep')
            with self.assertRaisesRegex(VerifierError, 'exists'):
                scaffold.initialize(definition(), root, SELECTED)
            self.assertFalse(root.exists())

    def test_protected_bytes_and_forged_inventory_require_external_receipt(self):
        raw = definition()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'
            result = scaffold.initialize(raw, root, SELECTED); receipt = read_json(result['receipt'])
            (root/'submission/proof.md').write_text('Allowed builder revision.')
            self.assertTrue(scaffold.check(root, raw, receipt)['valid'])
            (root/'rules.md').write_text('Changed rules.')
            with self.assertRaisesRegex(VerifierError, 'protected_file_changed'):
                scaffold.check(root, raw, receipt)
            inventory = read_json(root/'yukon-scaffold.json')
            inventory['selected']['model'] = 'attacker/model'
            write_json(root/'yukon-scaffold.json', inventory)
            with self.assertRaisesRegex(VerifierError, 'inventory_binding'):
                scaffold.context(root, 'build', raw, receipt)

    def test_bad_definitions_paths_and_cases_leave_no_export(self):
        for mutation in (lambda d: d['files'].update({'../escape': 'bad'}),
                         lambda d: d['files'].update({'verifier.json': '{}'}),
                         lambda d: d['edit_paths'].append('rules.md'),
                         lambda d: d['conformance'][0]['expected'].update(scores=[999])):
            raw = definition(); mutation(raw)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)/'challenge'
                with self.assertRaises(VerifierError):
                    scaffold.initialize(raw, root, SELECTED)
                self.assertFalse(root.exists())
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_symlink_parents_and_protected_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); (base/'real').mkdir(); (base/'alias').symlink_to(base/'real')
            with self.assertRaises(VerifierError):
                scaffold.initialize(definition(), base/'alias/challenge', SELECTED)
            root = base/'challenge'; result = scaffold.initialize(definition(), root, SELECTED)
            receipt = read_json(result['receipt'])
            payload = (root/'rules.md').read_text(); (root/'rules.md').unlink()
            (base/'external').write_text(payload); (root/'rules.md').symlink_to(base/'external')
            with self.assertRaises(VerifierError):
                scaffold.check(root, definition(), receipt)

    def test_complete_inventory_rejects_extras_and_symlink_directories(self):
        raw = definition()
        for extra in ('.github/workflows/unapproved.yml', 'unlisted.txt', 'AGENTS.md'):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)/'challenge'
                created = scaffold.initialize(raw, root, SELECTED)
                receipt = read_json(created['receipt'])
                (root/extra).write_text('unapproved')
                for operation in (lambda: scaffold.check(root, raw, receipt),
                                  lambda: scaffold.context(root, 'build', raw, receipt)):
                    with self.assertRaisesRegex(VerifierError, 'scaffold_unexpected_path'):
                        operation()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'; created = scaffold.initialize(raw, root, SELECTED)
            receipt = read_json(created['receipt'])
            (root/'extra-link').symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaisesRegex(VerifierError, 'scaffold_symlink'):
                scaffold.check(root, raw, receipt)
            (root/'extra-link').unlink()
            # Root Git internals can contain arbitrary names or links and are
            # never visited. Nested .git and symlink root .git remain forbidden.
            (root/'.git').mkdir(); (root/'.git/objects').mkdir()
            (root/'.git/objects/external').symlink_to('/nonexistent')
            self.assertTrue(scaffold.check(root, raw, receipt)['valid'])
            import shutil
            shutil.rmtree(root/'.git'); (root/'.git').write_text('gitdir: /external/metadata')
            self.assertTrue(scaffold.check(root, raw, receipt)['valid'])
            (root/'.git').unlink(); (root/'.git').symlink_to('/nonexistent')
            with self.assertRaisesRegex(VerifierError, 'scaffold_symlink'):
                scaffold.context(root, 'build', raw, receipt)

    def test_pin_model_and_receipt_are_explicit(self):
        for repository, revision, model in [('a/b;cmd', 'f'*40, 'a/b'), ('a/b', 'master', 'a/b'),
                                             ('a/b', 'f'*40, 'ORGANIZER/MODEL')]:
            with self.assertRaises(VerifierError):
                scaffold.options(repository, revision, model)
        with self.assertRaisesRegex(VerifierError, 'requires_definition_and_receipt'):
            scaffold.context('.', 'build')

    def test_configured_rules_path_and_bounded_public_capture(self):
        raw = definition()
        raw['files']['RULES.md'] = raw['files'].pop('rules.md')
        raw['verifier']['rules_path'] = 'RULES.md'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'
            scaffold.initialize(raw, root, SELECTED)
            self.assertIn('RULES.md', scaffold.context(root, 'solve')['read'])
        from llm_verifier.scaffold_templates import PUBLIC_RUNNER
        namespace = {'__name__': 'public_runner_test'}
        exec(compile(PUBLIC_RUNNER, 'public_conformance.py', 'exec'), namespace)
        import os
        with self.assertRaisesRegex(RuntimeError, 'output limit'):
            namespace['capture']([sys.executable, '-c', 'import sys;sys.stdout.write("x"*300000)'], b'', {'PATH': os.environ.get('PATH', '')})

    def test_cli_init_check_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'challenge'
            command = [sys.executable, '-m', 'llm_verifier']
            init = subprocess.run(command + ['init', '--definition', str(REPO/'examples/collision.definition.json'),
                '--output', str(root), '--verifier-repository', SELECTED['verifier_repository'],
                '--verifier-revision', SELECTED['verifier_revision'], '--model', SELECTED['model']], capture_output=True, text=True)
            self.assertEqual(init.returncode, 0, init.stderr)
            receipt = str(root)+'.receipt.json'
            for sub in [['check'], ['context', '--task', 'build']]:
                proc = subprocess.run(command + sub + ['--root', str(root), '--definition', str(REPO/'examples/collision.definition.json'), '--receipt', receipt], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                json.loads(proc.stdout.splitlines()[0])


if __name__ == '__main__':
    unittest.main()
