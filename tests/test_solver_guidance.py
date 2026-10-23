"""Discovery must survive ordinary author edits and regeneration without policy loss."""
import os
from pathlib import Path
import tempfile
import unittest

from llm_verifier import scaffold
from llm_verifier.errors import VerifierError
from tests.support import fill_toy


BEGIN = b'<!-- llm-verifier:solver-guidance:begin -->'
END = b'<!-- llm-verifier:solver-guidance:end -->'


class SolverGuidance(unittest.TestCase):
    def create(self, tmp):
        root = Path(tmp) / 'challenge'
        scaffold.initialize(root, 'test', 'test-model')
        return root

    def test_init_discovers_bundled_guide_before_a_pin_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.create(tmp)
            for name in ('README.md', 'AGENTS.md'):
                text = (root / name).read_text()
                self.assertIn('share/llm-verifier', text)
                self.assertIn('docs/solving.md', text)
                self.assertIn('skills/llm-verifier-solver/SKILL.md', text)
                self.assertNotIn('https://', text)
                self.assertNotIn('SOLVER_GUIDANCE_LOCATION', text)
                self.assertEqual(text.count(BEGIN.decode()), 1)
            # Filling only the original placeholders can retain the init block.
            readme = (root / 'README.md').read_bytes()
            fill_toy(root)
            (root / 'README.md').write_bytes(readme[readme.index(BEGIN):])
            self.assertTrue(scaffold.check(root)['valid'])
            scaffold.github(root, 'owner/verifier', 'a' * 40)
            for name in ('README.md', 'AGENTS.md'):
                text = (root / name).read_text()
                self.assertIn('/blob/' + 'a' * 40 + '/docs/solving.md', text)
                self.assertNotIn('No hosted verifier revision', text)

    def test_refresh_preserves_author_bytes_and_unrelated_agent_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.create(tmp)
            fill_toy(root)
            untouched = root / '.bb/AGENTS.md'
            untouched.parent.mkdir()
            untouched.write_bytes(b'Organization policy: never replace me.\r\n')
            (root / 'CLAUDE.md').write_bytes(b'Another agent policy\n')
            originals = {'README.md': '# My challenge — α\r\nSubmit only proofs.'.encode(),
                         'AGENTS.md': b'# Author policy\r\nKeep all constraints.\r\n'}
            for name, content in originals.items():
                (root / name).write_bytes(content)
            report = scaffold.github(root, 'owner/verifier', 'a' * 40)
            for name, content in originals.items():
                self.assertIn(name, report['files'])
                first = (root / name).read_bytes()
                self.assertEqual(first[:len(content)], content)
                # Prose added after a managed block must survive too.
                (root / name).write_bytes(first + b'\r\nAdditional author instructions.\r\n')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            scaffold.github(root, 'other/verifier', 'b' * 40)
            for name in originals:
                old = before[Path(name)]
                current = (root / name).read_bytes()
                self.assertEqual(old.split(BEGIN)[0], current.split(BEGIN)[0])
                self.assertEqual(old.split(END)[1], current.split(END)[1])
                self.assertIn(b'https://github.com/other/verifier/blob/' + b'b' * 40, current)
                self.assertNotIn(b'a' * 40, current)
                self.assertEqual(current.count(BEGIN), 1)
            stable = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            scaffold.github(root, 'other/verifier', 'b' * 40)
            self.assertEqual(stable, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertEqual(untouched.read_bytes(), before[Path('.bb/AGENTS.md')])
            self.assertEqual((root / 'CLAUDE.md').read_bytes(), before[Path('CLAUDE.md')])

    def test_ambiguous_blocks_refuse_before_any_generation_write(self):
        malformed = (BEGIN, END, END + b'\n' + BEGIN,
                     BEGIN + b'\n' + END + b'\n' + BEGIN + b'\n' + END,
                     b'quoted ' + BEGIN + b'\n' + END,
                     BEGIN + b'\n' + END + b' trailing policy')
        for name in ('README.md', 'AGENTS.md'):
            for content in malformed:
                with self.subTest(name=name, content=content), tempfile.TemporaryDirectory() as tmp:
                    root = self.create(tmp)
                    fill_toy(root)
                    scaffold.github(root, 'owner/verifier', 'a' * 40)
                    (root / name).write_bytes(content)
                    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    with self.assertRaisesRegex(VerifierError, 'ambiguous_solver_guidance_block'):
                        scaffold.github(root, 'owner/verifier', 'b' * 40)
                    self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_unsafe_document_targets_are_preserved(self):
        for kind in ('symlink', 'hardlink', 'directory'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = self.create(tmp)
                fill_toy(root)
                outside = Path(tmp) / 'policy.md'
                outside.write_bytes(b'Protected external policy\n')
                target = root / 'AGENTS.md'
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, target)
                else:
                    target.mkdir()
                readme = (root / 'README.md').read_bytes()
                with self.assertRaises(VerifierError):
                    scaffold.github(root, 'owner/verifier', 'a' * 40)
                self.assertEqual(outside.read_bytes(), b'Protected external policy\n')
                self.assertEqual((root / 'README.md').read_bytes(), readme)
                self.assertFalse((root / 'benchmark.json').exists())
                self.assertFalse((root / '.github').exists())
                if kind == 'symlink':
                    self.assertTrue(target.is_symlink())

    def test_crlf_block_refresh_keeps_surrounding_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.create(tmp)
            fill_toy(root)
            original = b'Author\r\n' + BEGIN + b'\r\nOld generated block\r\n' + END + b'\r\nPolicy\r\n'
            (root / 'AGENTS.md').write_bytes(original)
            scaffold.github(root, 'owner/verifier', 'a' * 40)
            current = (root / 'AGENTS.md').read_bytes()
            self.assertEqual(current.split(BEGIN)[0], b'Author\r\n')
            self.assertEqual(current.split(END)[1], b'\r\nPolicy\r\n')
