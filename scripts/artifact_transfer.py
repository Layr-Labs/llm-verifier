#!/usr/bin/env python3
"""Same-run Actions artifact transfer; standalone, stdlib-only, no wheel required.

Protocol matches actions/download-artifact at 018cc2cf5baa6db3ef3c5f8a56943fffe632ef53.
Only the generated local Node action receives the runner's runtime credential.
"""
import base64
import binascii
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MAX_HELPER_BYTES = 65536
MAX_BASE64_BYTES = 87384
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_METADATA_BYTES = 1024 * 1024
ATTEMPTS = 4
DEADLINE_SECONDS = 180
REQUEST_SECONDS = 15
CHUNK = 64 * 1024
SERVICE_HOST = "results-receiver.actions.githubusercontent.com"
SERVICE_PATH = "/twirp/github.actions.results.api.v1.ArtifactService/"

ACTION_YAML = """name: Verified same-run artifact transfer
description: Download fixed artifact IDs with bounded retries and verify before extraction
runs:
  using: node20
  main: index.js
"""
ACTION_JS = """'use strict';
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const names = ['PATH', 'RUNNER_TEMP', 'GITHUB_WORKSPACE', 'GITHUB_RUN_ID',
  'GITHUB_RUN_ATTEMPT', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RESULTS_URL', 'LLMV_ARTIFACTS'];
const env = Object.fromEntries(names.filter(k => process.env[k] !== undefined)
  .map(k => [k, process.env[k]]));
const result = spawnSync('python3', ['-I', path.join(__dirname, 'transfer.py'), 'download'],
  {env, cwd: env.RUNNER_TEMP, stdio: 'inherit', timeout: 190000, killSignal: 'SIGKILL'});
if (result.error || result.signal) console.error('artifact transfer: worker stopped');
process.exit(result.status === 0 ? 0 : 1);
"""


class TransferError(Exception):
    """Messages are fixed codes: never embed response bodies, tokens, or URLs."""


class TransientError(TransferError):
    pass


def fail(code):
    raise TransferError(code)


def positive(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def digest(value):
    if not isinstance(value, str):
        fail("invalid_digest")
    value = value.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("invalid_digest")
    return value


def url_text(value):
    if (not isinstance(value, str) or len(value) > 8192
            or any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in value)):
        fail("unsafe_url")
    return value


def https_url(value, *, service=False):
    value = url_text(value)
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443) and not parsed.fragment)
    except ValueError:
        fail("unsafe_url")
    if not valid:
        fail("unsafe_url")
    host = parsed.hostname
    if service:
        if host != SERVICE_HOST or parsed.path not in ("", "/") or parsed.query:
            fail("unsafe_service_url")
    elif not (host.endswith(".blob.core.windows.net")
              or host.endswith(".actions.githubusercontent.com")):
        fail("unsafe_storage_url")
    return value


def backend_ids(token):
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        scopes = [s.split(":") for s in claims["scp"].split()
                  if s.startswith("Actions.Results:")]
        if len(scopes) != 1 or len(scopes[0]) != 3:
            fail("invalid_runtime_scope")
        _, run, job = scopes[0]
        if not all(re.fullmatch(r"[0-9a-fA-F-]{36}", v) for v in (run, job)):
            fail("invalid_runtime_scope")
        return run, job
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
        fail("invalid_runtime_scope")


