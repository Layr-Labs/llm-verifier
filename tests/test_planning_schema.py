"""Check emitted planner schemas independently against the failing source shape."""

import copy
import json
import unittest
from unittest.mock import patch

from llm_verifier import committee as protocol, pipeline, regions, submission
from llm_verifier.errors import VerifierError
from llm_verifier.jsonutil import sha
from tests.support import REPO, completion, request_parts
from tests.test_committees import committee, report
from tests.test_regions import PROFILE, plan_for, region_report


def satisfies(schema, value):
    # Closed evaluator for every keyword emitted by the planner. Unknown schema
    # keywords fail this test instead of being silently ignored.
    kind = schema['type']
    allowed = {'object': {'properties', 'required', 'additionalProperties'},
               'array': {'items', 'minItems', 'maxItems'},
               'string': {'minLength', 'maxLength'},
               'integer': {'minimum', 'maximum'}}[kind]
    assert schema.keys() <= allowed | {'type'}
    if kind == 'object':
        assert type(value) is dict and set(schema['required']) <= value.keys()
        assert schema['additionalProperties'] is False and value.keys() <= schema['properties'].keys()
        for key, item in value.items(): satisfies(schema['properties'][key], item)
    elif kind == 'array':
        assert type(value) is list and schema.get('minItems', 0) <= len(value) <= schema['maxItems']
        for item in value: satisfies(schema['items'], item)
    elif kind == 'string':
        assert type(value) is str and schema.get('minLength', 0) <= len(value) <= schema['maxLength']
    elif kind == 'integer':
        assert type(value) is int and schema.get('minimum', -float('inf')) <= value <= schema.get('maximum', float('inf'))


class PlanningSchema(unittest.TestCase):
    def test_seven_file_candidate_zero_and_nine_are_rejected_on_wire(self):
        fixture = json.loads((REPO / 'tests/fixtures/planner-count-regression.json').read_text())
        files = {}
        for path, count in fixture['source_line_counts'].items():
            text = 'Inert regression line.\n' * count
            files[path] = {'text': text, 'size': len(text), 'sha256': sha(text.encode())}
        self.assertEqual(len(files), 7)
        nine = fixture['synthetic_nine_regions']
        self.assertEqual(len(nine['regions']), 9)
        self.assertEqual(len(regions.freeze_plan(nine, files, 9)['regions']), 9)
        with committee(judging=PROFILE) as (_, effective, intake):
            planner = next(m for m in effective['judging']['committee']['members'] if m['stage'] == 'planning')
            schema = protocol.stage_schema(planner, effective['config'], intake['data']['manifest'], False,
                                           max_regions=8)
            for plan in ({'regions': []}, nine):
                with self.assertRaises(AssertionError): satisfies(schema, plan)
                with self.assertRaisesRegex(VerifierError, 'region_plan_count'):
                    regions.freeze_plan(plan, files, 8)
            # Demonstrate the old wire schema accepted both invalid plans.
            old = copy.deepcopy(schema)
            old['properties']['regions'].pop('minItems')
            old['properties']['regions']['maxItems'] = 16
            satisfies(old, nine)
            satisfies(old, {'regions': []})

    def test_selected_nondefault_caps_match_preflight_request_and_replay(self):
        for cap in (2, 3, 12, 16):
            with self.subTest(cap=cap), committee(judging={**PROFILE, 'max_regions': cap}) as (root, effective, intake):
                preflight = []
                original = protocol.stage_schema
                def capture(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if args[0]['stage'] == 'planning': preflight.append(result)
                    return result
                with patch.object(protocol, 'stage_schema', side_effect=capture):
                    submission.preflight_manifest(intake['data']['files'], effective['config'],
                                                   committee=effective['judging']['committee'])
                self.assertEqual(len(preflight), 2)
                for schema in preflight:
                    self.assertEqual(schema['properties']['regions']['minItems'], 1)
                    self.assertEqual(schema['properties']['regions']['maxItems'], cap)
                initial = pipeline.execute(intake)
                plan = plan_for(intake)
                decision = json.loads((root / 'simulation.json').read_text())['initial_decision']
                bodies = []
                def request(body, *args):
                    bodies.append(json.loads(body))
                    _, member, context = request_parts(body)
                    if member['stage'] == 'planning': value = plan
                    elif member['stage'] == 'region': value = region_report(context['region'])
                    elif member['stage'] == 'adjudication': value = {'decision': decision, 'resolutions': []}
                    else: value = report()
                    return completion(value)
                with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-only'}), patch(
                        'llm_verifier.provider.HttpTransport.request', side_effect=request):
                    first = pipeline.review(intake, initial)
                wire = next(b['response_format']['json_schema']['schema'] for b in bodies
                            if 'regions' in b['response_format']['json_schema']['schema']['properties'])
                self.assertEqual(wire, preflight[0])
                satisfies(wire, plan)
                with self.assertRaises(AssertionError):
                    satisfies(wire, {'regions': [plan['regions'][0]] * (cap + 1)})
                pipeline.verify_initial_review(intake, initial, first)
