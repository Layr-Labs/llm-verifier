# Independent sets

Prove a universal lower bound on the independence number of a finite simple graph.

## Problem and model

Let G be any finite simple undirected graph (no loops or parallel edges) with n >= 1 vertices and m edges. An independent set contains no adjacent pair; alpha(G) is its largest possible size. A submission must prove, for one exact rational constant c, that alpha(G) >= c*n^2/(2*m+n) for every such G.

## Score

The score is c, represented as a reduced exact fraction with positive denominator; higher is better. Eligible scores satisfy 0 <= c <= 1. The lower endpoint is trivial because alpha(G) >= 0. The starting submission claims c = 1/2. The Caro-Wei bound alpha(G) >= sum_v 1/(d(v)+1), combined with Cauchy-Schwarz, gives c = 1. Disjoint unions of equal cliques attain equality: k cliques of size r have n=kr, 2m+n=kr^2, and alpha(G)=k. Thus c > 1 is impossible, and 1 is the largest possible score. Compare fractions exactly; finite binary64 score projections can tie. A person confirms official results.

## What does not count

Do not assume all vertices have equal degree: the theorem must cover irregular graphs. A proof only for sufficiently large n is insufficient: every n >= 1 is required. Numerical tests, examples, conjectures, and unproved partial lemmas do not establish the universal claim. Unscored partial results are not accepted.

## Submission and review

Edit submission/submission.json and submission/proof.md. Declare exactly one structured claim stating the universal theorem and the chosen c, its exact score, and evidence ["proof.md"]. Keep the complete proof in that file, not duplicated in the manifest. Use experiments: []. The LLM judge reviews the entire claim and proof under the shared atomic-claims and external-evidence rules; all material assumptions and steps must be justified. Standard results may be invoked with their precise hypotheses checked. Structural validation is not mathematical acceptance. A person confirms official results.
