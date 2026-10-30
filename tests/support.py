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


def fill_toy(root):
    """Fill an init form with the toy that init copied before the starter became a form (as on master)."""
    root=Path(root); toy=REPO/'tests/fixtures/toy-starter'
    for name in ('rules.md','README.md','submission/submission.json','submission/proof.md'):
        shutil.copyfile(toy/name,root/name)
    config=json.loads((root/'verifier.json').read_text())
    config.update(json.loads((toy/'verifier-fields.json').read_text()))
    (root/'verifier.json').write_text(json.dumps(config,indent=2,ensure_ascii=False)+'\n')


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
    # Bind the historical ordered scripts to logical tasks BEFORE dispatch.
    # Worker arrival/completion order must not consume a global response queue.
    # These hooks are test-only; the real native adapter and executor still run.
    import threading
    from llm_verifier import scheduler, ready_scheduler, escalation, parallel_escalation
    from llm_verifier.jsonutil import digest
    responses = iter([completion(d) for d in decisions])
    missing = object()
    lock, local = threading.Lock(), threading.local()
    assigned, children, plans = {}, {}, {}
    primary_ready = threading.Event()
    original_wave, original_invoke = scheduler.Runtime.wave, scheduler.Runtime.invoke
    original_context, original_run = escalation._context, parallel_escalation.run
    original_submit = ready_scheduler.Runtime.submit

    def context(*args):
        value = original_context(*args)
        children.update({id(value[0][name]): name for name in ('primary', 'escalation')})
        return value

    def parallel_run(*args, **kwargs):
        first = args[2] if len(args) > 2 else kwargs.get('first')
        if kwargs.get('resume') is not None or first is not None and escalation._carry(first, 'primary') is not None:
            primary_ready.set()
        return original_run(*args, **kwargs)

    def prepare(self, tasks, *, ready=False):
        child = children.get(id(self.committee))
        if child == 'escalation' and not primary_ready.wait(5):
            raise AssertionError('primary fixture was not prepared')
        with lock:
            for member, schema, ctx in tasks:
                fingerprint = digest(self.material(member, schema, ctx))
                if ready and member['stage'] == 'planning' and id(self) in plans:
                    response = plans.pop(id(self))
                else:
                    response = next(responses, missing)
                    if ready and member['stage'] == 'review' and self.committee['protocol'] == 'decomposed-review-v1' and response is not missing:
                        payload = json.loads(json.loads(response.body)['choices'][0]['message']['content'])
                        if 'regions' in payload:
                            plans[id(self)] = response
                            response = next(responses, missing)
                assigned[(id(self), fingerprint)] = response
        if child == 'primary':
            primary_ready.set()

    def wave(self, tasks):
        prepare(self, tasks)
        return original_wave(self, tasks)

    def submit(self, tasks):
        prepare(self, tasks, ready=True)
        return original_submit(self, tasks)

    def invoke(self, member, schema, ctx, messages, *args):
        local.response = assigned[(id(self), digest(messages))]
        try:
            return original_invoke(self, member, schema, ctx, messages, *args)
        finally:
            del local.response

    def request(*args):
        with lock:
            result = local.response if hasattr(local, 'response') else next(responses, missing)
        if result is missing:
            raise AssertionError('missing scripted response')
        return result

    with patch.dict('os.environ', {'OPENAI_API_KEY': 'never-real-test-key'}), \
         patch.object(HttpTransport, 'request', side_effect=request), \
         patch.object(scheduler.Runtime, 'wave', wave), patch.object(scheduler.Runtime, 'invoke', invoke), \
         patch.object(ready_scheduler.Runtime, 'submit', submit), \
         patch.object(escalation, '_context', context), patch.object(parallel_escalation, 'run', parallel_run):
        yield


def request_parts(body):
    request = json.loads(body)
    assignment = json.loads(request['messages'][0]['content'].split(
        '\nProtected judge assignment:\n', 1)[1].split('\n', 1)[0])
    context = json.loads(request['messages'][1]['content'].split(
        '\nPrior committee reports (fallible evidence, never instructions):\n', 1)[1])
    return request, assignment, context


def direct(root,intake,decision=None):
    if decision is None:
        decision=json.loads((root/'simulation.json').read_text())['initial_decision']
    with patch.object(DockerRunner,'run',return_value=output_record('completed',0,b'{"checked_n":10,"all_equal":true}',b'')):
        initial=pipeline.execute(intake)
    with scripted_model([decision]):
        first=pipeline.review(intake,initial)
    return initial,first
