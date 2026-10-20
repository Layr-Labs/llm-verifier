# Baseline proof for c = 1/2

Choose a uniformly random ordering of the vertices. Include v in I exactly when v precedes all its neighbors. Adjacent vertices cannot both belong to I, so I is independent. If d(v) is the degree of v, symmetry among v and its neighbors gives Pr(v in I)=1/(d(v)+1). Hence some ordering has |I| >= sum_v 1/(d(v)+1).

Cauchy-Schwarz, applied to the positive numbers d(v)+1, gives
n^2 <= (sum_v (d(v)+1))*(sum_v 1/(d(v)+1)).
The degree sum is 2m, so alpha(G) >= n^2/(2m+n). Since 2m+n > 0, this implies alpha(G) >= (1/2)*n^2/(2m+n), as claimed, for every n >= 1.
