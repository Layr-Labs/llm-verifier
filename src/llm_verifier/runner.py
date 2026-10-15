"""Credential-free Docker execution of immutable source snapshots.

The command policy follows HashSmash's isolated runner pattern. Mathematical
interpretation is deliberately left to review; stdout is candidate evidence.
"""

import base64
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid

from .config import IMAGE
from .errors import InfrastructureError, VerifierError
from .jsonutil import canonical, loads


def docker_command(binary, image, directory, name, limits):
    if not IMAGE.fullmatch(image):
        raise InfrastructureError("pinned_runtime_required")
    return [binary, "run", "--rm", "--pull=never", "--platform=linux/amd64", "--name", name,
            "--network=none", "--user=65534:65534", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--log-driver=none", "--ipc=none",
            f"--memory={limits['memory_mb']}m", f"--memory-swap={limits['memory_mb']}m",
            f"--cpus={limits['cpus']}", f"--pids-limit={limits['pids']}",
            "--ulimit=nofile=64:64", "--ulimit=core=0:0", "--ulimit=fsize=16777216:16777216",
            f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={limits['tmpfs_mb']}m,mode=1777",
            "--mount", f"type=bind,src={directory},dst=/snapshot,readonly",
            "--workdir=/tmp", "--env=PYTHONHASHSEED=0", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONUNBUFFERED=1", "--env=LANG=C.UTF-8", "--env=LC_ALL=C.UTF-8",
            "--entrypoint=python", "-i", image, "-I", "-B", "/snapshot/entrypoint.py"]


def capture(process, timeout, stdout_limit, stderr_limit):
    """Drain both streams concurrently; continue timing after early pipe closure."""
    deadline = time.monotonic() + timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": stdout_limit, "stderr": stderr_limit}
    status = "completed"
    try:
        with selectors.DefaultSelector() as selector:
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    status = "timeout"
                    break
                events = selector.select(timeout=min(0.05, max(0, deadline - time.monotonic())))
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = caps[key.data] - len(buffers[key.data])
                    buffers[key.data].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        status = "output_limit"
                        break
                if status != "completed":
                    break
                if not selector.get_map() and process.poll() is None:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if status != "completed":
            process.kill()
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()) if status == "completed" else 2)
        except subprocess.TimeoutExpired:
            status = "timeout"
            process.kill()
            process.wait(timeout=2)
        return status, process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def output_record(status, returncode, stdout, stderr):
    """Raw bytes remain exact; malformed UTF-8/JSON is explicit failed evidence."""
    streams = {}
    for label, data in (("stdout", stdout), ("stderr", stderr)):
        try:
            decoded = data.decode("utf-8")
        except UnicodeError:
            decoded = None
        streams[label] = {"base64": base64.b64encode(data).decode("ascii"), "utf8": decoded,
                          "bytes": len(data), "complete": status not in {"timeout", "output_limit"}}
    result = None
    if status == "completed":
        if returncode != 0:
            status = "runtime_error"
        elif any(stream["utf8"] is None for stream in streams.values()):
            status = "protocol_error"
        else:
            try:
                result = loads(stdout)
                if type(result) is not dict:
                    raise VerifierError("output_object_required")
            except VerifierError:
                status = "protocol_error"
                result = None
    return {"status": status, "exit_code": returncode, **streams, "result": result}


class DockerRunner:
    simulation = False

    def __init__(self, runtime, limits):
        self.runtime, self.limits = runtime, limits

    def run(self, files, source, inputs, timeout):
        binary = shutil.which("docker")
        if binary is None:
            raise InfrastructureError("docker_unavailable")
        name = "llmv-" + uuid.uuid4().hex
        # Never inherit credentials, HOME, proxies, arbitrary Docker context or
        # remote TCP daemons. The socket is used by the host CLI, never mounted.
        env = {"PATH": os.path.dirname(binary) + ":/usr/bin:/bin", "DOCKER_HOST": "unix:///var/run/docker.sock"}
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="llmv-") as temporary:
                root = Path(temporary)
                mount = root / "snapshot"
                mount.mkdir(mode=0o755)
                # A fixed filename prevents candidate text entering argv. The
                # original snapshot is also available as /snapshot/files/...
                for path, record in files.items():
                    dest = mount / "files" / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(record["text"].encode("utf-8"))
                    dest.chmod(0o444)
                (mount / "entrypoint.py").write_bytes(files[source]["text"].encode("utf-8"))
                (mount / "entrypoint.py").chmod(0o444)
                config = root / "docker-config"
                config.mkdir()
                env["DOCKER_CONFIG"] = str(config)
                try:
                    available = subprocess.run([binary, "image", "inspect", self.runtime["image"]],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, timeout=min(10, timeout), check=False)
                except subprocess.TimeoutExpired:
                    raise InfrastructureError("docker_preflight_timeout") from None
                if available.returncode:
                    raise InfrastructureError("docker_image_or_daemon_unavailable")
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise InfrastructureError("docker_preflight_budget")
                request = root / "stdin.json"
                request.write_bytes(canonical(inputs) + b"\n")
                with request.open("rb") as stdin:
                    process = subprocess.Popen(docker_command(binary, self.runtime["image"], mount, name, self.limits),
                        stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                    status, code, stdout, stderr = capture(process, remaining, self.limits["stdout_bytes"], self.limits["stderr_bytes"])
                # Docker reserves these statuses. Conservatively classify an
                # entrypoint deliberately exiting 125..127 as infrastructure.
                if status == "completed" and (code < 0 or code in (125, 126, 127)):
                    raise InfrastructureError("docker_start_failed")
                return output_record(status, code, stdout, stderr)
        except (OSError, subprocess.SubprocessError):
            raise InfrastructureError("docker_execution_failed") from None
        finally:
            # Only this generated name. Cleanup is bounded independently by 3s.
            try:
                removed = subprocess.run([binary, "rm", "-f", name], env=env, timeout=2,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if removed.returncode != 0:
                    # --rm may already have removed it. Distinguish proven
                    # absence from a failed cleanup with a surviving container.
                    remaining = subprocess.run([binary, "container", "ls", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"],
                        env=env, timeout=1, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, check=False)
                    if remaining.returncode != 0 or remaining.stdout != b"":
                        raise InfrastructureError("docker_cleanup_unverified")
            except (OSError, subprocess.SubprocessError):
                raise InfrastructureError("docker_cleanup_failed") from None
