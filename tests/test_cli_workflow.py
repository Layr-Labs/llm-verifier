import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from llm_verifier.cli import main
from llm_verifier.files import read_json, write_json
from tests.support import REPO, example, direct


class CLI(unittest.TestCase):
    def test_subprocess_cli_and_both_offline_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('proof','computation'):
                output=Path(tmp)/(name+'.json')
                result=subprocess.run([sys.executable,'-m','llm_verifier','simulate','--root',str(REPO/'examples'/name),'--script',str(REPO/'examples'/name/'simulation.json'),'--out',str(output)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('SIMULATION ONLY',result.stdout)
                value=read_json(output)
                self.assertTrue(value['simulation_only'])
                self.assertFalse(value['publishable'])
                self.assertNotIn('valid',value)
                self.assertNotIn('scores',value)

    def test_finalization_clears_stale_before_malformed_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);write_json(root/'score.json',{'valid':True,'scores':[999]})
            (root/'intake.json').write_text('malformed')
            result=main(['finalize','--intake',str(root/'intake.json'),'--initial',str(root/'missing.json'),'--initial-review',str(root/'missing2.json'),'--score',str(root/'score.json'),'--dossier',str(root/'dossier.json')])
            self.assertEqual(result,2)
            self.assertFalse((root/'score.json').exists())

    def test_invalid_judgment_cli_exits_successfully(self):
        with example() as (root,_,intake):
            decision=read_json(root/'simulation.json')['initial_decision']
            decision['judgment'].update(valid=False,accepted_claims=[],reason='Not justified.')
            initial,first=direct(root,intake,decision)
            for name,value in [('intake',intake),('initial',initial),('first',first)]:write_json(root/(name+'.json'),value)
            result=main(['finalize','--intake',str(root/'intake.json'),'--initial',str(root/'initial.json'),'--initial-review',str(root/'first.json'),'--score',str(root/'score.json'),'--dossier',str(root/'dossier.json')])
            self.assertEqual(result,0)
            self.assertFalse(read_json(root/'score.json')['valid'])

    def test_evaluation_env_and_protected_output(self):
        with example() as (root,effective,_):
            evaluation={'policies':effective['config']['policies'],'fields':{'scores':False,'validity':True}}
            with patch.dict(os.environ,{'YUKON_EVALUATION':json.dumps(evaluation)}):
                result=main(['intake','--root',str(root),'--out',str(root/'intake.json')])
            self.assertEqual(result,0)
            self.assertEqual(read_json(root/'intake.json')['data']['effective']['evaluation'],evaluation)
            result=main(['intake','--root',str(root),'--out',str(root/'submission/forbidden.json')])
            self.assertEqual(result,2)
            self.assertFalse((root/'submission/forbidden.json').exists())


class Workflow(unittest.TestCase):
    def test_no_candidate_shell_interpolation_and_exact_action_pins(self):
        text=(REPO/'.github/workflows/verify.yml').read_text()
        for action in re.findall(r'uses: (actions/[^\s]+)',text):
            self.assertRegex(action,r'^actions/[a-z-]+@[a-f0-9]{40}$')
        # Every shell block is free of expression interpolation. All inputs and
        # context fields arrive through environment variables or action inputs.
        in_run=False
        for line in text.splitlines():
            if line.startswith('        run: |'):in_run=True
            elif in_run and line and not line.startswith('          '):in_run=False
            if in_run:self.assertNotIn('${{',line)
        self.assertEqual(text.count('name: benchmark-score'),1)
        self.assertIn('persist-credentials: false',text)
        self.assertIn('always() && needs.prepare.result',text)
        self.assertIn('CALLED_WORKFLOW: ${{ toJSON(job) }}',text)
        self.assertNotIn('simulate ',text)

    def test_workflow_pin_guard_rejects_mismatch_without_checkout(self):
        # Execute the actual bootstrap shell block with dummy trusted identities;
        # no git, network or credentials are involved.
        for workflow in ('verify.yml','test-checkers.yml'):
            lines=(REPO/'.github/workflows'/workflow).read_text().splitlines()
            begin=next(i for i,line in enumerate(lines) if line=='        run: |')+1
            block=[]
            for line in lines[begin:]:
                if line and not line.startswith('          '):break
                block.append(line[10:])
            script='\n'.join(block)
            for match in (True,False):
                sha='a'*40
                env={**os.environ,'VERIFIER_REVISION':sha,'VERIFIER_REPOSITORY':'organizer/verifier','CALLED_WORKFLOW':json.dumps({'workflow_repository':'organizer/verifier','workflow_sha':sha if match else 'b'*40})}
                result=subprocess.run(['bash','-eu','-o','pipefail','-c',script],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode==0,match,result.stderr)

    def test_checker_workflow_separates_checkout_from_execution(self):
        text=(REPO/'.github/workflows/test-checkers.yml').read_text()
        prepare,execution=text.split('  checkers:',1)
        self.assertEqual(prepare.count('persist-credentials: false'),2)
        self.assertNotIn('test-checkers --root',prepare)
        self.assertNotIn('secrets.',execution)
        self.assertNotIn('actions/checkout',execution)
        self.assertNotIn('OPENROUTER',text)
        self.assertNotIn('pull_request_target',text)
        self.assertIn('permissions: {}',execution)
        self.assertIn('env -i PATH="$PATH" "$cli" test-checkers --root "$root"',execution)
        self.assertIn('prepare-runtime --phase followup',execution)
        self.assertIn('checkout_revision(workspace / "challenge"',prepare)
        self.assertIn('checkout_revision(workspace / "verifier"',prepare)
        for action in re.findall(r'uses: (actions/[^\s]+)',text):
            self.assertRegex(action,r'^actions/[a-z-]+@[a-f0-9]{40}$')
        in_run=False
        for line in text.splitlines():
            if line.startswith('        run: |'):in_run=True
            elif in_run and line and not line.startswith('          '):in_run=False
            if in_run:self.assertNotIn('${{',line)