def references(text, run_id, attempt):
    try:
        entries = json.loads(text)
    except (TypeError, ValueError):
        fail("invalid_manifest")
    if not positive(run_id) or not positive(attempt):
        fail("invalid_run")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
        fail("invalid_manifest")
    ids, paths = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"artifact", "path"}:
            fail("invalid_manifest")
        ref, path = entry["artifact"], entry["path"]
        if not isinstance(ref, dict) or set(ref) != {"id", "name", "sha256", "run_id", "run_attempt"}:
            fail("invalid_reference")
        if not positive(ref["id"]) or ref["id"] in ids:
            fail("invalid_artifact_id")
        if not isinstance(ref["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref["name"]):
            fail("invalid_artifact_name")
        if ref["run_id"] != run_id or not positive(ref["run_attempt"]) or int(ref["run_attempt"]) > int(attempt):
            fail("wrong_producer_run")
        digest(ref["sha256"])
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path) or path in paths:
            fail("invalid_destination")
        ids.add(ref["id"])
        paths.add(path)
    return entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transfer:
    def __init__(self, token, service_url, run_id, attempt):
        if not positive(run_id) or not positive(attempt):
            fail("invalid_run")
        self.run_id, self.attempt = run_id, int(attempt)
        self.run, self.job = backend_ids(token)
        self.token = token
        self.service = https_url(service_url, service=True).rstrip("/")
        self.opener = urllib.request.build_opener(NoRedirect())
        self.deadline = time.monotonic() + DEADLINE_SECONDS

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail("transfer_deadline")
        return remaining

    def retry(self, stage, operation):
        for attempt in range(1, ATTEMPTS + 1):
            self.remaining()
            try:
                return operation()
            except TransientError:
                if attempt == ATTEMPTS:
                    fail(stage + "_retry_exhausted")
                delay = 2 ** (attempt - 1) + random.uniform(0, 0.25)
                if delay >= self.remaining():
                    fail("transfer_deadline")
                print(f"artifact transfer: {stage} retry {attempt}/{ATTEMPTS}", file=sys.stderr)
                time.sleep(delay)

    def open(self, request):
        try:
            return self.opener.open(request, timeout=min(REQUEST_SECONDS, self.remaining()))
        except urllib.error.HTTPError as error:
            # No API redirects; storage redirects are explicitly followed without auth.
            if error.code in (301, 302, 303, 307, 308):
                return error
            code = error.code
            error.close()
            if code in (408, 429, 500, 502, 503, 504):
                raise TransientError("transient_http") from None
            fail("http_" + str(code))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            self.network_error(error)

    @staticmethod
    def network_error(error):
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if (isinstance(reason, (TimeoutError, ConnectionError, http.client.IncompleteRead,
                                http.client.RemoteDisconnected))
                or getattr(reason, "errno", None) in {
                    errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED, errno.ETIMEDOUT,
                    errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH, socket.EAI_AGAIN}):
            raise TransientError("transient_network") from None
        fail("network_error")

    def read(self, response):
        self.remaining()
        try:
            return response.read(CHUNK)
        except (OSError, http.client.HTTPException) as error:
            self.network_error(error)

    def rpc(self, method, payload):
        def request():
            req = urllib.request.Request(self.service + SERVICE_PATH + method,
                data=json.dumps(payload).encode(), headers={
                    "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                    "Accept": "application/json", "User-Agent": "llmv-artifact-transfer/1"})
            with self.open(req) as response:
                if response.status != 200:
                    fail("unexpected_api_status")
                data = bytearray()
                while chunk := self.read(response):
                    data.extend(chunk)
                    if len(data) > MAX_METADATA_BYTES:
                        fail("metadata_too_large")
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeError):
                    fail("invalid_api_json")
                if not isinstance(result, dict):
                    fail("invalid_api_json")
                return result
        return self.retry(method, request)

    @staticmethod
    def field(data, snake, camel):
        if snake in data and camel in data:
            fail("ambiguous_api_field")
        return data.get(snake, data.get(camel))

    def metadata(self, ref):
        if (ref.get("run_id") != self.run_id or not positive(ref.get("run_attempt"))
                or int(ref["run_attempt"]) > self.attempt):
            fail("wrong_producer_run")
        result = self.rpc("ListArtifacts", {"workflow_run_backend_id": self.run,
            "workflow_job_run_backend_id": self.job, "id_filter": ref["id"]})
        rows = result.get("artifacts")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            fail("artifact_identity_not_unique")
        raw = rows[0]
        row = {**raw, **{camel: self.field(raw, snake, camel) for snake, camel in (
            ("database_id", "databaseId"), ("workflow_run_backend_id", "workflowRunBackendId"),
            ("workflow_job_run_backend_id", "workflowJobRunBackendId"))}}
        # Retained artifacts can belong to an older attempt's backend run. The
        # protected descriptor fixes their global ID/name/hash/numeric run; List
        # still uses current runtime scope. Sign only the returned producer, as
        # the pinned official client does. Same-attempt backend drift is invalid.
        if (row.get("databaseId") != ref["id"] or row.get("name") != ref["name"]
                or any(not isinstance(row.get(key), str)
                       or not re.fullmatch(r"[0-9a-fA-F-]{36}", row[key])
                       for key in ("workflowRunBackendId", "workflowJobRunBackendId"))
                or (int(ref["run_attempt"]) == self.attempt
                    and row["workflowRunBackendId"] != self.run)):
            fail("artifact_identity_mismatch")
        size = row.get("size")
        if not positive(size) or int(size) > MAX_ARCHIVE_BYTES:
            fail("invalid_archive_size")
        if row.get("digest") is not None and digest(row["digest"]) != digest(ref["sha256"]):
            fail("artifact_digest_mismatch")
        return {key: row[key] for key in ("databaseId", "name", "workflowRunBackendId", "workflowJobRunBackendId", "size")}

    def archive(self, row, ref, target):
        def download():
            # Refresh an expired/failed signed URL on each retry, for this same producer only.
            result = self.rpc("GetSignedArtifactURL", {"workflow_run_backend_id": row["workflowRunBackendId"],
                "workflow_job_run_backend_id": row["workflowJobRunBackendId"], "name": row["name"]})
            url = https_url(self.field(result, "signed_url", "signedUrl"))
            for redirect in range(4):
                req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                    "User-Agent": "llmv-artifact-transfer/1"})
                with self.open(req) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect == 3 or not response.headers.get("Location"):
                            fail("storage_redirect_limit")
                        url = https_url(urllib.parse.urljoin(url, url_text(response.headers["Location"])))
                        continue
                    if response.status != 200:
                        fail("unexpected_storage_status")
                    sha, size = hashlib.sha256(), 0
                    # Every retry truncates partial bytes and starts hashing from zero.
                    with target.open("wb") as archive:
                        while chunk := self.read(response):
                            size += len(chunk)
                            if size > MAX_ARCHIVE_BYTES or size > int(row["size"]):
                                fail("archive_too_large")
                            sha.update(chunk)
                            archive.write(chunk)
                    if size != int(row["size"]):
                        raise TransientError("truncated_archive")
                    if sha.hexdigest() != digest(ref["sha256"]):
                        fail("archive_digest_mismatch")
                    return
        self.retry("download", download)

    def fetch(self, ref, destination):
        if destination.exists() or destination.is_symlink():
            fail("destination_exists")
        with tempfile.TemporaryDirectory(prefix="llmv-transfer-", dir=destination.parent) as temporary:
            root = Path(temporary)
            row = self.metadata(ref)
            self.archive(row, ref, root / "artifact.zip")
            # GetSignedArtifactURL is name/job addressed; recheck the immutable ID before use.
            if self.metadata(ref) != row:
                fail("artifact_identity_changed")
            staged = root / "contents"
            files = extract(root / "artifact.zip", staged, self.remaining)
            self.remaining()
            if destination.exists() or destination.is_symlink():
                fail("destination_exists")
            staged.rename(destination)
        print("artifact transfer: verified " + ref["id"])
        return files


