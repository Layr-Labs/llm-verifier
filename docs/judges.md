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

## Decomposed mathematical review

Select the offline whole-proof plus region-review profile:

```json
{"judging": {"profile": "builtin:math-decomposed-v1"}}
```

This uses `decomposed-review-v1`, with five member definitions: `planner`
(`planning`), `proof` (`review`), `region` (`region`), `defender` (`defense`), and
`adjudicator` (`adjudication`). One region member definition supplies the persona
and provider settings for a separate, stateless call for each planned region.
The initial whole-proof reviewer and region workers receive no peer reports.
They all retain access to the complete original evidence for context.

1. The planner sees numbered immutable source text and proposes source paths,
   inclusive one-based line ranges, unique region IDs and brief purposes. It
   produces no correctness verdict. The plan must cover every line of every
   submitted file exactly once, including `submission.json` and experiment code.
   Regions for each file must appear in increasing line order. Empty files have
   one empty line. Protected rules and prior-result evidence remain available to
   every reviewer but are not candidate source regions.
2. The verifier checks paths, ranges, gaps, overlaps and the region cap before
   mathematical review. It freezes the plan against the source hashes. The
   planner is asked to prefer individual proof obligations, estimates or cases,
   usually around four numbered equations; mathematical granularity remains a
   model judgment, not a structurally verified property.
3. The whole-proof reviewer and assigned region calls run concurrently after
   the plan is frozen, sharing the bounded scheduler described below. Every
   call receives the complete original evidence; region calls also receive
   numbered assigned lines. Results retain plan order regardless of completion
   order. A region report also contains `coverage`:
   `{region_id, start_line, end_line, complete}`. It must report the entire
   assignment as complete. Partial, missing or mismatched coverage fails the
   review as infrastructure failure; it never becomes an accepting verdict.
   `region_review_incomplete` (the diagnostic call record identifies the region) identifies a partially checked region.
4. Findings retain the existing blocking/observation semantics. Blocking IDs
   include the worker and region, for example `region/r2/gap`. All survive to
   the existing defense/adjudication protocol, including singleton findings;
   even similar findings remain separately accountable. There is no vote or
   automatic inference of validity from completion or agreement.
5. If reviewers request the existing one experiment batch, final review reuses
   the frozen plan, repeats the whole-proof and every region review with updated
   evidence, and then performs defense/adjudication. The planner is not rerun.

`max_regions` defaults to 8 in the bundled profile, with a hard maximum of 16.
Override it in the selector, or set it in a local profile/explicit committee:

```json
{
  "judging": {
    "profile": "builtin:math-decomposed-v1",
    "max_regions": 12,
    "overrides": {"region": {"provider": {"reasoning_effort": "high"}}}
  }
}
```

The cap is a maximum, not a target or permission to omit text. A submission with
more files than the cap fails preflight before model calls. `max_regions` is
invalid on other protocols. A custom decomposed profile requires exactly one
planning member, one region member, one defender, one adjudicator, and one to
four whole-evidence review members. `add_members` can add those review members;
all normal persona catalogs and provider overrides apply.

The minimum shared budget reserves one call for every fixed member plus
`max_regions` calls for the region member, including conditional defense.
The default profile therefore requires 12 requests and 98,304 reserved completion
tokens at default settings. Only actual calls consume that reservation accounting;
a shorter plan uses fewer calls. Retries and follow-up share the same time,
request and token limits and can still exhaust the budget. The fixed 1,200-second
phase deadline also applies. No skipped region or fallback verdict fills a gap.

Review artifacts add `region_plan` (regions and source hashes/line counts) and
`region_coverage`. Each region call records its `region_id` alongside the existing
member provenance. Finalization replays the plan, assignments, coverage, source
and prompt bindings without model calls. Coverage validates declared completion;
it cannot establish that a model actually checked every inference correctly.

