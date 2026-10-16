import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from llm_verifier.artifacts import intake_snapshot
from llm_verifier.config import load_config, normalize
from llm_verifier.errors import VerifierError
from llm_verifier.files import read_bytes, read_json, relative, snapshot, write_json
from llm_verifier.input_schema import validate_schema, validate_value
from llm_verifier.jsonutil import canonical, digest, loads
from tests.support import REPO, example


class Boundaries(unittest.TestCase):
    def test_strict_json_duplicate_nonfinite_encoding_and_depth(self):
        for data in [b'{"x":1,"x":2}',b'NaN',b'Infinity',b'-Infinity',b'1e9999',b'"\xff"',b'"\\ud800"',b'[] true',b'\xef\xbb\xbf{}',b'['*40+b'0'+b']'*40]:
            with self.subTest(data=data),self.assertRaises(VerifierError):loads(data)
        self.assertEqual(loads(b'{"real":0.125,"integer":9007199254740993}'),{'real':.125,'integer':9007199254740993})

    def test_paths_traversal_absolute_controls_and_parent_links(self):
        for path in ('../x','a/../x','/tmp/a','a//b','./a','a\\b','a\nname','.git/config','x/./y','',1):
            with self.subTest(path=path),self.assertRaises(VerifierError):relative(path)
        with example() as (root,effective,_):
            (root/'alias').symlink_to(root/'submission',target_is_directory=True)
            with self.assertRaises(VerifierError):snapshot(root/'alias',effective['config']['limits'])
            (root/'alias-parent').symlink_to(root,target_is_directory=True)
            with self.assertRaises(VerifierError):read_bytes(root/'alias-parent/submission/proof.md')

    def test_submission_symlink_hardlink_directory_link_and_fifo_nonblocking(self):
        for kind in ('symlink','hardlink','directory','fifo'):
            with self.subTest(kind=kind),example() as (root,effective,_):
                target=root/'submission/bad'
                if kind=='symlink':target.symlink_to('/etc/passwd')
                elif kind=='hardlink':os.link(root/'rules.md',target)
                elif kind=='directory':target.symlink_to(root,target_is_directory=True)
                else:os.mkfifo(target)
                started=time.monotonic()
                with self.assertRaises(VerifierError):intake_snapshot(effective,root/'submission')
                self.assertLess(time.monotonic()-started,1)

    def test_direct_fifo_read_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fifo';os.mkfifo(path)
            started=time.monotonic()
            with self.assertRaises(VerifierError):read_bytes(path)
            self.assertLess(time.monotonic()-started,1)

    def test_submission_limits_encoding_and_python_cookie(self):
        for mode in ('bytes','count','total','encoding','cookie','nul','dirs'):
            with self.subTest(mode=mode),example() as (root,effective,_):
                limits=copy.deepcopy(effective['config']['limits'])
                if mode=='bytes':limits['max_file_bytes']=10
                elif mode=='count':limits['max_files']=1
                elif mode=='total':limits['max_submission_bytes']=20
                elif mode=='encoding':(root/'submission/bad.md').write_bytes(b'\xff')
                elif mode=='cookie':(root/'submission/bad.py').write_text('# coding: latin-1\nprint(1)\n')
                elif mode=='nul':(root/'submission/bad.md').write_bytes(b'\x00')
                else:
                    limits['max_files']=2
                    for n in range(10):(root/f'submission/empty{n}').mkdir()
                with self.assertRaises(VerifierError):snapshot(root/'submission',limits)

    def test_protected_config_and_rules_not_in_candidate(self):
        with example() as (root,_,_):
            original=read_json(root/'verifier.json')
            write_json(root/'submission/config.json',original)
            with self.assertRaisesRegex(VerifierError,'protected_path'):load_config(root,'submission/config.json','submission')
            original['rules_path']='submission/proof.md';write_json(root/'verifier.json',original)
            with self.assertRaisesRegex(VerifierError,'protected_path'):load_config(root,'verifier.json','submission')

    def test_schema_references_unknown_types_and_unbounded_inputs(self):
        invalid=[{'$ref':'https://example.invalid/schema'}, {'type':['integer','null']},
                 {'type':'integer'}, {'type':'string'}, {'type':'number','minimum':0,'maximum':float('inf')},
                 {'type':'object','properties':{},'required':[],'additionalProperties':True},
                 {'type':'string','maxLength':10,'pattern':'.*'},
                 {'type':'array','maxItems':100000,'items':{'type':'null'}}]
        for schema in invalid:
            with self.subTest(schema=schema),self.assertRaises(VerifierError):validate_schema(schema)
        schema={'type':'object','additionalProperties':False,'required':['n'],
                'properties':{'n':{'type':'integer','minimum':0,'maximum':10},'names':{'type':'array','maxItems':2,'items':{'type':'string','maxLength':10}}}}
        validate_schema(schema)
        validate_value(schema,{'n':10,'names':['a']})
        for value in ({'n':True},{'n':10.0},{'n':11},{'n':1,'x':2},{},{'n':0,'names':['a','b','c']}):
            with self.subTest(value=value),self.assertRaises(VerifierError):validate_value(schema,value)

    def test_all_candidate_declaration_fields_are_closed(self):
        for field,value in [('image','evil:latest'),('command','sh -c anything'),('provider','fake'),('limits',{'run_seconds':100000})]:
            with self.subTest(field=field),example('computation') as (root,effective,_):
                manifest=read_json(root/'submission/submission.json');manifest['experiments'][0][field]=value
                write_json(root/'submission/submission.json',manifest)
                with self.assertRaises(VerifierError):intake_snapshot(effective,root/'submission')

    def test_organizer_hard_caps_cannot_be_raised(self):
        with example() as (_,effective,_):
            for values in ({'max_followups':9},{'memory_mb':100000},{'run_seconds':float('nan')},{'cpus':True},{'arbitrary':1}):
                config=copy.deepcopy(effective['config']);config['limits'].update(values)
                with self.subTest(values=values),self.assertRaises(VerifierError):normalize(config)
            for model in ('','auto','provider/model,other/model'):
                config=copy.deepcopy(effective['config']);config['provider']['model']=model
                with self.assertRaises(VerifierError):normalize(config)

    def test_output_parent_symlink_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'outside').mkdir();(root/'alias').symlink_to(root/'outside',target_is_directory=True)
            with self.assertRaises(VerifierError):write_json(root/'alias/new/file.json',{})
            self.assertFalse((root/'outside/new').exists())


    def test_openai_models_and_closed_provider_settings(self):
        with example() as (_, effective, _):
            for model in ('gpt-4.1', 'gpt-4.1-2025-04-14', 'o3', 'ft:gpt-4.1:org:custom:id'):
                config = copy.deepcopy(effective['config']); config['provider']['model'] = model
                self.assertEqual(normalize(config)['provider']['model'], model)
            for model in ('openai/gpt-4.1', 'auto', 'Auto', 'default', 'DEFAULT', '', 'x'*201, 'x\n', ['gpt-4.1'], None):
                config = copy.deepcopy(effective['config']); config['provider']['model'] = model
                with self.subTest(model=model), self.assertRaisesRegex(VerifierError, 'explicit_provider_model_required'):
                    normalize(config)
            for field, value in (('max_tokens',8192), ('upstream',None), ('zdr',True), ('data_collection','deny'),
                                 ('endpoint','https://invalid.example'), ('api_key','test-only')):
                config = copy.deepcopy(effective['config']); config['provider'][field] = value
                with self.subTest(field=field), self.assertRaises(VerifierError): normalize(config)
            for value in (127, 32769, True, 8192.0):
                config = copy.deepcopy(effective['config']); config['provider']['max_completion_tokens'] = value
                with self.subTest(value=value), self.assertRaises(VerifierError): normalize(config)
            for value in (None, [], {}, 'text'):
                config = copy.deepcopy(effective['config']); config['provider']['response_format'] = value
                with self.subTest(value=value), self.assertRaisesRegex(VerifierError, 'provider_settings'): normalize(config)


    def test_reasoning_effort_is_optional_and_exact(self):
        values = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
        schema = read_json(REPO/'docs/config.schema.json')['properties']['provider']
        self.assertNotIn('reasoning_effort', schema['required'])
        self.assertEqual(schema['properties']['reasoning_effort'], {'type':'string', 'enum':list(values)})
        with example() as (_, effective, _):
            for value in values:
                with self.subTest(value=value):
                    raw = copy.deepcopy(effective['config'])
                    raw['provider']['reasoning_effort'] = value
                    before = canonical(raw)
                    normalized = normalize(raw)
                    self.assertEqual(canonical(normalized), before)
                    self.assertEqual(canonical(raw), before)
                    self.assertEqual(normalized['provider']['reasoning_effort'], value)
            for value in (None, True, False, 0, 1, 1.0, [], ['high'], {}, {'effort':'high'},
                          '', 'HIGH', 'High', ' high', 'high ', 'high\n', 'auto', 'default',
                          'maximum', 'ultra', 'high,low'):
                with self.subTest(value=value):
                    raw = copy.deepcopy(effective['config'])
                    raw['provider']['reasoning_effort'] = value
                    with self.assertRaisesRegex(VerifierError, '^provider_settings$'):
                        normalize(raw)

    def test_omitted_effort_preserves_normalized_config_bytes(self):
        # Provider omission bytes remain fixed; the full fixture includes atomic policy prose.
        with example() as (_, effective, _):
            raw = copy.deepcopy(effective['config'])
            raw['provider'] = {'model':'test-model'}
            normalized = normalize(raw)
            self.assertEqual(canonical(normalized['provider']),
                b'{"max_attempts":2,"max_completion_tokens":8192,"max_response_bytes":262144,'
                b'"model":"test-model","response_format":"json_schema","timeout_seconds":60}')
            self.assertNotIn('reasoning_effort', normalized['provider'])
            self.assertEqual(digest(normalized),
                'efab4299c72272d4858b8d284dca35279b31786e12b2c1235dba2dbeda85d0f1')
            self.assertEqual(canonical(normalize(normalized)), canonical(normalized))