def extract(archive, target, check=lambda: None):
    """Validate every member before writing; stage regular files only, never extractall."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or len(members) > MAX_MEMBERS:
                fail("invalid_archive_members")
            seen, files, total = set(), set(), 0
            for member in members:
                name = member.filename
                path = name[:-1] if member.is_dir() else name
                parts = path.split("/")
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (name != member.orig_filename or not path or "\\" in path or ":" in path
                        or any(ord(c) < 32 or ord(c) == 127 for c in path)
                        or any(p in ("", ".", "..") for p in parts)
                        or path in seen or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or (kind == stat.S_IFDIR and not member.is_dir())
                        or (kind == stat.S_IFREG and member.is_dir())
                        or member.flag_bits & 1):
                    fail("unsafe_archive_member")
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    fail("unsupported_archive_compression")
                seen.add(path)
                if not member.is_dir():
                    files.add(path)
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    fail("expanded_archive_too_large")
            for path in seen:
                if any("/".join(path.split("/")[:i]) in files for i in range(1, len(path.split("/")))):
                    fail("archive_path_collision")
            target.mkdir()
            hashes = {}
            for member in members:
                check()
                path = target / member.filename
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, path.open("xb") as out:
                    size, sha = 0, hashlib.sha256()
                    while chunk := source.read(CHUNK):
                        check()
                        size += len(chunk)
                        if size > member.file_size:
                            fail("expanded_member_too_large")
                        out.write(chunk)
                        sha.update(chunk)
                    if size != member.file_size:
                        fail("truncated_member")
                    hashes[member.filename] = sha.hexdigest()
            return hashes
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        fail("invalid_zip")


def emit_bootstrap(source, output):
    data = source.read_bytes()
    if not data or len(data) > MAX_HELPER_BYTES:
        fail("helper_too_large")
    encoded = base64.b64encode(data).decode("ascii")
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("helper_base64=" + encoded + "\nhelper_sha256=" + hashlib.sha256(data).hexdigest() + "\n")


def main():
    try:
        command = sys.argv[1] if len(sys.argv) == 2 else ""
        if command == "emit-bootstrap":
            emit_bootstrap(Path(__file__), Path(os.environ["GITHUB_OUTPUT"]))
        elif command == "install-action":
            for name, content in (("action.yml", ACTION_YAML), ("index.js", ACTION_JS)):
                with Path(__file__).with_name(name).open("x", encoding="utf-8") as handle:
                    handle.write(content)
        elif command == "download":
            entries = references(os.environ["LLMV_ARTIFACTS"], os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
            root = Path(os.environ["RUNNER_TEMP"])
            if not root.is_absolute() or not root.is_dir() or root.is_symlink():
                fail("invalid_runner_temp")
            # A real wall deadline covers DNS, headers, slow reads, retries and extraction.
            def deadline(signum, frame):
                fail("transfer_deadline")
            signal.signal(signal.SIGALRM, deadline)
            signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
            try:
                transfer = Transfer(os.environ["ACTIONS_RUNTIME_TOKEN"], os.environ["ACTIONS_RESULTS_URL"],
                                    os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
                receipt = root / "llmv-transfer-receipt.json"
                if receipt.exists() or receipt.is_symlink():
                    fail("receipt_exists")
                verified = []
                for entry in entries:
                    files = transfer.fetch(entry["artifact"], root / entry["path"])
                    verified.append({**entry, "files": files})
                with receipt.open("x", encoding="utf-8") as handle:
                    json.dump({"version": 1, "run_id": os.environ["GITHUB_RUN_ID"],
                        "delivery_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "artifacts": verified}, handle)
                    handle.write("\n")
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        else:
            fail("invalid_command")
    except TransferError as error:
        print("artifact transfer: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        # Never emit an exception repr/traceback: it may contain a signed URL or token.
        print("artifact transfer: invalid_input_or_io", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
