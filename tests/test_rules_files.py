import unittest
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.artifacts import intake_snapshot
from llm_verifier.pipeline import execute, review_material
from tests.test_scoring_sources import starter

class RulesFiles(unittest.TestCase):
    def test_protected_files_reach_reviewer_and_change_intake(self):
        with starter() as (root, _, _):
            config=read_json(root/'verifier.json')
            config['rules_files']=['target.json']
            write_json(root/'verifier.json',config)
            write_json(root/'target.json',{'unique_constant':123})
            effective=load_config(root,'verifier.json','submission')
            first=intake_snapshot(effective,root/'submission')
            messages,_,_=review_material(first,execute(first))
            self.assertIn('unique_constant',messages[0]['content'])
            write_json(root/'target.json',{'unique_constant':456})
            second=intake_snapshot(load_config(root,'verifier.json','submission'),root/'submission')
            self.assertNotEqual(first['sha256'],second['sha256'])

    def test_missing_symlink_candidate_and_oversize_rules_rejected(self):
        with starter() as (root, _, _):
            config=read_json(root/'verifier.json')
            for path in ('missing.json','submission/proof.md','linked.json','large.json'):
                if path=='linked.json': (root/path).symlink_to(root/'rules.md')
                if path=='large.json': (root/path).write_text('x'*131073)
                config['rules_files']=[path];write_json(root/'verifier.json',config)
                with self.subTest(path=path), self.assertRaises(VerifierError):
                    load_config(root,'verifier.json','submission')

    def test_invalid_lists_rejected(self):
        with starter() as (root, _, _):
            config=read_json(root/'verifier.json')
            for paths in ('target.json',['../escape'],['x','x'],['rules.md'],['x']*17):
                with self.subTest(paths=paths), self.assertRaises(VerifierError):
                    normalize({**config,'rules_files':paths})
