"""Scan selected directories and move verified macOS files to the Trash."""

import hashlib
import os
import secrets
import stat
import subprocess
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple


MINIMUM_SIZE = 1024 * 1024
TRASH_COMMAND = Path("/usr/bin/trash")
_FILE_ID_KEY = secrets.token_bytes(32)
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CODEX = Path.home() / ".codex"
_PROJECTS = _CODEX / ".chatgpt-projects"
_CODEX_ARTIFACTS = (_PROJECTS, _CODEX / "generated_images", _CODEX / "visualizations")
_IGNORED_DIRECTORIES = frozenset({
    ".cache", ".codex", ".git", ".hg", ".mypy_cache", ".next", ".nuxt",
    ".pytest_cache", ".ruff_cache", ".svn", ".tox", ".trash", ".trashes",
    ".turbo", ".venv", "__pycache__", "node_modules", "venv",
})
_PACKAGE_SUFFIXES = frozenset({
    "app", "band", "bundle", "fcpbundle", "framework", "imovielibrary", "key",
    "logicx", "numbers", "pages", "photoslibrary", "pkg", "playground", "rtfd",
    "sparsebundle", "xcworkspace", "xcodeproj",
})
_GIT_LOCK = "Git 已跟踪的文件"
_GIT_UNKNOWN = "无法确认 Git 跟踪状态"


@dataclass(frozen=True)
class Root:
    id: str
    label: str
    path: Path
    trashable: bool = True
    include: Optional[Callable[[Path], bool]] = None
    group: Optional[Callable[[Path], Optional[Tuple[str, str]]]] = None
    descend: Optional[Callable[[Path], bool]] = None


@dataclass(frozen=True)
class FileEntry:
    root: Path
    relative: Tuple[str, ...]
    root_identity: Tuple[int, int]
    parent_identity: Tuple[int, int]
    signature: Tuple[int, int, int, int, int]
    excluded: Tuple[Path, ...]
    locked_reason: Optional[str]


def _absolute(path):
    return Path(os.path.abspath(os.path.expanduser(path)))


def _normalized(name):
    return unicodedata.normalize("NFD", name).casefold()


def _path_key(path):
    return tuple(map(_normalized, path.parts))


def _under(path, parent):
    parent_parts = _path_key(parent)
    return _path_key(path)[:len(parent_parts)] == parent_parts


def _directory_allowed(path, excluded):
    if any(_under(path, protected) for protected in excluded):
        return False
    parts = _path_key(path)
    if _under(path, _CODEX):
        if not any(_under(path, allowed) for allowed in _CODEX_ARTIFACTS):
            return False
        parts = parts[len(_CODEX.parts):]
    if any(part in _IGNORED_DIRECTORIES for part in parts):
        return False
    if any(part.rpartition(".")[2] in _PACKAGE_SUFFIXES for part in parts if "." in part):
        return False
    if _under(path, _PROJECTS):
        return not any(part.startswith(".") for part in path.parts[len(_PROJECTS.parts):])
    return True


def _file_allowed(path, excluded):
    return not any(_under(path, protected) for protected in excluded)


