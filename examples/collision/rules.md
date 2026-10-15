# TOY eight-bit collision challenge

The target is the first eight bits of SHA-256(prefix || message), with exact prefix
and truncation fixed by checkers/target.json. Inputs are distinct UTF-8 strings,
not filenames, code, alternative target parameters or supplied digest values.
Only the organizer collision checker establishes this concrete pair's predicate.

For numeric scoring the single submission-declared canonical pair must pass.
The only objective is the checker's total_bytes for that exact passing pair.
The judge must bind its tuple to that result and reject conflicting score claims.
Different pairs tried in follow-up cannot replace the declared witness.
A pass does not establish search complexity, success probability, global
optimality, or a collision in full SHA-256. Review such claims separately.

A correct relevant lemma may be accepted with scores=null and no concrete pair.
A missing or failed required check forbids every non-null tuple, including an
invalid judgment. allow_unscored does not excuse an unsupported validity claim.

This is a small teaching example, not HashSmash support or cryptographic evidence.
