"""Explicit approved definitions, managed edit maps and offline conformance."""

import copy
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from . import __version__
from .artifacts import intake_snapshot
from .config import identifier, load_config, normalize
from .errors import VerifierError
from .files import directory_fd, read_bytes, read_json, relative, utf8, write_json
from .input_schema import validate_value
from .jsonutil import canonical, digest, keys, sha, text
from .scaffold_templates import AUTHORING, BUILD_SKILL, PUBLIC_RUNNER
from .simulation import simulate

CHECKOUT = 'fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09'
RESERVED = {'verifier.json', 'benchmark.json', 'yukon-scaffold.json', 'AUTHORING.md',
            'public-checks.json', 'tools/public_conformance.py',
            '.agents/skills/build-challenge/SKILL.md',
            '.github/workflows/benchmark.yml', '.github/workflows/public-conformance.yml'}


def definition(raw):
    keys(raw, {'version', 'name', 'description', 'verifier', 'files', 'edit_paths', 'conformance'}, {'checker_cases'})
    if type(raw['version']) is not int or raw['version'] != 1:
        raise VerifierError('definition_version')
    identifier(raw['name']); text(raw['description'], 2000)
    config = normalize(raw['verifier'])
    if not config['objectives']:
        raise VerifierError('scaffold_yukon_objective_required')
    files = raw['files']
    if type(files) is not dict or not 1 <= len(files) <= 128 or len(canonical(files)) > 4 * 1024 * 1024:
        raise VerifierError('definition_file_limit')
    for path, content in files.items():
        relative(path)
        if (path in RESERVED or path.startswith(('.git/', '.github/', '.agents/', '.yukon/', 'tools/'))
                or type(content) is not str or len(content.encode('utf-8')) > 262144):
            raise VerifierError('definition_file_reserved_or_large')
        utf8(content.encode('utf-8'), python=path.endswith('.py'))
        if any(parent.as_posix() in files for parent in Path(path).parents if parent != Path('.')):
            raise VerifierError('definition_file_directory_collision')
    if config['rules_path'] not in files or 'submission/submission.json' not in files:
        raise VerifierError('definition_rules_and_submission_required')
    for checker in config.get('checkers', []):
        if not set(checker['files']) <= files.keys():
            raise VerifierError('definition_checker_bundle_missing')
    edits = raw['edit_paths']
    if (type(edits) is not list or any(type(p) is not str or p not in files for p in edits)
            or len(set(edits)) != len(edits) or config['rules_path'] in edits):
        raise VerifierError('definition_edit_map')
    cases = raw['conformance']
    if type(cases) is not list or not 1 <= len(cases) <= 32:
        raise VerifierError('definition_conformance_required')
    ids = set()
    for case in cases:
        keys(case, {'id', 'submission_path', 'script_path', 'expected'})
        identifier(case['id'])
        if case['id'] in ids:
            raise VerifierError('definition_duplicate_case')
        ids.add(case['id'])
        relative(case['submission_path']); relative(case['script_path'])
        if case['submission_path'] + '/submission.json' not in files or case['script_path'] not in files:
            raise VerifierError('definition_case_files')
        keys(case['expected'], {'valid', 'scores'}, {'score_records'})
        if type(case['expected']['valid']) is not bool:
            raise VerifierError('definition_case_validity')
    registry = {c['id']: c for c in config.get('checkers', [])}
    checker_cases = raw.get('checker_cases', [])
    if type(checker_cases) is not list or len(checker_cases) > 64:
        raise VerifierError('definition_checker_case_limit')
    ids = set()
    for case in checker_cases:
        keys(case, {'id', 'check', 'inputs', 'expected'})
        identifier(case['id'])
        if case['id'] in ids or type(case['check']) is not str or case['check'] not in registry:
            raise VerifierError('definition_checker_case_identity')
        ids.add(case['id'])
        validate_value(registry[case['check']]['input_schema'], case['inputs'])
        # Negative tests may deliberately produce schema-invalid output.
        if type(case['expected']) is not dict or len(canonical(case['expected'])) > 16384:
            raise VerifierError('definition_checker_expected')
    return raw


def options(repository, revision, model):
    if type(repository) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise VerifierError('scaffold_repository')
    if type(revision) is not str or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise VerifierError('scaffold_revision')
    if type(model) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+', model) or model == 'ORGANIZER/MODEL':
        raise VerifierError('scaffold_explicit_model')
    return {'verifier_repository': repository, 'verifier_revision': revision, 'model': model}


