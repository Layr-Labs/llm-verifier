"""Linux/POSIX descriptor-relative reads: no symlink parents or blocking FIFOs."""

from contextlib import contextmanager
import io
import os
from pathlib import Path
import re
import stat
import tempfile
import tokenize

from .errors import VerifierError
from .jsonutil import canonical, integer, keys, loads, sha, MAX_ARTIFACT_BYTES

_PART = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def relative(path: str, *, dot=False) -> str:
    if dot and path == ".":
        return path
    if type(path) is not str or len(path) > 512:
        raise VerifierError("unsafe_path")
    parts = path.split("/")
    if len(parts) > 12 or any(not _PART.fullmatch(p) or p in {".", "..", ".git"} for p in parts):
        raise VerifierError("unsafe_path")
    return path


@contextmanager
def directory_fd(path):
    # Do not resolve(): it silently follows precisely the symlinks we reject.
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    except OSError:
        raise VerifierError("unsafe_or_missing_directory") from None
    finally:
        os.close(fd)


def _read(fd, name, limit):
    # O_NONBLOCK prevents a substituted FIFO from blocking before fstat.
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise VerifierError("regular_unlinked_file_required")
        if before.st_size > limit:
            raise VerifierError("file_byte_limit")
        chunks, size = [], 0
        while True:
            part = os.read(handle, min(65536, limit + 1 - size))
            if not part:
                break
            size += len(part)
            if size > limit:
                raise VerifierError("file_byte_limit")
            chunks.append(part)
        after = os.fstat(handle)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise VerifierError("file_changed_during_intake")
        return b"".join(chunks)
    finally:
        os.close(handle)


def read_bytes(path, limit=MAX_ARTIFACT_BYTES):
    target = Path(path)
    try:
        with directory_fd(target.parent) as fd:
            return _read(fd, target.name, limit)
    except OSError:
        raise VerifierError("unsafe_or_missing_file") from None


def read_json(path):
    return loads(read_bytes(path))


def utf8(data: bytes, *, python=False):
    try:
        value = data.decode("utf-8")
        if "\x00" in value or value.startswith("\ufeff"):
            raise VerifierError("utf8_without_bom_or_nul_required")
        if python:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            if encoding != "utf-8":
                raise VerifierError("python_utf8_required")
        return value
    except (UnicodeError, SyntaxError, LookupError):
        raise VerifierError("utf8_required") from None


def snapshot(root, limits):
    files, total, entries = {}, 0, 0
    def walk(fd, prefix=""):
        nonlocal total, entries
        names = []
        with os.scandir(fd) as iterator:
            for item in iterator:
                entries += 1
                if entries > limits["max_files"] * 4:
                    raise VerifierError("directory_entry_limit")
                names.append(item.name)
        for name in sorted(names):
            path = relative(prefix + name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(files) >= limits["max_files"]:
                    raise VerifierError("file_count_limit")
                data = _read(fd, name, min(limits["max_file_bytes"], limits["max_submission_bytes"] - total))
                total += len(data)
                files[path] = {"text": utf8(data, python=path.endswith(".py")),
                               "sha256": sha(data), "size": len(data)}
            else:
                raise VerifierError("regular_file_required")
    try:
        with directory_fd(root) as fd:
            walk(fd)
    except OSError:
        raise VerifierError("unsafe_submission_tree") from None
    return files


def validate_files(files, limits, *, empty=False):
    if type(files) is not dict or not (0 if empty else 1) <= len(files) <= limits["max_files"]:
        raise VerifierError("file_count_limit")
    total = 0
    for path, record in files.items():
        relative(path)
        keys(record, {"text", "sha256", "size"})
        if type(record["text"]) is not str:
            raise VerifierError("source_text_required")
        data = record["text"].encode("utf-8")
        integer(record["size"], 0, limits["max_file_bytes"])
        if sha(data) != record["sha256"] or len(data) != record["size"]:
            raise VerifierError("source_hash_mismatch")
        utf8(data, python=path.endswith(".py"))
        total += len(data)
    if total > limits["max_submission_bytes"]:
        raise VerifierError("submission_byte_limit")


def snapshot_declared(root, paths, candidate, limits):
    """Read only explicitly protected files; never walk the challenge root."""
    files, total = {}, 0
    if len(paths) > limits["max_files"]:
        raise VerifierError("file_count_limit")
    for path in sorted(paths):
        relative(path)
        outside(Path(root) / path, candidate)
        data = read_bytes(Path(root) / path, min(limits["max_file_bytes"], limits["max_submission_bytes"] - total))
        total += len(data)
        files[path] = {"text": utf8(data, python=path.endswith(".py")), "sha256": sha(data), "size": len(data)}
    validate_files(files, limits, empty=True)
    return files


def outside(path, candidate):
    # Lexical absolute check plus caller uses directory_fd to reject aliases.
    if Path(os.path.abspath(path)).is_relative_to(Path(os.path.abspath(candidate))):
        raise VerifierError("protected_path_inside_submission")


def clear_output(path):
    target = Path(path)
    if not target.parent.exists():
        return
    with directory_fd(target.parent) as fd:
        try:
            os.unlink(target.name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError:
            raise VerifierError("cannot_clear_output") from None


def ensure_directory(path):
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        raise VerifierError("unsafe_output_directory") from None
    finally:
        os.close(fd)


def write_json(path, value):
    write_bytes(path, canonical(value) + b"\n")


def write_bytes(path, payload):
    target = Path(path)
    ensure_directory(target.parent)
    with directory_fd(target.parent):
        # Output directories are organizer-owned; atomic replace also avoids
        # following an existing destination symlink. Never use candidate dirs.
        fd, temporary = tempfile.mkstemp(prefix=".llmv-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def distinct_paths(inputs, outputs):
    """Preserve input files before stale-output cleanup, including aliases."""
    sources = [Path(os.path.abspath(p)) for p in inputs if p is not None]
    destinations = [Path(os.path.abspath(p)) for p in outputs if p is not None]
    for index, destination in enumerate(destinations):
        for other in sources + destinations[:index]:
            if destination == other:
                raise VerifierError("distinct_input_output_paths_required")
            try:
                if os.path.samefile(destination, other):
                    raise VerifierError("distinct_input_output_paths_required")
            except (FileNotFoundError, NotADirectoryError):
                pass
