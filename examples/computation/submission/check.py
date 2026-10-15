import json
import sys

n = json.load(sys.stdin)["n"]
total = 0
for k in range(n + 1):
    total += k * k
    if total != k * (k + 1) * (2 * k + 1) // 6:
        raise RuntimeError("identity mismatch")
print(json.dumps({"checked_n": n, "all_equal": True}))