def render(raw, selected):
    raw = definition(raw)
    selected = options(selected['verifier_repository'], selected['verifier_revision'], selected['model'])
    config = copy.deepcopy(raw['verifier'])
    config['provider']['model'] = selected['model']
    config = normalize(config)
    track = {'name': 'main', 'description': raw['description'], 'category': 'math', 'mode': 'pareto',
             'promotionMode': 'manual', 'direction': config['objectives'][0]['direction'],
             'objectives': [{k: item[k] for k in ('name', 'direction')} for item in config['objectives']],
             'paretoPolicies': config['policies'], 'editablePaths': ['submission'],
             'setupCommand': ['llm-verifier', 'intake', '--root', '.', '--out', '.yukon/work/intake.json'],
             'benchmarkCommand': ['llm-verifier', 'finalize', '--intake', '.yukon/work/intake.json',
                                  '--initial', '.yukon/work/initial.json', '--initial-review', '.yukon/work/review.json',
                                  '--score', '.yukon/score.json', '--dossier', '.yukon/dossier.json'],
             'scorePath': '.yukon/score.json', 'maxSubmissionBytes': config['limits']['max_submission_bytes'],
             'runner': {'provider': 'github-actions', 'workflow': 'benchmark.yml',
                        'scoreArtifactName': 'benchmark-score', 'maxConcurrentWorkflows': 2}}
    files = dict(raw['files'])
    files['verifier.json'] = canonical(config).decode() + '\n'
    files['benchmark.json'] = canonical({'schemaVersion': 2, 'name': raw['name'], 'tracks': [track]}).decode() + '\n'
    files['public-checks.json'] = canonical(raw.get('checker_cases', [])).decode() + '\n'
    repo, revision = selected['verifier_repository'], selected['verifier_revision']
    files['.github/workflows/benchmark.yml'] = f'''# Access and read-only checkout credentials must be configured separately.
name: Benchmark
on:
  workflow_dispatch:
    inputs:
      yukon_evaluation:
        type: string
        required: false
        default: ''
permissions:
  contents: read
jobs:
  verify:
    uses: {repo}/.github/workflows/verify.yml@{revision}
    with:
      verifier_repository: {repo}
      verifier_revision: {revision}
      root_dir: '.'
      config_path: verifier.json
      submission_path: submission
      score_path: .yukon/score.json
      yukon_evaluation: ${{{{ inputs.yukon_evaluation }}}}
    secrets:
      OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
      VERIFIER_CHECKOUT_TOKEN: ${{{{ secrets.VERIFIER_CHECKOUT_TOKEN }}}}
'''
    files['.github/workflows/public-conformance.yml'] = f'''name: Public checker conformance
on: [push, pull_request, workflow_dispatch]
permissions:
  contents: read
jobs:
  public-checkers:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@{CHECKOUT}
        with:
          persist-credentials: false
          submodules: false
          lfs: false
      - name: Explicit credential-free pinned image setup
        run: docker pull '{config['runtime']['image']}'
      - name: Public known-answer checker cases (no model review)
        run: python3 tools/public_conformance.py --root .
'''
    files['.agents/skills/build-challenge/SKILL.md'] = BUILD_SKILL
    files['AUTHORING.md'] = AUTHORING
    files['tools/public_conformance.py'] = PUBLIC_RUNNER
    inventory = {path: {'sha256': sha(content.encode()),
                       'role': 'builder-edit' if path in raw['edit_paths'] else 'protected'}
                 for path, content in sorted(files.items())}
    scaffold = {'version': 1, 'recipe': 'llm-verifier', 'package_version': __version__,
                'definition_sha256': digest(raw), 'selected': selected,
                'builder_edit_paths': raw['edit_paths'], 'candidate_edit_paths': ['submission'],
                'files': inventory, 'qualification': 'not_performed'}
    files['yukon-scaffold.json'] = canonical(scaffold).decode() + '\n'
    return files, scaffold


def initialize(raw, output, selected, receipt_path=None):
    files, scaffold = render(raw, selected)
    output = Path(os.path.abspath(output))
    receipt_path = Path(os.path.abspath(receipt_path or str(output) + '.receipt.json'))
    if receipt_path.is_relative_to(output):
        raise VerifierError('controller_receipt_must_be_external')
    with directory_fd(output.parent):
        if os.path.lexists(output) or os.path.lexists(receipt_path):
            raise VerifierError('scaffold_output_or_receipt_exists')
        with directory_fd(receipt_path.parent):
            staging = Path(tempfile.mkdtemp(prefix='.llmv-init-', dir=output.parent))
            try:
                for path, content in files.items():
                    destination = staging / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content.encode('utf-8'))
                # Validate all immutable source, configuration and cases before
                # admitting the new export. No code/model execution occurs.
                report = _check(staging, raw, selected, scaffold)
                os.rename(staging, output)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    receipt = {'version': 1, 'definition_sha256': digest(raw), 'selected': selected,
               'scaffold_sha256': digest(scaffold)}
    write_json(receipt_path, receipt)
    return {'root': str(output), 'receipt': str(receipt_path), 'scaffold_sha256': digest(scaffold),
            'validation': report, 'publishable': False}