The design adapts the offline global/decomposed review pattern from
[UCLA Moonshot Math Verifier](https://github.com/xinjiegit/UCLAMoonshot-math-verifier/tree/adcde6b29e7ca420a87edd397df767df4926e712).
It is not an implementation of that project's complete audit workflow. It adds
no external retrieval, agent tools, code execution, revision verdicts, or changes
to the selected acceptance policy, atomic claim set, prior-result rules and
score/check gates. It makes no calibrated accuracy claim.

See the [decomposed example](../examples/decomposed) for a fully offline simulation.
Its `initial_reviews.region` is a list of reports in plan order; fixed members
retain a single report each. `final_reviews.region` follows the same plan order
when a follow-up occurs. Missing or extra scripted region reports are rejected.

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
adds whole-evidence reviewers only; an explicit profile defines the other roles.

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

1. Reviewers run concurrently within the shared worker limit; saved reports
   retain listed order. Each receives the same complete evidence, its own
   persona and schema, and no peer reports. They return an
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

Independent model calls share one coordinator with at most **four active calls**
across the entire phase. A selected primary and escalated judge share this same
coordinator; their internal reviewers do not create separate four-call pools.
The decomposed planner precedes region assignment, while defense and adjudication
wait for the complete validated reports they consume. A coordinator waiting for
its child work does not occupy a model-call slot. The ordinary single-reviewer
path remains unchanged.

Concurrent call durations are retained individually, but the phase time budget
uses elapsed wall time, including coordination and validation. Overlapping calls
are not added together as if they ran sequentially. Request and completion-token
reservations remain shared across every active call and retry.

Explicit values in `limits` take precedence. Each review phase also has a hard
1200-second wall deadline across all members. Review jobs retain their existing
30-minute hosted timeout. A member cannot obtain a fresh whole-evaluation budget;
initial and final rounds share attempts, time and token allowance. Before inference,
the request and token budgets must cover one call to every member, including the
conditional defender, using each member's configured completion-token cap. The
bundled math and HashSmash profiles therefore require at least six requests and
49,152 reserved completion tokens with default member settings. Defense is still
skipped when there are no blocking findings, and unused calls are not charged.
Retries and follow-up reviews may still exhaust this minimum budget.

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

## Probabilistic escalation (opt-in)

`builtin:probabilistic-escalation-v1` always runs the primary judge and samples
submissions for an additional, independent escalated judge. The default primary
is `builtin:single-v1`; the default escalation is the offline
`builtin:math-decomposed-v1` profile described above (the Moonshot-inspired
profile). Selecting this wrapper does not switch any existing challenge or model.
Both children inherit the challenge provider unless their member overrides say
otherwise. For example:

```json
{
  "judging": {
    "profile": "builtin:probabilistic-escalation-v1",
    "probability": 0.1,
    "mode": "record-only",
    "seed": "challenge-audit-v1",
    "primary": {
      "profile": "builtin:single-v1",
      "overrides": {"reviewer": {"provider": {"model": "YOUR_PRIMARY_MODEL"}}}
    },
    "escalation": {
      "profile": "builtin:math-decomposed-v1",
      "max_regions": 8,
      "overrides": {"adjudicator": {"provider": {"model": "YOUR_ESCALATED_MODEL"}}}
    }
  }
}
```

Omit both child selectors to use their defaults. In the example only the
escalation adjudicator's model changes; set each child's member overrides to
change the other roles. Ordinary inline definitions and local persona/profile
catalogs are supported within each child. Their source files share the existing
32-file/256-KiB protected judge-source cap. A child cannot select another
escalation wrapper or a file pointer. The outer `judging` field may still point
to a protected JSON file containing the complete wrapper.

`probability` is a finite number in inclusive range 0..1, default **0.1**. Zero
never escalates and one always escalates. `mode` defaults to **record-only**:

- `record-only`: the primary decision is authoritative. A completed escalation,
  including a disagreement, remains comparison evidence and cannot override it.
- `override`: a selected escalation is authoritative, even when the primary
  disagrees. Unselected submissions still use the primary decision. A failed
  authoritative escalation produces an infrastructure failure and no judgment.

The primary and selected escalation run concurrently. Neither child's prompt
includes the other child's verdict or reports, including on final review. Both see the original protected
rules, atomic claims, source files, prior-result evidence and captured experiments.
A completed child must satisfy the same structured-claim acknowledgment and
score/check gates as an ordinary review. The comparison reports separate validity
and score disagreement, without turning disagreement into automatic rejection.
Agreement is not a calibrated probability of correctness.

### Sampling and ownership

The protected challenge owner chooses `seed` (nonempty text up to 256 characters;
default `organizer-default-v1`). It is public configuration, not a secret, and
requires no additional credentials. The audit records `selection.inputs`, whose
version is `sha256-submission-config-v1`, seed, and the intake's submission,
effective-configuration and installed-verifier content hashes. Compute SHA-256
of the library's canonical JSON encoding of those inputs, interpret all 256 bits
as an unsigned big-endian integer, and select when it is below
`floor(decimal(probability) * 2^256)`. `selection` saves the digest, probability,
65-digit hexadecimal threshold and boolean outcome.

The random evaluation ID, timestamps, transport attempts and review responses
are excluded. Identical frozen source/configuration/verifier bytes produce the
same selection across retry and replay, including a fresh intake of the same
inputs. Changing the seed, configuration, submission or verifier can change it.
Because the hash and seed are predictable, a solver can search for unselected
variants: **this is not resistance to solver grinding or adversarial sampling**.

### One budget, one follow-up batch

Both children and all attempts/rounds/resumes share `total_provider_requests`,
`total_review_tokens` and `total_review_seconds`. A phase also has one shared
1,200-second wall deadline. `total_review_tokens` is the existing conservative
completion-token reservation limit, not an input-token or monetary cap. Actual
reported prompt/completion usage is preserved per call. The coordinator reserves one request and the member's full
`max_completion_tokens` allowance before each native generation attempt,
including retries. First attempts are admitted before their wave is dispatched;
a grant remains charged even if the call later fails or never reaches the
provider. A full retry allowance is not reserved for every parallel member in
advance. Diagnostic usage is evidence, never invented success provenance.

For any probability above zero, preflight requires capacity for **both** initial
profiles, including conditional defense and the escalation's full configured
region cap. The defaults require 13 requests and 106,496 completion-token
reservations. This admission rule applies even to subsequently unselected
submissions; it does not reserve extra capacity for retries or final review.
At zero probability only the primary's minimum budget is required. Schemas and
source-file region caps for **both configured children** are checked at every
probability, including zero and subsequently unselected submissions. Invalid
escalation configuration is never hidden behind an unlucky sample.

The union of both children's requested experiments is deduplicated by exact typed
input and checked against the one existing follow-up budget. Audit-only requests
can therefore trigger that batch. Overflow fails instead of silently dropping
requests. On final review only children that requested follow-up run again;
completed terminal decisions and failed audit evidence are carried unchanged.
A carried decision retains its original evidence/provenance and consumes no new
inference budget. If the primary was terminal, audit follow-up cannot replace it.

### Failure evidence, replay and explicit resume

A failed audit is recorded with its partial reports, interrupted request bindings,
conservative budget reservation, redacted raw-response diagnostics and error.
Its status is `failed`; there is no substitute judgment. In record-only mode the
primary can still finalize. The main review/dossier includes this evidence and
`--failure-out` also saves a separate `review_failure` artifact. Hosted workflows
retain that diagnostic artifact even when the primary review succeeds. Existing
immutable artifact transfer and consumer-only recovery are unchanged.

In override mode an escalation failure writes no successful review. A primary
failure also prevents a judgment in either mode. The versioned
`parallel-children-v1` checkpoint retains both child outcomes, so a completed
audit is preserved even if the primary fails. Child execution records use
`independent-waves-v1`: stable task order, request grants, completion times and
failures are replayed without summing overlapping durations. Fixed-size
per-attempt admission grants and terminal outcome/record-digest receipts remain
available even when raw response capture is truncated. Replay checks these
receipts against charged attempts and retained reports; changing a failure count
or hiding a completed peer is rejected.

When a child finishes, its evidence is immediately saved to `--failure-out`
with checkpoint status `in-progress`, even while its peer is still running.
These provisional snapshots are **diagnostic-only and cannot be resumed**:
unknown later work must not be refunded or duplicated. They remain available
through post-join composition, sealing and replay validation. Library calls clear
them only on successful return; the CLI waits until its successful atomic output
write. A composition, serialization or output failure retains completed child
evidence without inventing a combined judgment or permitting an automatic retry.
After all admitted work has finished or failed, a resumable failure snapshot has
status `settled` and contains the complete charged round history. Independent peers already admitted in a failed wave are
drained and retained; a failed call does not discard their completed reports.

To retry only failed child graphs, explicitly pass the settled, trusted organizer
artifact to the **same phase**, for example:

```sh
llm-verifier review-initial --intake intake.json --initial initial.json \
  --resume previous.failure.json --out retried-review.json \
  --failure-out retried.failure.json
```

Resume requires identical intake bindings (including evaluation ID, frozen
configuration, original source and installed verifier), exact ordered phase-parent
hashes, selection, child prompt/schema/provider provenance and replayable failed
attempts. For final review the original initial review and follow-up experiment
artifacts must also match. Inputs and output/diagnostic paths must be distinct.
Completed children are reused exactly: primary inference is not repeated after
an audit failure, and a completed audit is not repeated after a primary failure.
The whole prior round's wall time and every granted request/token reservation
remain charged once. Failed children restart their protocol, including planning
in the initial phase; partial reports remain evidence rather than judgments.
Exhausted budgets refuse new inference. A timeout consumes its remaining time
reservation; actual wall time, including deadline overrun, remains in failure
evidence. Record-only can still finalize a completed primary after an audit
failure.

Legacy serial review artifacts retain their replay path. Ordinary hosted reruns
do not automatically resume failed model reviews; a consumer-only artifact retry
still performs no model calls. There is no distributed lease or automatic recovery
of an in-progress snapshot.

Successful artifacts include both child envelopes, the versioned selection,
authority and reason, comparison, and aggregate reservation provenance. Dossier,
score finalization and accepted-result export replay these bindings without model
calls. Offline simulation uses `initial_reviews.primary` and
`initial_reviews.escalation` (and similarly `final_reviews`) as member-to-report
maps. Omit an unselected or carried child's reports; extra reports fail. Region
members still use ordered arrays. Calls sharing the same injected provider object
are serialized in logical order to preserve stateful simulation behavior; distinct
objects can overlap. Native calls use separate provider instances. Simulations
remain unpublishable. See the
[escalation example](../examples/escalation) for a runnable always-selected audit.
