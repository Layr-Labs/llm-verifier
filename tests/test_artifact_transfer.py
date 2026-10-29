"""Offline transport and bootstrap regressions. No models, Docker, or live requests."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / 'scripts/artifact_transfer.py'
spec = importlib.util.spec_from_file_location('artifact_transfer', SOURCE)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
RUN = 'ce7f54c7-61c7-4aae-887f-30da475f5f1a'
JOB = 'ca395085-040a-526b-2ce8-bdc85f692774'
PRODUCER = 'ba395085-040a-526b-2ce8-bdc85f692774'
TOKEN = 'fixture.' + base64.urlsafe_b64encode(json.dumps({'scp': f'Actions.Results:{RUN}:{JOB}'}).encode()).decode().rstrip('=') + '.secret-runtime-token'
SERVICE = 'https://results-receiver.actions.githubusercontent.com/'
BLOB = 'https://fixture.blob.core.windows.net/artifacts/archive.zip?sig=secret-sas'


def archive(entries=(('wheel.whl', b'fixture-wheel'),)):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for path, content in entries:
            z.writestr(path, content)
    return out.getvalue()


def identity(data):
    return {'id': '7123456789', 'name': 'llmv-package', 'sha256': hashlib.sha256(data).hexdigest(),
            'run_id': '40947958680', 'run_attempt': '1'}


def metadata(data, ref=None):
    ref = ref or identity(data)
    # Exact protobuf JSON field names/types from the pinned action's wire codec.
    return {'artifacts': [{'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': PRODUCER,
        'database_id': ref['id'], 'name': ref['name'], 'size': str(len(data)),
        'created_at': '2026-10-29T19:00:00Z', 'digest': 'sha256:' + ref['sha256']}]}


class Response(io.BytesIO):
    def __init__(self, body=b'', status=200, headers=None):
        super().__init__(json.dumps(body).encode() if isinstance(body, (dict, list)) else body)
        self.status, self.headers = status, headers or {}


class ResetBody(Response):
    def read(self, size=-1):
        if self.tell():
            raise ConnectionResetError('secret-runtime-token ' + BLOB)
        return super().read(5)


class Transport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = archive()
        self.ref = identity(self.data)
        self.client = t.Transfer(TOKEN, SERVICE)
        self.open = Mock()
        self.client.opener.open = self.open
        self.sleep = self.enterContext(patch.object(t.time, 'sleep'))
        self.enterContext(patch.object(t.random, 'uniform', return_value=0.125))
        self.enterContext(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('live network forbidden')))
        self.log = io.StringIO()
        self.enterContext(patch('sys.stderr', self.log))
        self.enterContext(patch('sys.stdout', io.StringIO()))

    def script(self, events):
        self.open.side_effect = events

    def success(self):
        return [Response(metadata(self.data)), Response({'signed_url': BLOB}),
                Response(self.data), Response(metadata(self.data))]

    def fetch(self):
        return self.client.fetch(self.ref, self.root / 'wheel')

    def test_list_reset_then_success_is_exact_and_hashes_extracted_files(self):
        self.script([ConnectionResetError('secret-runtime-token ' + BLOB)] + self.success())
        self.assertEqual(self.fetch(), {'wheel.whl': hashlib.sha256(b'fixture-wheel').hexdigest()})
        self.assertEqual((self.root/'wheel/wheel.whl').read_bytes(), b'fixture-wheel')
        self.assertEqual(self.sleep.call_count, 1)
        self.assertEqual(self.open.call_count, 5)
        for call in self.open.call_args_list:
            req = call.args[0]
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], t.REQUEST_SECONDS)
            if req.data:
                body = json.loads(req.data)
                self.assertEqual(req.get_header('Authorization'), 'Bearer ' + TOKEN)
                if req.full_url.endswith('ListArtifacts'):
                    self.assertEqual(body, {'workflow_run_backend_id': RUN,
                        'workflow_job_run_backend_id': JOB, 'id_filter': self.ref['id']})
                else:
                    self.assertEqual(body, {'workflow_run_backend_id': RUN,
                        'workflow_job_run_backend_id': PRODUCER, 'name': self.ref['name']})
            else:
                self.assertIsNone(req.get_header('Authorization'))
        self.assertNotIn('secret', self.log.getvalue())
        self.assertNotIn('https', self.log.getvalue())

    def test_network_and_http_exhaustion_are_bounded_without_destination(self):
        for error in (ConnectionResetError('secret'), socket.timeout('secret'),
                      urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, 'secret')),
                      urllib.error.HTTPError(BLOB, 503, 'secret', {}, None)):
            with self.subTest(error=type(error)):
                self.open.reset_mock(); self.sleep.reset_mock()
                self.script([error] * t.ATTEMPTS)
                with self.assertRaisesRegex(t.TransferError, '^ListArtifacts_retry_exhausted$'):
                    self.fetch()
                self.assertEqual(self.open.call_count, 4)
                self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.125, 2.125, 4.125])
                self.assertFalse((self.root/'wheel').exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_permanent_http_and_tls_errors_do_not_retry(self):
        import ssl
        for error in [urllib.error.HTTPError(BLOB, code, 'secret', {}, None) for code in (400, 401, 403, 404, 410, 422)] + [urllib.error.URLError(ssl.SSLCertVerificationError('secret'))]:
            with self.subTest(error=type(error)):
                self.open.reset_mock(); self.sleep.reset_mock()
                self.script([error])
                with self.assertRaises(t.TransferError):
                    self.fetch()
                self.assertEqual(self.open.call_count, 1)
                self.sleep.assert_not_called()

    def test_signed_url_lookup_reset_then_success(self):
        events = self.success()
        events.insert(1, ConnectionResetError('secret'))
        self.script(events)
        self.fetch()
        self.assertEqual(self.sleep.call_count, 1)
        self.assertEqual(self.open.call_args_list[1].args[0].data, self.open.call_args_list[2].args[0].data)

    def test_partial_download_reset_discards_bytes_and_restarts_hash(self):
        self.script([Response(metadata(self.data)), Response({'signed_url': BLOB}), ResetBody(self.data),
                     Response({'signed_url': BLOB}), Response(self.data), Response(metadata(self.data))])
        self.fetch()
        self.assertEqual((self.root/'wheel/wheel.whl').read_bytes(), b'fixture-wheel')
        self.assertEqual(self.sleep.call_count, 1)
        self.assertEqual(list(self.root.iterdir()), [self.root/'wheel'])

    def test_truncated_response_retries_but_corruption_fails_before_extract(self):
        self.script([Response(metadata(self.data)), Response({'signed_url': BLOB}), Response(self.data[:10]),
                     Response({'signed_url': BLOB}), Response(self.data), Response(metadata(self.data))])
        self.fetch()
        self.assertEqual(self.sleep.call_count, 1)
        other = self.root/'other'
        bad = bytearray(self.data); bad[4] ^= 1
        self.script([Response(metadata(self.data)), Response({'signed_url': BLOB}), Response(bad)])
        with patch.object(t, 'extract') as extract:
            with self.assertRaisesRegex(t.TransferError, 'archive_digest_mismatch'):
                self.client.fetch(self.ref, other)
            extract.assert_not_called()
        self.assertFalse(other.exists())

    def test_storage_redirects_never_carry_auth_and_api_redirects_fail_closed(self):
        redirect = urllib.error.HTTPError(BLOB, 302, 'secret', {'Location': BLOB.replace('fixture.', 'second.')}, None)
        self.script([Response(metadata(self.data)), Response({'signed_url': BLOB}), redirect,
                     Response(self.data), Response(metadata(self.data))])
        self.fetch()
        for call in self.open.call_args_list[2:4]:
            self.assertIsNone(call.args[0].get_header('Authorization'))
        self.script([Response(status=302, headers={'Location': BLOB})])
        with self.assertRaisesRegex(t.TransferError, 'unexpected_api_status'):
            self.client.metadata(self.ref)

    def test_unsafe_signed_urls_and_redirects_fail_without_following(self):
        urls = ['http://fixture.blob.core.windows.net/a', 'https://user:pass@fixture.blob.core.windows.net/a',
                'https://fixture.blob.core.windows.net.evil.invalid/a', 'https://127.0.0.1/a',
                'file:///tmp/a', 'https://fixture.blob.core.windows.net:444/a',
                BLOB + '\nsecret', BLOB + '#secret', 'https://fixture.blob.core.windows.net/\\a']
        for url in urls:
            for redirect in (False, True):
                with self.subTest(url=url, redirect=redirect):
                    self.open.reset_mock()
                    self.script([Response(metadata(self.data)), Response({'signed_url': BLOB if redirect else url})]
                        + ([Response(status=302, headers={'Location': url})] if redirect else []))
                    with self.assertRaises(t.TransferError):
                        self.fetch()
                    self.assertEqual(self.open.call_count, 3 if redirect else 2)

    def test_redirect_loop_is_bounded(self):
        self.script([Response(metadata(self.data)), Response({'signed_url': BLOB})]
                    + [Response(status=302, headers={'Location': BLOB}) for _ in range(4)])
        with self.assertRaisesRegex(t.TransferError, 'storage_redirect_limit'):
            self.fetch()
        self.assertEqual(self.open.call_count, 6)
        self.sleep.assert_not_called()

    def test_metadata_exact_identity_missing_duplicate_renamed_wrong_run_or_digest(self):
        changes = [('database_id', '999'), ('name', 'newest-package'), ('workflow_run_backend_id', JOB),
                   ('workflow_job_run_backend_id', ''), ('digest', 'sha256:' + '0'*64),
                   ('size', str(t.MAX_ARCHIVE_BYTES+1))]
        rows = [[], metadata(self.data)['artifacts'] * 2]
        for key, value in changes:
            row = metadata(self.data)['artifacts'][0]; row[key] = value; rows.append([row])
        for candidates in rows:
            with self.subTest(candidates=candidates):
                self.open.reset_mock()
                self.script([Response({'artifacts': candidates})])
                with self.assertRaises(t.TransferError):
                    self.fetch()
                self.assertEqual(self.open.call_count, 1)
                self.assertFalse((self.root/'wheel').exists())

    def test_producer_job_change_during_download_is_rejected(self):
        altered = metadata(self.data); altered['artifacts'][0]['workflow_job_run_backend_id'] = JOB
        self.script(self.success()[:3] + [Response(altered)])
        with self.assertRaisesRegex(t.TransferError, 'artifact_identity_changed'):
            self.fetch()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exact_pinned_protobuf_codec_fixture_request_and_real_upload_metadata(self):
        fixture = json.loads((REPO/'tests/fixtures/artifact-transfer-protocol.json').read_text())
        self.assertEqual(fixture['action_revision'], '018cc2cf5baa6db3ef3c5f8a56943fffe632ef53')
        row = fixture['response']['artifacts'][0]
        ref = {**self.ref, 'id':row['database_id'], 'name':row['name'], 'sha256':row['digest']}
        self.script([Response(fixture['response'])])
        observed = self.client.metadata(ref)
        self.assertEqual(json.loads(self.open.call_args.args[0].data), fixture['request'])
        self.assertEqual(observed['size'], '94310')  # Real archive size, not wheel size.
        self.assertEqual(observed['databaseId'], '12930800270')

    def test_changed_backend_run_on_rerun_fails_closed(self):
        # Numeric run and producer attempt may match; backend mismatch still cannot be widened.
        self.client.run = JOB
        self.script([Response(metadata(self.data))])
        with self.assertRaisesRegex(t.TransferError, 'artifact_identity_mismatch'):
            self.fetch()
        self.assertFalse((self.root/'wheel').exists())

    def test_metadata_camelcase_and_missing_optional_digest(self):
        body = metadata(self.data)
        row = body['artifacts'][0]
        for snake, camel in [('database_id','databaseId'), ('workflow_run_backend_id','workflowRunBackendId'), ('workflow_job_run_backend_id','workflowJobRunBackendId')]:
            row[camel] = row.pop(snake)
        del row['digest']
        self.script([Response(body), Response({'signedUrl': BLOB}), Response(self.data), Response(body)])
        self.fetch()

    def test_metadata_body_reset_then_success_and_size_bound(self):
        self.script([ResetBody(json.dumps(metadata(self.data)).encode())] + self.success())
        self.fetch()
        self.script([Response(b'x'*(t.MAX_METADATA_BYTES+1))])
        with self.assertRaisesRegex(t.TransferError, 'metadata_too_large'):
            self.client.metadata(self.ref)

    def test_download_exhaustion_never_leaves_partial_destination(self):
        events = [Response(metadata(self.data))]
        for _ in range(4):
            events += [Response({'signed_url': BLOB}), ResetBody(self.data)]
        self.script(events)
        with self.assertRaisesRegex(t.TransferError, 'download_retry_exhausted'):
            self.fetch()
        self.assertEqual(self.sleep.call_count, 3)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_retry_budget_consumes_one_shared_deadline(self):
        now = [0]
        with patch.object(t.time, 'monotonic', side_effect=lambda: now[0]):
            self.client.deadline = 2
            self.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
            self.script([ConnectionResetError('secret')] * 4)
            with self.assertRaisesRegex(t.TransferError, 'transfer_deadline'):
                self.fetch()
        self.assertEqual(self.open.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_existing_destination_and_symlink_never_overwritten(self):
        for symlink in (False, True):
            path = self.root/('link' if symlink else 'existing')
            if symlink: path.symlink_to(self.root/'absent')
            else: path.mkdir()
            with self.assertRaisesRegex(t.TransferError, 'destination_exists'):
                self.client.fetch(self.ref, path)
        self.open.assert_not_called()

    def test_manifest_preserves_old_producer_attempt_and_rejects_invalid_inputs(self):
        entries = [{'artifact': self.ref, 'path':'wheel'}]
        self.assertEqual(t.references(json.dumps(entries), self.ref['run_id'], '2'), entries)
        for key, value in [('run_id','different'), ('run_attempt','3'), ('id',''), ('id',True),
                           ('sha256',''), ('name','../wheel')]:
            changed = copy.deepcopy(entries); changed[0]['artifact'][key] = value
            with self.subTest(key=key), self.assertRaises(t.TransferError):
                t.references(json.dumps(changed), self.ref['run_id'], '2')
        for path in ('../wheel', '/wheel', '.', 'a/b', 'a\\b'):
            changed = copy.deepcopy(entries); changed[0]['path'] = path
            with self.assertRaises(t.TransferError):
                t.references(json.dumps(changed), self.ref['run_id'], '2')
        with self.assertRaises(t.TransferError):
            t.references(json.dumps(entries*2), self.ref['run_id'], '2')

    def env(self):
        return {'LLMV_ARTIFACTS': json.dumps([{'artifact':self.ref,'path':'wheel'}]),
                'GITHUB_RUN_ID':self.ref['run_id'], 'GITHUB_RUN_ATTEMPT':'2', 'RUNNER_TEMP':str(self.root),
                'ACTIONS_RUNTIME_TOKEN':TOKEN, 'ACTIONS_RESULTS_URL':SERVICE}

    def test_download_entrypoint_receipt_binds_preserved_descriptor_and_file_bytes(self):
        self.script(self.success())
        with patch.object(t, 'Transfer', return_value=self.client), patch.dict(os.environ, self.env(), clear=True), patch.object(sys, 'argv', ['transfer.py','download']):
            self.assertEqual(t.main(), 0)
        receipt = json.loads((self.root/'llmv-transfer-receipt.json').read_text())
        self.assertEqual(receipt, {'version':1, 'run_id':self.ref['run_id'], 'delivery_attempt':'2',
            'artifacts':[{'artifact':self.ref,'path':'wheel','files':{'wheel.whl':hashlib.sha256(b'fixture-wheel').hexdigest()}}]})

    def test_real_wall_deadline_interrupts_stalled_headers_and_body(self):
        # Real OS timer, entirely mocked I/O: covers blocking calls, not just clock checks.
        for stage in ('open','body'):
            with self.subTest(stage=stage):
                def stall(*args, **kwargs):
                    signal.pause()
                if stage == 'open':
                    self.open.side_effect = stall
                else:
                    response = Response(); response.read = stall
                    self.script([response])
                started = time.monotonic()
                with patch.object(t, 'Transfer', return_value=self.client), patch.object(t, 'DEADLINE_SECONDS', 0.03), patch.dict(os.environ, self.env(), clear=True), patch.object(sys, 'argv', ['transfer.py','download']):
                    self.assertEqual(t.main(), 1)
                self.assertLess(time.monotonic()-started, 1)
                self.assertFalse((self.root/'llmv-transfer-receipt.json').exists())
        self.assertIn('transfer_deadline', self.log.getvalue())

    def test_unexpected_errors_do_not_disclose_token_signed_url_or_traceback(self):
        with patch.object(t, 'Transfer', side_effect=ValueError(TOKEN + BLOB)), patch.dict(os.environ,self.env(),clear=True), patch.object(sys,'argv',['transfer.py','download']):
            self.assertEqual(t.main(), 1)
        self.assertNotIn(TOKEN, self.log.getvalue())
        self.assertNotIn(BLOB, self.log.getvalue())
        self.assertNotIn('Traceback', self.log.getvalue())


class Extraction(unittest.TestCase):
    def test_reject_unsafe_zip_names_links_duplicates_and_collisions(self):
        bad_names = ('/absolute', '../outside', 'a/../outside', 'a//b', './a', 'C:/a', 'a\\b', 'a\nb')
        cases = [[(name,b'bad')] for name in bad_names]
        link = zipfile.ZipInfo('link'); link.create_system=3; link.external_attr=(stat.S_IFLNK|0o777)<<16
        fifo = zipfile.ZipInfo('fifo'); fifo.create_system=3; fifo.external_attr=(stat.S_IFIFO|0o600)<<16
        cases += [[(link,b'/outside')], [(fifo,b'')], [('same',b'a'),('same',b'b')],
                  [('a',b'file'),('a/b',b'child')]]
        import warnings
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                root=Path(tmp); (root/'a.zip').write_bytes(archive(entries))
                with self.assertRaises(t.TransferError):
                    t.extract(root/'a.zip',root/'out')
                self.assertFalse((root/'out').exists())

    def test_expanded_bytes_member_count_and_compression_bounds(self):
        for setting, value in [('MAX_EXPANDED_BYTES', 3), ('MAX_MEMBERS', 1)]:
            with tempfile.TemporaryDirectory() as tmp, patch.object(t,setting,value):
                root=Path(tmp); (root/'a.zip').write_bytes(archive([('a',b'1234'),('b',b'5678')]))
                with self.assertRaises(t.TransferError): t.extract(root/'a.zip',root/'out')
                self.assertFalse((root/'out').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with zipfile.ZipFile(root/'a.zip','w',compression=zipfile.ZIP_BZIP2) as z: z.writestr('a',b'abc')
            with self.assertRaisesRegex(t.TransferError,'unsupported_archive_compression'): t.extract(root/'a.zip',root/'out')

    def test_safe_directory_hidden_file_and_executable_mode_become_plain_files(self):
        member = zipfile.ZipInfo('bin/tool'); member.create_system=3; member.external_attr=(stat.S_IFREG|0o755)<<16
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'a.zip').write_bytes(archive([('bin/',b''),(member,b'tool'),('.hidden',b'bytes')]))
            hashes=t.extract(root/'a.zip',root/'out')
            self.assertEqual(set(hashes), {'bin/tool','.hidden'})
            self.assertEqual((root/'out/.hidden').read_bytes(),b'bytes')
            self.assertEqual((root/'out/bin/tool').stat().st_mode & 0o111,0)


class Bootstrap(unittest.TestCase):
    def block(self):
        text=(REPO/'.github/workflows/verify.yml').read_text()
        body=text.split('  experiments:\n',1)[1].split('      - name: Download and verify',1)[0]
        return '\n'.join(line[10:] for line in body.split('        run: |\n',1)[1].splitlines())

    def run_bootstrap(self, root, encoded, sha):
        env={'PATH':os.environ['PATH'],'GITHUB_WORKSPACE':str(root),
             'LLMV_TRANSFER_HELPER':encoded,'LLMV_TRANSFER_SHA256':sha}
        return subprocess.run(['bash','-eu','-o','pipefail','-c',self.block()],env=env,capture_output=True,text=True,timeout=5)

    def test_trusted_output_roundtrip_before_wheel_or_checkout_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'outputs'
            t.emit_bootstrap(SOURCE,output)
            values=dict(line.split('=',1) for line in output.read_text().splitlines())
            self.assertLess(output.stat().st_size*2,1024*1024)  # GitHub counts output size as UTF-16.
            self.assertEqual(base64.b64decode(values['helper_base64'],validate=True),SOURCE.read_bytes())
            result=self.run_bootstrap(root,values['helper_base64'],values['helper_sha256'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((root/'.llmv-artifact-transfer/transfer.py').read_bytes(),SOURCE.read_bytes())
            self.assertEqual((root/'.llmv-artifact-transfer/index.js').read_text(),t.ACTION_JS)
            self.assertFalse((root/'wheel').exists())

    def test_bootstrap_rejects_overlimit_crlf_bad_digest_and_shell_data(self):
        encoded=base64.b64encode(SOURCE.read_bytes()).decode(); sha=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        cases=[('',sha),('A'*(t.MAX_BASE64_BYTES+1),sha),(encoded+'\r\n',sha),
               (encoded,'0'*64),('$(touch injected)',sha),('`touch injected`',sha),
               (base64.b64encode(b'x'*(t.MAX_HELPER_BYTES+1)).decode(),sha)]
        for data,expected in cases:
            with self.subTest(length=len(data)),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); result=self.run_bootstrap(root,data,expected)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(list(root.iterdir()),[])

    def test_output_encoder_bounds_and_preserves_source_crlf_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source'; output=root/'outputs'
            source.write_bytes(b'one\r\ntwo\r\n')
            t.emit_bootstrap(source,output)
            self.assertNotIn(b'\r',output.read_bytes())
            encoded=output.read_text().splitlines()[0].split('=',1)[1]
            self.assertEqual(base64.b64decode(encoded),source.read_bytes())
            source.write_bytes(b'x'*(t.MAX_HELPER_BYTES+1)); before=output.read_bytes()
            with self.assertRaisesRegex(t.TransferError,'helper_too_large'):t.emit_bootstrap(source,output)
            self.assertEqual(output.read_bytes(),before)

    def test_existing_bootstrap_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'.llmv-artifact-transfer').mkdir()
            result=self.run_bootstrap(root,base64.b64encode(SOURCE.read_bytes()).decode(),hashlib.sha256(SOURCE.read_bytes()).hexdigest())
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(list((root/'.llmv-artifact-transfer').iterdir()),[])

    def test_launcher_forwards_only_transfer_environment_without_shell(self):
        # Execute the exact JS with mocked child_process; no child or network is started.
        script="""const vm=require('node:vm'); const fs=require('node:fs'); let called=false;
        vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'), {
          require: name => name==='node:path' ? require(name) : {spawnSync:(exe,args,opts)=>{
            called=true;
            if(exe!=='python3'||args[0]!=='-I'||args[2]!=='download') throw Error('interpreter');
            if(opts.shell||opts.env.OPENAI_API_KEY||opts.env.GITHUB_TOKEN||opts.env.PYTHONPATH) throw Error('env');
            if(opts.env.ACTIONS_RUNTIME_TOKEN!=='runtime-fixture'||opts.timeout!==190000||opts.killSignal!=='SIGKILL') throw Error('limits');
            return {status:0}; }},
          __dirname:'/trusted',console,process:{env:{PATH:'/bin',RUNNER_TEMP:'/tmp',
            ACTIONS_RUNTIME_TOKEN:'runtime-fixture',OPENAI_API_KEY:'model-fixture',GITHUB_TOKEN:'github-fixture',PYTHONPATH:'/candidate'},
            exit:code=>{if(code!==0) throw Error('exit');}} });
        if(!called)throw Error('missing launch');"""
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'index.js';path.write_text(t.ACTION_JS)
            result=subprocess.run(['node','-e',script,str(path)],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_hosted_smoke_uses_exact_bootstrap_without_consumer_checkout_or_permissions(self):
        smoke=(REPO/'.github/workflows/test-artifact-transfer.yml').read_text()
        verify=(REPO/'.github/workflows/verify.yml').read_text()
        marker='      - name: Bootstrap trusted artifact transfer before wheel download'
        def block(text):
            return text.split(marker,1)[1].split('      - name: Download and verify',1)[0]
        self.assertEqual(block(smoke),block(verify))
        consumer=smoke.split('  download:',1)[1]
        self.assertIn('permissions: {}',consumer)
        self.assertNotIn('actions/checkout',consumer)
        self.assertNotIn('secrets.',smoke)
        self.assertNotIn('actions: read',smoke)
        self.assertIn('uses: ./.llmv-artifact-transfer',consumer)
        self.assertIn('needs.prepare.outputs.artifact',consumer)
        self.assertIn('artifact-id',smoke)
        self.assertIn('artifact-digest',smoke)
        self.assertIn('llmv-transfer-receipt.json',consumer)

    def test_smoke_fixture_and_verification_scripts_execute_offline(self):
        text=(REPO/'.github/workflows/test-artifact-transfer.yml').read_text()
        def script_after(name):
            body=text.split(name,1)[1].split('        run: |\n',1)[1]
            return '\n'.join(line[10:] for line in body.splitlines() if line.startswith('          '))
        producer=script_after('      - name: Write known fixture').split('PY\n',1)[0]+'PY\n'
        consumer=script_after('      - name: Verify fixture bytes and transfer receipt')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'output'
            ref=identity(archive()); env={'PATH':os.environ['PATH'],'RUNNER_TEMP':tmp,
                'GITHUB_OUTPUT':str(output),'GITHUB_RUN_ID':ref['run_id'],'GITHUB_RUN_ATTEMPT':'2'}
            result=subprocess.run(['bash','-eu','-o','pipefail','-c',producer],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            sha=output.read_text().strip().split('=',1)[1]
            (root/'transfer-fixture').rename(root/'smoke')
            receipt={'version':1,'run_id':ref['run_id'],'delivery_attempt':'2',
                'artifacts':[{'artifact':ref,'path':'smoke','files':{'payload.txt':sha}}]}
            (root/'llmv-transfer-receipt.json').write_text(json.dumps(receipt))
            env.update(EXPECTED_ARTIFACT=json.dumps(ref),EXPECTED_FIXTURE_SHA256=sha)
            for corrupt in (False,True):
                if corrupt:(root/'smoke/payload.txt').write_text('tampered')
                result=subprocess.run(['bash','-eu','-o','pipefail','-c',consumer],env=env,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode==0,not corrupt,result.stderr)

    def test_workflow_bootstraps_all_consumers_and_preserves_legacy_permissions(self):
        text=(REPO/'.github/workflows/verify.yml').read_text()
        self.assertNotIn('actions: read',text)
        self.assertNotIn('actions/download-artifact',text)
        self.assertEqual(text.count('uses: ./.llmv-artifact-transfer'),5)
        for body in re.split(r'(?m)^  [a-z_]+:\n',text.split('jobs:\n')[1])[2:]:
            self.assertNotIn('actions/checkout',body)
            self.assertIn('needs.prepare.outputs.helper_base64',body)
            self.assertIn('needs.prepare.outputs.helper_sha256',body)
            self.assertLess(body.index('Bootstrap trusted'),body.index('Download and verify'))
            self.assertLess(body.index('Download and verify'),body.index('Setup verifier'))
        self.assertNotIn('${{',self.block())
        # Existing direct judgments do not require skipped follow-up descriptors.
        final=text.split('  finalize:\n')[1]
        self.assertIn("needs.initial_review.outputs.kind == 'followup' && format",final)
        self.assertIn("|| ''",final)
        for output in ('package_artifact','intake_artifact','dossier_artifact','review_run_attempt'):
            self.assertIn('      '+output+':',text.split('permissions:',1)[0])


if __name__=='__main__':
    unittest.main()
