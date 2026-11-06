"""Find conventional export folders in the desktop project's registered roots."""

from pathlib import Path

from storage import _directory, _directory_allowed, _under


_EXPORT_FOLDERS = ("output", "outputs", "exports", "deliverables")
_VOLUMES = Path("/Volumes")


def _permitted_project(path, home):
    if not path.is_absolute() or ".." in path.parts:
        return False
    if _under(path, home):
        relative = path.parts[len(home.parts):]
        return (bool(relative) and relative[0].casefold() != "library"
                and not any(part.startswith(".") for part in relative))
    if _under(path, _VOLUMES):
        relative = path.parts[len(_VOLUMES.parts):]
        return len(relative) >= 2 and not any(part.startswith(".") for part in relative)
    return False


def linked_roots(state, home, excluded):
    """Yield project IDs, labels, and existing, non-overlapping export folders."""
    projects = state.get("local-projects")
    if not isinstance(projects, dict):
        return
    reserved = list(excluded)
    for project_id, project in projects.items():
        if not isinstance(project_id, str) or not project_id or not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list):
            continue
        name = project.get("name")
        name = name.strip()[:200] if isinstance(name, str) else ""
        for root in roots:
            if not isinstance(root, str) or not root or "\x00" in root:
                continue
            path = Path(root)
            if not _permitted_project(path, home):
                continue
            for folder in _EXPORT_FOLDERS:
                candidate = path / folder
                if not _directory_allowed(candidate, ()) or any(
                    _under(candidate, other) or _under(other, candidate) for other in reserved
                ):
                    continue
                try:
                    with _directory(candidate.anchor, candidate.parts[1:]):
                        pass
                except (OSError, UnicodeError):
                    continue
                reserved.append(candidate)
                yield project_id, name or path.name, candidate
