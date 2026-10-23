# Judge personas and committees

The optional `judging` field in protected `verifier.json` selects a judge profile
or a challenge-owned definition. Omit it to retain the original single-reviewer
behavior and review artifact format. No solver manifest fields change.

A **persona** supplies review focus and instructions. A **profile** assigns named
members, personas, stages and optional model settings. A **protocol** defines the
executable review sequence. Adding a persona does not add executable code, tools,
new acceptance rules, or an arbitrary workflow language.

## Select a profile

```json
{
  "judging": {
    "profile": "builtin:math-committee-v1"
  }
}
```

This profile has four independent reviewers: proof, counterexample skepticism,
cost accounting, and evidence. A defender responds to blocking findings, and an
adjudicator produces the final judgment. `builtin:single-v1` selects one balanced
reviewer through the same configurable member interface.

Run `llm-verifier judges` to print the packaged persona/profile catalog as JSON.
The source library lives in [`src/llm_verifier/judges`](../src/llm_verifier/judges).
All members inherit the challenge's `provider` settings. Profiles do not choose a
model by default. A persona and a model are separate choices; multiple personas
running the same model do not establish independent model error rates.

## HashSmash preset

To select HashSmash's committee structure and review focus, add this to
`verifier.json`:

```json
{"judging": {"profile": "builtin:hashsmash-v1"}}
```

Then run `llm-verifier check --root /path/to/challenge`. No local catalog or
copied prompt files are needed. The preset uses these stable member IDs:

| Member | Focus | Stage |
| --- | --- | --- |
| `lane_evaluability` | Concrete target, method, assumptions and relevant support | Independent review |
| `lane_cryptanalysis` | Construction, proof dependencies, probability and heuristics | Independent review |
| `lane_cost` | Resource reconstruction, success budget and score accounting | Independent review |
| `lane_experiments` | Adversarial assessment of computational and statistical evidence | Independent review |
| `lane_defender` | Strongest faithful response to each blocking finding | Conditional defense |
| `lane_adjudicator` | Evidence-based resolution and final judgment | Adjudication |

