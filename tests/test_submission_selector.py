import shutil
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from llm_verifier.cli import main
from llm_verifier.config import resolve_submission
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_json, write_json
from llm_verifier.workflow import prepare
from tests.test_scoring_sources import starter

class SubmissionSelector(unittest.TestCase):
    def setup_selector(self, root):
        dest=root/'submissions/track/first'
        dest.parent.mkdir(parents=True)
        shutil.copytree(root/'submission',dest)
        c=read_json(root/'verifier.json')
        c['submission_selector']={'path':'selected.txt','directory':'submissions/track'}
        write_json(root/'verifier.json',c)
        (root/'selected.txt').write_text('submissions/track/first\n')

    def test_cli_and_hosted_intake_select_same_folder(self):
        with starter() as (root,_,_):
            self.setup_selector(root)
            self.assertEqual(main(['intake','--root',str(root),'--out',str(root/'intake.json')]),0)
            # Only checkout identity is mocked; actual hosted selection/intake runs.
            args=SimpleNamespace(checkout=root,verifier_checkout=root,candidate_revision='a'*40,
                verifier_revision='b'*40,root_dir='.',submission='submission',score_path='.yukon/score.json',
                config='verifier.json',evaluation=None,out=root/'hosted.json',run_id='1:1')
            (root/'.github/workflows').mkdir(parents=True,exist_ok=True)
            (root/'.github/workflows/verify.yml').write_text('fixture')
            with patch('llm_verifier.workflow.checkout_revision'):
                hosted=prepare(args)
            local=read_json(root/'intake.json')
            self.assertEqual(local['data']['files'],hosted['data']['files'])

    def test_invalid_selection_rejected(self):
        with starter() as (root,_,_):
            self.setup_selector(root)
            for value in ('../escape','submissions/other/candidate','submissions/track-other/x','', 'a\nb'):
                (root/'selected.txt').write_text(value)
                with self.subTest(value=value), self.assertRaises(VerifierError):
                    resolve_submission(root,'verifier.json')
            (root/'selected.txt').unlink()
            (root/'selected.txt').symlink_to(root/'rules.md')
            with self.assertRaises(VerifierError): resolve_submission(root,'verifier.json')