@contextmanager
def _directory(start, parts):
    """Walk directory descriptors without following links, including ancestors."""
    descriptor = os.dup(start) if isinstance(start, int) else os.open(start, _OPEN_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, _OPEN_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _identity(info):
    return info.st_dev, info.st_ino


def _signature(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _has_git_metadata(path, dir_fd=None):
    try:
        os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _repositories(directory):
    return tuple(ancestor for ancestor in (directory, *directory.parents)
                 if _has_git_metadata(ancestor / ".git"))


def _tracked_paths(repository):
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    try:
        metadata = (repository / ".git").lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            return None
        result = subprocess.run(
            ["/usr/bin/git", "-c", "core.fsmonitor=false", "-C", repository, "ls-files", "--cached", "-z"],
            env=environment, capture_output=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {_normalized(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}


def _git_lock(path, repositories, indexes):
    for repository in repositories:
        if repository not in indexes:
            indexes[repository] = _tracked_paths(repository)
        tracked = indexes[repository]
        if tracked is None:
            return _GIT_UNKNOWN
        relative = _normalized(path.relative_to(repository).as_posix())
        # Entries for submodules also protect everything underneath their paths.
        parts = relative.split("/")
        if any("/".join(parts[:length]) in tracked for length in range(1, len(parts) + 1)):
            return _GIT_LOCK
    return None


def scan(roots: Iterable[Root], *, excluded_paths=(), minimum_size=MINIMUM_SIZE):
    """Return public file rows, aggregate root stats, and a private deletion snapshot."""
    excluded = tuple(_absolute(path) for path in excluded_paths)
    rows, stats, snapshot, indexes = [], [], {}, {}
    for root in roots:
        path = _absolute(root.path)
        aggregate = dict(id=root.id, label=root.label, path=str(path), size=0, allocated=0,
                         count=0, trashable=root.trashable, skipped=0, projects=[])
        stats.append(aggregate)
        if not _directory_allowed(path, excluded):
            aggregate["error"] = "这个目录受保护或已排除"
            continue
        try:
            with _directory(path.anchor, path.parts[1:]) as descriptor:
                root_identity = _identity(os.fstat(descriptor))
                _scan_root(root, path, descriptor, root_identity, excluded, minimum_size,
                           rows, aggregate, snapshot, indexes)
        except FileNotFoundError:
            aggregate["error"] = "目录不存在"
        except OSError as error:
            aggregate["error"] = f"无法读取目录：{error.strerror or error}"
    return rows, stats, snapshot


def _scan_root(root, root_path, root_fd, root_identity, excluded, minimum_size,
               rows, aggregate, snapshot, indexes):
    pending = [((), _repositories(root_path.parent))]
    projects = {}
    while pending:
        parts, repositories = pending.pop()
        directory_path = root_path.joinpath(*parts)
        try:
            with _directory(root_fd, parts) as directory_fd, os.scandir(directory_fd) as children:
                parent_identity = _identity(os.fstat(directory_fd))
                if parent_identity[0] != root_identity[0]:
                    continue
                if _has_git_metadata(".git", dir_fd=directory_fd):
                    repositories = (directory_path, *repositories)
                for child in children:
                    if child.name.startswith("."):
                        continue
                    child_path = directory_path / child.name
                    try:
                        if child.is_dir(follow_symlinks=False):
                            if (_directory_allowed(child_path, excluded)
                                    and (root.descend is None or root.descend(Path(*parts, child.name)))):
                                pending.append(((*parts, child.name), repositories))
                            continue
                        info = child.stat(follow_symlinks=False)
                    except OSError:
                        aggregate["skipped"] += 1
                        continue
                    if not stat.S_ISREG(info.st_mode) or not _file_allowed(child_path, excluded):
                        continue
                    relative = (*parts, child.name)
                    relative_path = Path(*relative)
                    if root.include and not root.include(relative_path):
                        continue
                    allocated = info.st_blocks * 512
                    aggregate["size"] += info.st_size
                    aggregate["allocated"] += allocated
                    aggregate["count"] += 1
                    group = root.group(relative_path) if root.group else None
                    if group is not None:
                        key, label = group
                        if key not in projects:
                            projects[key] = dict(key=key, label=label, path=str(root_path / key),
                                                 size=0, allocated=0, count=0)
                        project = projects[key]
                        project["size"] += info.st_size
                        project["allocated"] += allocated
                        project["count"] += 1
                    if max(info.st_size, allocated) < minimum_size:
                        continue
                    signature = _signature(info)
                    identity = repr((root.id, root_identity, parent_identity, signature)).encode()
                    file_id = hashlib.blake2b(os.fsencode(child_path) + b"\0" + identity,
                                              key=_FILE_ID_KEY, digest_size=18).hexdigest()
                    locked_reason = None if root.trashable else "此目录仅供查看"
                    locked_reason = locked_reason or _git_lock(child_path, repositories, indexes)
                    row = dict(id=file_id, name=child.name, path=str(child_path),
                               relativePath="/".join(relative), rootId=root.id,
                               size=info.st_size, allocated=allocated, mtime=info.st_mtime,
                               trashable=locked_reason is None)
                    if group is not None:
                        row["projectKey"] = group[0]
                    if locked_reason:
                        row["lockedReason"] = locked_reason
                    rows.append(row)
                    snapshot[file_id] = FileEntry(root_path, relative, root_identity,
                                                  parent_identity, signature, excluded,
                                                  locked_reason)
        except OSError as error:
            aggregate["skipped"] += 1
            if not parts:
                aggregate["error"] = f"无法读取目录：{error.strerror or error}"
    aggregate["projects"] = sorted(projects.values(), key=lambda project: (-project["allocated"], project["key"]))


def trash_files(ids, snapshot):
    """Move selected unchanged files to the native Trash, reporting each outcome."""
    results = []
    for file_id in dict.fromkeys(ids):
        entry = snapshot.get(file_id)
        try:
            if entry is None:
                raise ValueError("文件记录已失效，请重新扫描")
            _trash_entry(entry)
        except (OSError, ValueError) as error:
            results.append(dict(id=file_id, ok=False, error=str(error)))
        except subprocess.TimeoutExpired:
            results.append(dict(id=file_id, ok=False, error="移到废纸篓超时，请刷新后确认文件位置"))
        else:
            results.append(dict(id=file_id, ok=True))
    return results


def _trash_entry(entry):
    if entry.locked_reason:
        raise ValueError(entry.locked_reason)
    if not TRASH_COMMAND.is_file():
        raise ValueError("系统未提供废纸篓命令，此操作需要 macOS 15 或更新版本")
    path = entry.root.joinpath(*entry.relative)
    if not (_directory_allowed(path.parent, entry.excluded) and _file_allowed(path, entry.excluded)):
        raise ValueError("这个文件位于受保护或已排除的目录")
    _verify_entry(entry, path)
    locked_reason = _git_lock(path, _repositories(path.parent), {})
    if locked_reason:
        raise ValueError(locked_reason)
    # Git may take time. Reopen the path without following links just before dispatch.
    _verify_entry(entry, path)
    result = subprocess.run([TRASH_COMMAND, "--stopOnError", path], capture_output=True, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        if "NSCocoaErrorDomain Code=513" in detail:
            raise ValueError("macOS 拒绝访问废纸篓。请从 Finder 或终端启动工具，并检查文件夹权限。")
        raise ValueError(detail or "系统未能将文件移到废纸篓")


@contextmanager
def _entry_parent(entry):
    try:
        with _directory(entry.root.anchor, entry.root.parts[1:]) as root_fd:
            if _identity(os.fstat(root_fd)) != entry.root_identity:
                raise ValueError("扫描目录已变化，请重新扫描")
            with _directory(root_fd, entry.relative[:-1]) as parent_fd:
                if _identity(os.fstat(parent_fd)) != entry.parent_identity:
                    raise ValueError("文件所在目录已变化，请重新扫描")
                yield parent_fd
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ValueError("文件或所在目录已变化，请重新扫描") from error


def _verify_info(entry, info):
    if not stat.S_ISREG(info.st_mode) or _signature(info) != entry.signature:
        raise ValueError("文件在扫描后已变化，请重新扫描")


def _verify_entry(entry, path):
    with _entry_parent(entry) as parent_fd:
        _verify_info(entry, os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False))


def open_scanned_file(entry):
    """Open a scan-authorized file without following links or reopening by path."""
    with _entry_parent(entry) as parent_fd:
        descriptor = os.open(entry.relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
        try:
            _verify_info(entry, os.fstat(descriptor))
            return os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise
