# Prepare an llm-verifier submission

Use this guide with the verifier revision selected by the challenge. Starting
from only a challenge clone, read `verifier_repository` and `verifier_revision`
in `.github/workflows/benchmark.yml` (or obtain the exact pair from its author).
Replace both placeholders below with that repository and full 40-character commit:

```sh
python3 -m venv /path/to/verifier-env
/path/to/verifier-env/bin/python -m pip install 'llm-verifier @ git+https://github.com/VERIFIER_REPOSITORY.git@FULL_40_CHARACTER_COMMIT'
export PATH="/path/to/verifier-env/bin:$PATH"
```

Use that environment for all commands below. Installing may need repository
access and downloads build tools; it needs no model credential. Do not replace
this source pin with a package version, branch or tag.

Generated participant README and AGENTS files link to that full commit after `github`
generation. Before pinning, read this file in the source used to install the CLI,
or the wheel's `share/llm-verifier/docs/solving.md`. Locate the wheel's shared
directory with the same Python environment used to install `llm-verifier`:

```sh
python -c "import sysconfig; print(sysconfig.get_path('data') + '/share/llm-verifier')"
```

The thin skill is at `skills/llm-verifier-solver/SKILL.md` beside that `docs`
directory. A copied skill or a package version number alone does not establish
the challenge's source revision. If the pin or matching source is unavailable,
ask the author and report the gap; do not silently use the latest branch.

## Read the protected context

Read the participant instructions, `verifier.json`, its `rules_path`, objectives,
score sources/bounds, checker definitions and applicable agent instructions.
Determine the selected scoring and validity policies and their corresponding
`supported_policies` instructions. Local intake defaults to `config.policies`;
an organizer-supplied `YUKON_EVALUATION` (or `--evaluation` JSON) may select others.
Use the actual evaluation context when available and state any assumption.
Do not edit protected rules, configuration, checkers or policies to pass a check.

`submission/submission.json` declares the entire claim set: acceptance is atomic,
not a subset or a weakened rewrite. State material hypotheses, scope, parameters
and nonstandard definitions in each declared claim; place its complete supporting
argument in the referenced proof files. Prior results supply only their exact
accepted declared claims, not incidental proof/reason prose or current scores
and checker passes. Familiar standard results require the correct variant and
hypotheses. Reproduce and justify any unfamiliar or uncertain material external
premise in supplied evidence; a citation or URL alone is insufficient. The
configured judge has no web/PDF retrieval.

## Validate and run the final candidate

Use the challenge's pinned verifier installation, with outputs outside
`submission/` and other candidate-editable paths:

```sh
llm-verifier check --root /path/to/challenge
llm-verifier intake --root /path/to/challenge --out /path/to/work/intake.json
llm-verifier prepare-runtime --intake /path/to/work/intake.json
llm-verifier experiments --intake /path/to/work/intake.json --out /path/to/work/initial.json
```

`check` validates configuration, submission structure, evidence references and
organizer checker cases; it executes no submitted code and proves no mathematics.
`intake` freezes the candidate, rules, selected policies and implementation.
`experiments` runs the submission's declared initial experiments and checks,
using their exact inputs. Inspect its per-run status and output, including failed
or missing required checks; command completion alone does not mean a witness passed.
`test-checkers` runs organizer known-answer fixtures and cannot substitute for
candidate execution. Passing a checker establishes only its scoped predicate,
not every claim or the correctness of the whole submission.

Use a credential-free execution process/worker. `prepare-runtime` explicitly
pulls the configured digest-pinned Linux/amd64 Docker image when work is present;
there is no host execution fallback. A proof-only candidate with an empty initial
plan needs no Docker execution. Report that the plan was empty, not that
computational evidence passed. If Docker, an image or the required environment
is unavailable, report the omitted execution and its consequence.

