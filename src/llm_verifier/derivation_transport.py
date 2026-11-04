"""Exact historical GitHub artifacts selected by protected organizer plans.

Separate from the same-run transfer helper. Downloads are inert ZIPs only;
source and wheels inside them are never executed.
"""

import io
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .errors import InfrastructureError, VerifierError
from .files import read_bytes, relative, write_bytes
from .jsonutil import loads, sha

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_MEMBERS = 512


def archive_path(root, ref):
    from .derivation import artifact_ref
    artifact_ref(ref)
    return Path(root) / (ref["artifact_id"] + ".zip")


def read_artifact(root, ref):
    return read_archive(read_bytes(archive_path(root, ref), MAX_ARCHIVE), ref)


def read_archive(raw, ref):
    from .derivation import artifact_ref
    artifact_ref(ref)
    if len(raw) > MAX_ARCHIVE or sha(raw) != ref["archive_sha256"]:
        raise VerifierError("derivation_archive_digest")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            members = bundle.infolist()
            if not 1 <= len(members) <= MAX_MEMBERS:
                raise VerifierError("derivation_archive_members")
            seen, total, selected = set(), 0, None
            for member in members:
                name = member.filename.removesuffix("/") if member.is_dir() else member.filename
                relative(name)
                if member.filename != member.orig_filename or name in seen:
                    raise VerifierError("derivation_archive_duplicate")
                seen.add(name)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if kind not in (0, stat.S_IFDIR if member.is_dir() else stat.S_IFREG):
                    raise VerifierError("derivation_archive_special_file")
                if member.flag_bits & 1 or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise VerifierError("derivation_archive_encoding")
                total += member.file_size
                if member.file_size > MAX_FILE or total > MAX_EXPANDED:
                    raise VerifierError("derivation_archive_size")
                if member.filename == ref["path"] and not member.is_dir():
                    selected = member
            if selected is None:
                raise VerifierError("derivation_archive_missing_file")
            with bundle.open(selected) as stream:
                value = stream.read(MAX_FILE + 1)
            if len(value) != selected.file_size or sha(value) != ref["file_sha256"]:
                raise VerifierError("derivation_source_file_digest")
            return value
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError):
        raise VerifierError("derivation_archive_invalid") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubArtifacts:
    """Credentials go only to api.github.com, never to storage redirects."""

    def __init__(self, token=None, *, opener=None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        if type(self.token) is not str or not self.token or any(not 33 <= ord(c) <= 126 for c in self.token):
            raise InfrastructureError("derivation_artifact_credential_missing")
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.deadline = time.monotonic() + 180

    def request(self, url, limit, *, api=False, redirect=False):
        headers = {"User-Agent": "llm-verifier-score-derivation"}
        if api:
            if not url.startswith("https://api.github.com/repos/"):
                raise VerifierError("derivation_artifact_api_origin")
            headers.update(Authorization="Bearer " + self.token, Accept="application/vnd.github+json",
                           **{"X-GitHub-Api-Version": "2022-11-28"})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise InfrastructureError("derivation_artifact_deadline")
        try:
            try:
                response = self.opener.open(urllib.request.Request(url, headers=headers), timeout=min(30, remaining))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                if redirect and response.status in (301, 302, 303, 307, 308):
                    return None, response.headers.get("Location")
                if response.status != 200:
                    raise InfrastructureError("derivation_artifact_http_status")
                value = bytearray()
                while True:
                    if time.monotonic() >= self.deadline:
                        raise InfrastructureError("derivation_artifact_deadline")
                    part = response.read(min(65536, limit + 1 - len(value)))
                    if not part:
                        break
                    value.extend(part)
                    if len(value) > limit:
                        raise VerifierError("derivation_artifact_response_limit")
                return bytes(value), None
        except (urllib.error.URLError, OSError):
            raise InfrastructureError("derivation_artifact_transport") from None

    def metadata(self, path):
        raw, _ = self.request("https://api.github.com/repos/" + path, 1024 * 1024, api=True)
        value = loads(raw, 1024 * 1024)
        if type(value) is not dict:
            raise VerifierError("derivation_artifact_metadata")
        return value

    def fetch(self, ref, validity, root):
        from .derivation import artifact_ref
        artifact_ref(ref)
        base = ref["repository"] + "/actions/"
        run = self.metadata(base + "runs/" + ref["run_id"] + "/attempts/" + ref["run_attempt"])
        if (str(run.get("id")) != ref["run_id"] or str(run.get("run_attempt")) != ref["run_attempt"]
                or run.get("head_sha") != validity["commit_sha"]
                or run.get("status") != "completed" or run.get("conclusion") != "success"):
            raise VerifierError("derivation_artifact_validity_run")
        info = self.metadata(base + "artifacts/" + ref["artifact_id"])
        workflow = info.get("workflow_run", {})
        if (type(workflow) is not dict or str(info.get("id")) != ref["artifact_id"]
                or info.get("name") != ref["artifact_name"] or info.get("expired") is not False
                or str(workflow.get("id")) != ref["run_id"]
                or workflow.get("head_sha") != validity["commit_sha"]
                or type(info.get("size_in_bytes")) is not int or not 0 < info["size_in_bytes"] <= MAX_ARCHIVE
                or info.get("digest", "sha256:" + ref["archive_sha256"]) != "sha256:" + ref["archive_sha256"]):
            raise VerifierError("derivation_artifact_metadata")
        value, location = self.request(
            "https://api.github.com/repos/" + base + "artifacts/" + ref["artifact_id"] + "/zip",
            MAX_ARCHIVE, api=True, redirect=True,
        )
        for _ in range(4):
            if location is None:
                break
            value, location = self.request(storage_url(location), MAX_ARCHIVE, redirect=True)
        if location is not None or value is None:
            raise InfrastructureError("derivation_artifact_redirect_limit")
        read_archive(value, ref)
        write_bytes(archive_path(root, ref), value)


def storage_url(value):
    if (type(value) is not str or len(value) > 8192 or "\\" in value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise VerifierError("derivation_artifact_storage_url")
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or not parsed.hostname or not parsed.hostname.endswith(
                    (".blob.core.windows.net", ".actions.githubusercontent.com")
                )):
            raise ValueError()
    except ValueError:
        raise VerifierError("derivation_artifact_storage_url") from None
    return value


def fetch_entry(entry, root, *, client=None):
    from .derivation import validate_entry
    validate_entry(entry)
    if entry["artifacts"] is None:
        return
    client = client or GitHubArtifacts()
    # Retained validity may come from a different job than the latest score.
    for ref in entry["artifacts"].values():
        client.fetch(ref, entry["origin"]["validity"], root)
