"""Runtime resource path helpers.

This module keeps file lookups working both in a normal source checkout and
inside a PyInstaller bundle.
"""
from __future__ import annotations

import sys
import shutil
import os
from pathlib import Path
from typing import Union


PathLike = Union[str, Path]


def app_base_dir() -> Path:
    """Return the preferred base directory for bundled read-only resources."""
    if getattr(sys, "frozen", False):
        mei_path = getattr(sys, "_MEIPASS", None)
        if mei_path:
            return Path(mei_path)
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def project_root() -> Path:
    """Return the source checkout root when running from Python files."""
    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    """Return a writable per-user app data directory."""
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "mc-enhance-helper"


def runtime_base_dir() -> Path:
    """Return the directory beside the running executable, or cwd in source runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def runtime_logs_dir() -> Path:
    """Return the writable logs directory next to the running program."""
    target = runtime_base_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _copy_if_missing(source: Path, target: Path):
    if target.exists() or not source.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _legacy_db_candidates(db_name: str) -> list[Path]:
    candidates = []

    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / db_name)

    candidates.extend(
        [
            Path.cwd() / db_name,
            project_root() / db_name,
        ]
    )

    seen = set()
    result = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    return result


def resource_path(relative_path: PathLike) -> str:
    """Resolve a resource path for source runs and PyInstaller bundles.

    Absolute paths are returned unchanged. Relative paths are searched in the
    PyInstaller extraction/base directory first, then in the current working
    directory, then in the source project root.
    """
    path = Path(relative_path)
    if path.is_absolute():
        return str(path)

    candidates = []
    base = app_base_dir()
    candidates.append(base / path)

    if getattr(sys, "frozen", False):
        exe_candidate = Path(sys.executable).resolve().parent / path
        if exe_candidate not in candidates:
            candidates.append(exe_candidate)

    cwd_candidate = Path.cwd() / path
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)

    root_candidate = project_root() / path
    if root_candidate not in candidates:
        candidates.append(root_candidate)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return str(candidates[0])


def writable_data_path(relative_path: PathLike) -> str:
    """Return a writable path for data/*.json files.

    Bundled/source files are treated as read-only defaults and copied to the
    per-user app data directory on first use. Existing user files are never
    overwritten by an app update.
    """
    path = Path(relative_path)
    if path.is_absolute():
        return str(path)

    parts = list(path.parts)
    if parts and parts[0].lower() == "data":
        target_rel = Path(*parts[1:]) if len(parts) > 1 else Path()
    else:
        target_rel = path

    target = app_data_dir() / "data" / target_rel
    if not target.exists():
        source = Path(resource_path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and source.is_file():
            shutil.copy2(source, target)
        else:
            target.touch()
    return str(target)


def writable_db_path(db_name: str = "mc_enhance.db") -> str:
    """Return the per-user writable database path.

    When upgrading from older builds, copy a same-name database from the legacy
    working directory or executable directory if the new per-user database does
    not exist yet. The legacy file is left in place.
    """
    target = app_data_dir() / db_name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        for candidate in _legacy_db_candidates(db_name):
            if not candidate.exists() or not candidate.is_file():
                continue
            _copy_if_missing(candidate, target)
            for suffix in ("-wal", "-shm"):
                _copy_if_missing(Path(str(candidate) + suffix), Path(str(target) + suffix))
            break
    return str(target)


def writable_db_url(db_name: str = "mc_enhance.db") -> str:
    """Return a SQLAlchemy SQLite URL for the per-user writable database."""
    return "sqlite:///" + Path(writable_db_path(db_name)).resolve().as_posix()