For a submission declaring `prior_results`, local structural checking still
works, but intake needs the original consumer admission enabling references and
authentic platform context. The hosted workflow supplies `YUKON_EVALUATION` and
`YUKON_ACCEPTED_RESULTS` and authenticates automatic resolution. An ordinary
local shell or downloaded discovery response does not supply that authority.
Use the authorized platform evaluation path or report authenticated intake as
unavailable. Do not fabricate admission, accepted-result bundles, contexts or
acceptance to get past intake, or remove declared references and call the changed
candidate equivalent. `inspect-results` is informational, not intake authorization.

## Request independent critique

After preparing the final candidate and available execution evidence, ask a fresh
subagent that did not write the solution to critique it. Supply the complete
final manifest and all submission files, protected rules/configuration, selected
policy instructions, checker definitions, execution results and any authenticated
prior-result evidence. Include exact paths or snapshot identifiers; do not replace
the submission with your summary, omit failed runs or give only selected excerpts.
Give it this guide and applicable protected policies. If context limits prevent a
complete review, arrange complete access or report that limitation.

Ask it to look for counterexamples, proof gaps, unjustified external premises,
missing hypotheses, scope changes, scoring mistakes and discrepancies between
declared inputs and computations. It should review every declared claim, identify
concrete findings with file/claim references, and say what it could not verify.
Prior unavailable/rejected results are neither accepted premises nor proofs of
their negations. A fresh critique is advisory, not the configured verifier judge
and not platform acceptance. There is no `llm-verifier` subagent command. If no
independent subagent is available, disclose that rather than relabeling self-review.

Fix supported findings. Any change to the candidate, protected context or verifier
requires a fresh intake and rerunning affected checks; old phase artifacts cannot
be reused with a new intake. Recheck structure and declared computations for the
final bytes, then have the independent reviewer revisit affected claims and
evidence. Report unresolved findings instead of claiming the submission is ready.

## Configured-provider evaluation is separate

Only when authorized, run the configured verifier review in a separate process/job
with its selected model and `OPENAI_API_KEY`. It consumes the trusted intake and
execution artifacts; it never executes candidates:

```sh
llm-verifier review-initial --intake /path/to/work/intake.json \
  --initial /path/to/work/initial.json --out /path/to/work/review.json
```

If `.data.decision.kind` is `followup`, transfer artifacts to a fresh
credential-free execution worker, then to a separate credentialed review job:

```sh
# Credential-free execution worker:
llm-verifier prepare-runtime --phase followup --intake /path/to/work/intake.json
llm-verifier followup --intake /path/to/work/intake.json --initial /path/to/work/initial.json \
  --initial-review /path/to/work/review.json --out /path/to/work/followup.json
# Separate credentialed review job:
llm-verifier review-final --intake /path/to/work/intake.json --initial /path/to/work/initial.json \
  --initial-review /path/to/work/review.json --followup /path/to/work/followup.json \
  --out /path/to/work/final-review.json
```

Finalize without model credentials after the required chain is complete:

```sh
# Direct judgment (no follow-up requested):
llm-verifier finalize --intake /path/to/work/intake.json --initial /path/to/work/initial.json \
  --initial-review /path/to/work/review.json --score /path/to/work/score.json \
  --dossier /path/to/work/dossier.json
# If a follow-up was requested, also pass:
# --followup /path/to/work/followup.json --final-review /path/to/work/final-review.json
```

Preserve trusted artifact origins and the independently trusted intake digest
across transfers (`--expected-intake`); hashes alone do not authenticate
candidate-provided artifacts. `simulate` uses scripted responses and execution
results. It tests plumbing, does not judge mathematical correctness and cannot
establish official acceptance. Neither a passing simulation, an advisory critique
nor a local model opinion replaces the challenge's authorized evaluation and
acceptance process.

## Report preparation

Record the verifier revision and final candidate identity, selected policies,
structural-check result, actual computations and their outputs, independent
review findings/fixes, and configured-provider evaluation status if run. Name
each omitted or incomplete step and why (for example no subagent, no Docker or
no authentic prior-result context), with its effect on confidence. Keep evidence
outside the submission and do not describe unperformed checks as passed.