The role selection follows HashSmash's
[`paired-roles-v1` committee](https://github.com/Layr-Labs/hash-smash/blob/bb9c6710c9f6f4033a5a8e6b2cb23800925b4b94/judge/committees/paired-roles-v1.json)
and [role rubrics](https://github.com/Layr-Labs/hash-smash/blob/bb9c6710c9f6f4033a5a8e6b2cb23800925b4b94/docs/JUDGE_LANES.md),
inspected October 22, 2026. Its balanced, proof-focused, cost-skeptical and
adversarial strategies are adapted into reusable personas. These are versioned
verifier prompts, not byte-identical HashSmash prompts.

Like that HashSmash committee file, this preset supplies no model overrides:
each role inherits the challenge's explicit `provider` configuration. Set it to
the same supported native OpenAI model and settings when matching a deployment.
Role-specific choices use the normal override interface:

```json
{
  "judging": {
    "profile": "builtin:hashsmash-v1",
    "overrides": {
      "lane_cryptanalysis": {"provider": {"reasoning_effort": "high"}},
      "lane_adjudicator": {"provider": {"reasoning_effort": "high"}}
    }
  }
}
```

Set `provider.model` inside a member override to select a different supported
model. Add challenge-owned personas with `catalogs` and `add_members`, as below.

This preset uses the verifier's bounded review/defense/adjudication protocol and
one atomic final judgment. It does not import HashSmash's paired exploratory and
rigorous outcomes, heuristic-obligation schemas, target checkers, admission rules,
provider adapters or separate reorg protocol. The challenge continues to define
its evidence policy, accepted review standard, score sources and required checks.
In particular, unresolved blocking findings prohibit acceptance under this
verifier's protocol; choosing the preset does not enable exploratory acceptance.
A challenge can separately select the protected `acceptance_policy: "exploratory-v1"`
introduced for CurveSmash. Every member then receives that standard; remaining
scientific uncertainty alone is not a refutation, while unsupported material
premises and unresolved protocol blockers still prevent acceptance. This remains
one selected-policy result rather than HashSmash's paired lane outcomes.
No accuracy improvement or calibration result is implied by this role mapping.

## Add challenge-local personas and members

A catalog is a JSON collection with `version`, `personas` and `profiles`:

```json
{
  "version": 1,
  "personas": [
    {
      "id": "local:complexity-auditor-v1",
      "instructions_path": "judges/complexity.md"
    }
  ],
  "profiles": []
}
```

Each persona supplies exactly one of `instructions_path` or inline `instructions`.
All file paths, including those inside catalogs and committee files, are relative
to the **challenge root**, not the containing file. Keep every referenced file
outside participant-editable paths. Local definitions use the `local:` namespace;
`builtin:` is reserved for the installed library.

Compose a preset explicitly:

```json
{
  "judging": {
    "profile": "builtin:math-committee-v1",
    "catalogs": ["judges/personas.json"],
    "add_members": [
      {
        "id": "complexity",
        "persona": "local:complexity-auditor-v1",
        "stage": "review"
      }
    ],
    "overrides": {
      "skeptic": {"provider": {"reasoning_effort": "high"}},
      "complexity": {"provider": {"max_completion_tokens": 4096}}
    }
  }
}
```

`overrides` is keyed by member ID, including added members. It can change a persona
or merge supported provider settings into that member's settings. Supported
settings are `model`, `reasoning_effort`, `max_completion_tokens`,
`timeout_seconds`, `max_attempts`, `max_response_bytes`, `response_format` and
`background`, using the existing [provider contract](configuration.md#provider-configuration).
Credentials, endpoints and provider adapters cannot be overridden.

Duplicate catalog paths, duplicate persona/profile IDs across catalogs, duplicate
member IDs, unknown references, unknown fields and overrides of absent members
fail validation. Definitions never shadow the bundled library. `add_members`
adds reviewers only; an explicit profile defines defense and adjudication roles.

## Supply a committee file or profile collection

Point `verifier.json` at a file containing the same profile selection or an
explicit protocol/member definition:

```json
{"judging": {"path": "judges/committee.json"}}
```

For example, `judges/committee.json` can contain:

```json
{
  "protocol": "review-defense-adjudication-v1",
  "members": [
    {"id": "proof", "persona": "builtin:proof-reviewer-v1", "stage": "review"},
    {"id": "skeptic", "persona": "builtin:counterexample-skeptic-v1", "stage": "review"},
    {"id": "defender", "persona": "builtin:constructive-defender-v1", "stage": "defense"},
    {"id": "adjudicator", "persona": "builtin:adjudicator-v1", "stage": "adjudication"}
  ]
}
```

Catalog `profiles` entries use that same `protocol`/`members` shape plus a
namespaced `id`. Select one with `profile: "local:my-profile-v1"` and list its
catalog in `catalogs`. Profiles reference personas from the bundled library and
all explicitly listed catalogs. Profiles do not recursively inherit other profiles.
A committee file cannot redirect to another committee file. Directory scanning,
remote URLs and executable plugins are not supported; a collection uses an
explicit catalog list.

Structural references: [config schema](config.schema.json),
[judge selector schema](judging.schema.json), and
[catalog schema](judge-catalog.schema.json). The executable loader also checks
cross-file references, stage counts, path safety and budgets. `llm-verifier check`
validates these using the same loader as intake. Preflight checks the schemas for
every resolved member before execution or model calls. Optional experiment inputs
require `json_object` on each reviewer that can request them; defense/adjudication
roles can retain `json_schema`. Protected manifest preprocessing and additional
rule files reach every role with the original bytes and transformation audit.

## Review protocol

`single-v1` requires exactly one `review` member. Its response is the existing
judgment-or-follow-up decision, augmented with the selected persona.

`review-defense-adjudication-v1` requires one to six `review` members, exactly one
`defense` member and exactly one `adjudication` member, with distinct IDs:

1. Reviewers run in listed order, independently: each receives the same complete
   evidence, its own persona and schema, and no peer reports. They return an
   assessment, findings with local IDs and evidence references, and optional
   initial-round experiment requests. They do not return votes or scores.
2. Requests are combined in stable member/request order and deduplicated by
   checker/experiment and exact typed JSON inputs. The first reason is retained
   in the execution plan; all original requests remain in member reports. A union
   exceeding the shared experiment budget fails rather than silently dropping
   requests. If a batch is requested, defense/adjudication wait until it completes.
   All reviewers then review the final evidence afresh. No second batch is allowed.
3. If there are blocking findings, the defender answers each exactly once, using
   IDs of the form `member/finding`. Without blocking findings, this stage is skipped.
4. The adjudicator sees the complete evidence, current-round reviews and any
   defense. It resolves every blocking finding exactly once as `confirmed`,
   `dismissed` or `unresolved`. Dismissal requires evidence references. Acceptance
   is disallowed unless every blocking finding is dismissed. The adjudicator must
   also assess every original claim; absence of objections does not establish truth.

Prior review text remains fallible evidence in the user message. The protected
rules, selected acceptance policy and external-evidence requirements apply to every stage. Personas
cannot relax them. Missing support is distinct from a demonstrated counterexample;
rejection need not assert mathematical falsity. These semantic assessments still
depend on model judgment; schema validation is not proof verification.

Malformed reports, missing stages, failed calls and exhausted inference budgets
are infrastructure failures. There is no majority vote, confidence threshold,
score averaging, silent member fallback, or automatic switch to single review.
The final judgment passes the existing exact-score projection and required-check
gates. One Yukon result and one original atomic accepted claim set are produced.

## Budgets and immutable provenance

With explicit `judging`, the default whole-evaluation inference limits are:

| Limit | Default | Hard maximum |
| --- | ---: | ---: |
| `total_provider_requests` | 24 | 64 |
| `total_review_seconds` | 1800 | 2400 |
| `total_review_tokens` | 262144 | 2097152 |

Explicit values in `limits` take precedence. Each review phase also has a hard
1200-second wall deadline across all members. Review jobs retain their existing
30-minute hosted timeout. A member cannot obtain a fresh whole-evaluation budget;
initial and final rounds share attempts, time and token allowance. Minimum capacity
for the initial mandatory roles is checked before inference; conditional defense,
follow-up reviews and retries may still exhaust a small configured budget.

`total_review_tokens` requires `judging`. It counts **reserved completion tokens**:
each member's `max_completion_tokens` multiplied by its actual generation attempts,
including retries. Fast or short responses do not refund that reservation.
This is not a dollar or input-token budget. Input size is bounded separately by
`max_prompt_bytes` per call and the shared request count. Actual reported usage is
also retained. Without `judging`, existing inference defaults remain unchanged.

At most eight catalogs and 32 total local files may be loaded. Local files are
bounded at 64 KiB each and 256 KiB in aggregate; persona instructions are limited
to 16,000 characters. Path traversal, symlinks, hardlinks and special files are
rejected by the existing protected-file reader.

Intake stores the referenced local source files and the fully resolved committee,
including instructions, persona hashes, effective model settings and protocol.
Later stages rederive the resolution from those snapshots. Editing local files
after intake does not alter the run. Bundled catalog and persona bytes are also
part of the installed verifier fingerprint.

Each review artifact keeps its existing `decision` for phase routing, adds
`reviews` with per-member reports/provenance, and records aggregate provenance.
Finalization replays the protocol without model calls: stage identity, order,
coverage, prompt/schema/model hashes, budgets, objection linkage and the derived
decision must match. All member reports remain in the dossier. Injected member
providers mark the entire phase as simulation and cannot publish an official score.
The hosted workflow already transports these same-run phase artifacts; no new
credential-bearing execution job is introduced.

Changing a committee affects newly evaluated work. It does not revoke historical
acceptance or change stable submission references by itself; ordinary rejudging
and [prior-result rules](prior-results.md) still apply. Upgrading verifier code or
bundled personas requires a fresh intake, as with any implementation change.

## Try the offline example

The [committee example](../examples/committee) selects a committee file, adds a
local scope reviewer and exercises a blocking objection that is dismissed after
defense. Its scripted outputs demonstrate mechanics, not mathematical reliability:

```sh
llm-verifier judges
llm-verifier check --root examples/committee
llm-verifier simulate --root examples/committee \
  --script examples/committee/simulation.json --out .work/committee.json
```

Committee simulation scripts use `initial_reviews` and `final_reviews`, maps from
member ID to that stage's report, alongside `initial_results`/`followup_results`.
Supply exactly the members called in each round; use `final_reviews: null` when
no follow-up occurs. `single-v1` uses the same map format with one decision per
round. Old configurations retain the original decision-based simulation format.

Model calibration is separate from selecting or publishing a profile. Labeled
valid/invalid and disagreement cases can later evaluate the chosen model settings;
the verifier does not require a calibration pack to load a profile. Passing the
protocol tests alone does not demonstrate improved judge accuracy.
