from contextlib import contextmanager
import copy
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from llm_verifier.artifacts import intake_snapshot, seal
from llm_verifier.config import load_config
from llm_verifier.jsonutil import canonical
from llm_verifier.provider import HttpResponse, HttpTransport
from llm_verifier.runner import DockerRunner, output_record
from llm_verifier import pipeline

REPO = Path(__file__).resolve().parents[1]


def reseal(value):
    return seal(value['stage'], value['bindings'], value['data'], value['parents'], value['simulation'])


@contextmanager
def example(name='proof', mutate=None, evaluation=None):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)/'challenge'
        shutil.copytree(REPO/'examples'/name,root)
        raw=json.loads((root/'verifier.json').read_text())
        raw['provider']['model']='test-model'
        if mutate:
            mutate(raw)
        (root/'verifier.json').write_text(json.dumps(raw))
        effective=load_config(root,'verifier.json','submission',evaluation)
        yield root, effective, intake_snapshot(effective,root/'submission')


def completion(decision, **updates):
    value={'id':'chatcmpl-test','object':'chat.completion','model':'test-model','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(decision)}}], 'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}
    value.update(updates)
    return HttpResponse(200,canonical(value))


@contextmanager
def scripted_model(decisions):
    # Mock only the trusted network boundary, never production provider choice.
    responses=iter([completion(d) for d in decisions])
    with patch.dict('os.environ',{'OPENAI_API_KEY':'never-real-test-key'}), patch.object(HttpTransport,'request',side_effect=lambda *a: next(responses)):
        yield


def direct(root,intake,decision=None):
    if decision is None:
        decision=json.loads((root/'simulation.json').read_text())['initial_decision']
    with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{"checked_n":10,"all_equal":true}',b'')):
        initial=pipeline.execute(intake)
    with scripted_model([decision]):
        first=pipeline.review(intake,initial)
    return initial,first