def trusted(root, raw, receipt):
    keys(receipt, {'version', 'definition_sha256', 'selected', 'scaffold_sha256'})
    if type(receipt['version']) is not int or receipt['version'] != 1 or receipt['definition_sha256'] != digest(raw):
        raise VerifierError('scaffold_definition_receipt_binding')
    keys(receipt['selected'], {'verifier_repository', 'verifier_revision', 'model'})
    _, expected = render(raw, receipt['selected'])
    if digest(expected) != receipt['scaffold_sha256']:
        raise VerifierError('scaffold_receipt_binding')
    current = read_json(Path(root) / 'yukon-scaffold.json')
    if canonical(current) != canonical(expected):
        raise VerifierError('scaffold_inventory_binding')
    return expected


def verify_inventory(root, expected_files):
    """Exact export inventory; skip only root Git metadata without traversing it."""
    expected_files = set(expected_files)
    expected_dirs = {parent.as_posix() for path in expected_files
                     for parent in Path(path).parents if parent != Path('.')}
    maximum = len(expected_files) + len(expected_dirs) + 1
    count = 0
    def walk(fd, prefix=''):
        nonlocal count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > maximum:
                    raise VerifierError('scaffold_unexpected_path')
                path = prefix + entry.name
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise VerifierError('scaffold_symlink')
                if path == '.git' and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    # A checkout directory or a worktree pointer is metadata;
                    # do not read it or follow its internal links/content.
                    continue
                relative(path)
                if stat.S_ISDIR(info.st_mode):
                    if path not in expected_dirs:
                        raise VerifierError('scaffold_unexpected_path')
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, path + '/')
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or path not in expected_files:
                    raise VerifierError('scaffold_unexpected_path')
    with directory_fd(root) as fd:
        walk(fd)


def _check(root, raw, selected, expected):
    files, _ = render(raw, selected)
    verify_inventory(root, files)
    observed = {}
    for path, original in files.items():
        payload = read_bytes(Path(root) / path, 4 * 1024 * 1024)
        utf8(payload, python=path.endswith('.py'))
        observed[path] = sha(payload)
        if path not in raw['edit_paths'] and payload != original.encode():
            raise VerifierError('scaffold_protected_file_changed')
    effective = load_config(root, 'verifier.json', 'submission')
    intake_snapshot(effective, Path(root) / 'submission')
    results = []
    for case in raw['conformance']:
        case_effective = load_config(root, 'verifier.json', case['submission_path'])
        with tempfile.TemporaryDirectory(prefix='llmv-conformance-') as tmp:
            result = simulate(case_effective, Path(root) / case['submission_path'],
                              read_json(Path(root) / case['script_path']), Path(tmp) / 'simulation.json')
        actual = result['data']['judgment']
        if any(canonical(actual[key]) != canonical(case['expected'][key]) for key in ('valid', 'scores')):
            raise VerifierError('scaffold_conformance_mismatch')
        if 'score_records' in case['expected'] and canonical(result['data']['score_records']) != canonical(case['expected']['score_records']):
            raise VerifierError('scaffold_score_records_mismatch')
        results.append({'id': case['id'], 'passed': True, 'simulation_only': True})
    return {'valid': True, 'definition_sha256': digest(raw), 'observed_files': observed,
            'conformance': results, 'checker_execution': 'not_run', 'model_review': 'not_run',
            'qualification': 'not_performed'}


def check(root, raw, receipt):
    expected = trusted(root, raw, receipt)
    return _check(root, raw, receipt['selected'], expected)


def context(root, task, raw=None, receipt=None):
    if task == 'solve':
        config = normalize(read_json(Path(root) / 'verifier.json'))
        return {'task': task, 'read': ['AUTHORING.md', config['rules_path'], 'verifier.json', 'benchmark.json'],
                'inspect': [], 'edit': ['submission'], 'authority': 'participant; organizer artifacts remain protected'}
    if raw is None or receipt is None:
        raise VerifierError('managed_context_requires_definition_and_receipt')
    expected = trusted(root, raw, receipt)
    # Protected bytes must still match, without running even a simulation.
    files, _ = render(raw, receipt['selected'])
    verify_inventory(root, files)
    for path, content in files.items():
        if path not in raw['edit_paths'] and read_bytes(Path(root) / path) != content.encode():
            raise VerifierError('scaffold_protected_file_changed')
    return {'task': task, 'read': ['AUTHORING.md', '.agents/skills/build-challenge/SKILL.md',
                                  raw['verifier']['rules_path'], 'verifier.json', 'benchmark.json'],
            'inspect': sorted(set(raw['files']) - set(raw['edit_paths'])),
            'edit': expected['builder_edit_paths'] if task == 'build' else [],
            'definition_sha256': expected['definition_sha256'],
            'authority': 'managed edit-only; external controller owns definition, receipt and final inventory'}
