"""Comparison keys only; original manifests, snapshots and provenance retain bytes."""

import re

# Match the native resolver's UUID dialect, not arbitrary UUID-shaped opaque IDs.
# Compact/braced/URN forms and unsupported versions/variants are not aliases.
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
                   r"|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff")


def submission_key(value):
    return value.lower() if _UUID.fullmatch(value) else value


def evidence_key(value):
    return "prior:" + submission_key(value[6:]) if value.startswith("prior:") else value
