"""Organizer-owned TOY collision checker, not a full hash challenge verifier."""
import hashlib
import json
from pathlib import Path
import sys

# Fixed protected path, never a path/source/target supplied by the requester.
target = json.loads(Path("/snapshot/files/checkers/target.json").read_text())
assert target["bits"] == 8
inputs = json.load(sys.stdin)
left, right = inputs["left"].encode("utf-8"), inputs["right"].encode("utf-8")
prefix = target["prefix"].encode("utf-8")
a = hashlib.sha256(prefix + left).hexdigest()[:2]
b = hashlib.sha256(prefix + right).hexdigest()[:2]
print(json.dumps({"passed": left != right and a == b, "target_bits": 8,
                  "digest_hex": a, "total_bytes": len(left) + len(right)}))
