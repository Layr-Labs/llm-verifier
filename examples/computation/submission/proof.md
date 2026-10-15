Base case n=0 gives 0=0. If S(n)=n(n+1)(2n+1)/6, then
S(n+1)=S(n)+(n+1)^2=(n+1)(n(2n+1)+6(n+1))/6
=(n+1)(n+2)(2n+3)/6. Induction proves the identity for all integers n>=0.
The Python experiment independently accumulates successive squares and compares
the closed expression at each prefix, including zero.
