import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from llm_verifier.config import DEFAULT_IMAGE
from llm_verifier.errors import InfrastructureError
from llm_verifier.runner import capture, docker_command, DockerRunner, output_record
from llm_verifier.jsonutil import loads
from tests.support import example


class Runner(unittest.TestCase):
    def test_command_restrictions_and_immutable_mount(self):
        with example('computation') as (_,effective,_):
            cmd=docker_command('/usr/bin/docker',DEFAULT_IMAGE,Path('/tmp/generated/snapshot'),'llmv-generated',effective['config']['limits'])
        for arg in ['--pull=never','--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--log-driver=none','--memory=256m','--memory-swap=256m','--cpus=1','--pids-limit=32','--ipc=none','--ulimit=nofile=64:64','--ulimit=core=0:0']:
            self.assertIn(arg,cmd)
        self.assertEqual(cmd[cmd.index('--mount')+1],'type=bind,src=/tmp/generated/snapshot,dst=/snapshot,readonly')
        self.assertEqual(cmd[-4:],[DEFAULT_IMAGE,'-I','-B','/snapshot/entrypoint.py'])
        self.assertNotIn('sh',cmd)
        self.assertEqual(cmd.count('--mount'),1)
        self.assertNotIn('/var/run/docker.sock',' '.join(cmd))
        with self.assertRaises(InfrastructureError):docker_command('docker','python:latest',Path('/tmp'),'llmv-x',effective['config']['limits'])

    def test_streaming_output_caps_both_streams(self):
        for stream in ('stdout','stderr'):
            process=subprocess.Popen([sys.executable,'-c',f'import sys; sys.{stream}.write("x"*200000); sys.{stream}.flush()'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            status,code,out,err=capture(process,2,1024,2048)
            self.assertEqual(status,'output_limit')
            self.assertLessEqual(len(out),1024)
            self.assertLessEqual(len(err),2048)
            self.assertIsNotNone(process.poll())

    def test_host_wall_timeout_including_closed_pipes(self):
        for code in ('import time;time.sleep(60)','import os,time;os.close(1);os.close(2);time.sleep(60)'):
            started=time.monotonic()
            process=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            status,*_=capture(process,.15,1024,1024)
            self.assertEqual(status,'timeout')
            self.assertLess(time.monotonic()-started,2)

    def test_protocol_exact_bytes_nonfinite_and_runtime_failures(self):
        for stdout in (b'{"n":NaN}',b'{"n":1e999}',b'{"x":1,"x":2}',b'\xff',b'[]',b'{} trailing'):
            result=output_record('completed',0,stdout,b'')
            self.assertEqual(result['status'],'protocol_error')
            self.assertEqual(base64.b64decode(result['stdout']['base64']),stdout)
        result=output_record('completed',1,b'{"n":1}',b'failure')
        self.assertEqual(result['status'],'runtime_error')
        self.assertIsNone(result['result'])
        self.assertEqual(output_record('completed',0,b'{"real":0.125}',b'')['result']['real'],.125)

    def test_docker_setup_failure_redacted_and_named_cleanup(self):
        with example('computation') as (_,effective,intake):
            runner=DockerRunner(effective['config']['runtime'],effective['config']['limits'])
            with patch('shutil.which',return_value='/usr/bin/docker'),patch('subprocess.run',side_effect=lambda cmd,**kw:Mock(returncode=1 if cmd[1]=='image' else 0,stdout=b'')) as run,patch.dict('os.environ',{'OPENAI_API_KEY':'SECRET','AWS_SECRET_ACCESS_KEY':'SECRET','DOCKER_HOST':'tcp://remote:2375'}),self.assertRaisesRegex(InfrastructureError,'docker_image_or_daemon_unavailable'):
                runner.run(intake['data']['files'],'check.py',{'n':10},20)
            cleanup=run.call_args.args[0]
            self.assertEqual(cleanup[:3],['/usr/bin/docker','rm','-f'])
            self.assertRegex(cleanup[3],r'^llmv-[a-f0-9]{32}$')
            for call in run.call_args_list:
                env=call.kwargs['env']
                self.assertEqual(set(env),{'PATH','DOCKER_CONFIG','DOCKER_HOST'})
                self.assertEqual(env['DOCKER_HOST'],'unix:///var/run/docker.sock')
                self.assertNotIn('SECRET',str(env))

    def test_snapshot_mount_is_fresh_and_never_live_candidate(self):
        with example('computation') as (root,effective,intake):
            runner=DockerRunner(effective['config']['runtime'],effective['config']['limits'])
            paths=[]
            def popen(cmd,**kwargs):
                mount=Path(cmd[cmd.index('--mount')+1].split('src=')[1].split(',dst=')[0])
                paths.append(mount)
                self.assertFalse(mount.is_relative_to(root))
                self.assertEqual((mount/'entrypoint.py').read_text(),intake['data']['files']['check.py']['text'])
                self.assertEqual((mount/'entrypoint.py').stat().st_mode & 0o777,0o444)
                return Mock()
            with patch('shutil.which',return_value='/usr/bin/docker'),patch('subprocess.run',return_value=Mock(returncode=0)),patch('subprocess.Popen',side_effect=popen),patch('llm_verifier.runner.capture',return_value=('completed',0,b'{}',b'')):
                runner.run(intake['data']['files'],'check.py',{'n':10},20)
                runner.run(intake['data']['files'],'check.py',{'n':10},20)
            self.assertNotEqual(paths[0],paths[1])
            self.assertTrue(all(not p.exists() for p in paths))

    def test_negative_host_status_and_unverified_cleanup_are_infrastructure(self):
        with example('computation') as (_,effective,intake):
            runner=DockerRunner(effective['config']['runtime'],effective['config']['limits'])
            for status,code,cleanup_fails in [('completed',-9,False),('timeout',-9,True)]:
                def docker(cmd,**kwargs):
                    if cmd[1]=='rm':return Mock(returncode=1 if cleanup_fails else 0,stdout=b'')
                    if cmd[1]=='container':return Mock(returncode=0,stdout=b'llmv-still-present\n')
                    return Mock(returncode=0,stdout=b'')
                with self.subTest(status=status),patch('shutil.which',return_value='/usr/bin/docker'),patch('subprocess.run',side_effect=docker),patch('subprocess.Popen',return_value=Mock()),patch('llm_verifier.runner.capture',return_value=(status,code,b'',b'')),self.assertRaises(InfrastructureError):
                    runner.run(intake['data']['files'],'check.py',{'n':10},1)



@unittest.skipUnless(os.environ.get('LLMV_DOCKER_INTEGRATION')=='1','set LLMV_DOCKER_INTEGRATION=1; prepare pinned image explicitly')
class DockerIntegration(unittest.TestCase):
    def test_actual_pinned_runtime_and_isolation(self):
        with example('computation') as (_,effective,intake):
            runner=DockerRunner(effective['config']['runtime'],effective['config']['limits'])
            result=runner.run(intake['data']['files'],'check.py',{'n':25},20)
            self.assertEqual(result['status'],'completed',result)
            self.assertEqual(result['result'],{'checked_n':25,'all_equal':True})
            # Organizer-authored probe, not candidate execution on the host.
            source='''import json,os,socket
from pathlib import Path
read_only=False
try:
 Path('/snapshot/entrypoint.py').write_text('changed')
except OSError:
 read_only=True
print(json.dumps({'uid':os.getuid(),'readonly':read_only,'key':os.environ.get('OPENAI_API_KEY'),'socket':Path('/var/run/docker.sock').exists(),'interfaces':socket.if_nameindex()}))
'''
            files={'probe.py':{'text':source}}
            with patch.dict('os.environ',{'OPENAI_API_KEY':'must-not-enter-container'}):
                probe=runner.run(files,'probe.py',{},20)
            self.assertEqual(probe['status'],'completed',probe)
            self.assertEqual(probe['result']['uid'],65534)
            self.assertTrue(probe['result']['readonly'])
            self.assertIsNone(probe['result']['key'])
            self.assertFalse(probe['result']['socket'])
            self.assertEqual([x[1] for x in probe['result']['interfaces']],['lo'])

    def test_actual_timeout_and_output_cleanup(self):
        with example('computation') as (_,effective,_):
            runner=DockerRunner(effective['config']['runtime'],effective['config']['limits'])
            result=runner.run({'test.py':{'text':'import time; time.sleep(60)'}},'test.py',{},1)
            self.assertEqual(result['status'],'timeout')
            result=runner.run({'test.py':{'text':'print("x"*200000)'}},'test.py',{},20)
            self.assertEqual(result['status'],'output_limit')
            self.assertEqual(result['stdout']['bytes'],effective['config']['limits']['stdout_bytes'])
