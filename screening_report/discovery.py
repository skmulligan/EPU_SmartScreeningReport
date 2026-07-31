"""Discover grid folders and their slot-specific atlas images."""

from __future__ import annotations

from pathlib import Path

from .models import GridFolder
from .naming import DEFAULT_NAMING_PROFILE, NamingProfile


class DiscoveryError(ValueError):
    """Raised when a selected atlas directory cannot describe a session."""


def extract_project_number(
    directory_name: str,
    profile: NamingProfile = DEFAULT_NAMING_PROFILE,
) -> str:
    """Return the project identifier parsed by ``profile``."""

    project_number = profile.extract_project(directory_name)
    if project_number is None:
        raise DiscoveryError(
            f"The atlas directory name does not match the {profile.name!r} "
            "project naming rule."
        )
    return project_number


def _find_atlas_image(
    atlas_root: Path,
    slot: int,
    profile: NamingProfile,
) -> Path | None:
    """Find the preferred atlas image for ``slot`` using ``profile``."""

    atlas_directory = profile.atlas_directory(atlas_root, slot)
    if not atlas_directory.is_dir():
        return None

    candidates = sorted(
        (
            path
            for path in atlas_directory.iterdir()
            if path.is_file() and profile.atlas_image_pattern.fullmatch(path.name)
        ),
        key=lambda path: path.name.lower(),
    )
    return candidates[-1] if candidates else None


def discover_grid_folders(
    atlas_directory: str | Path,
    profile: NamingProfile = DEFAULT_NAMING_PROFILE,
) -> list[GridFolder]:
    """Find sibling ``_SlotN`` folders matching the atlas directory name.

    Only slots 1 through 12 are accepted. Results are returned in ascending
    numeric slot order, so a missing slot 1 requires no special handling.
    """

    atlas_root = Path(atlas_directory).expanduser()
    if not atlas_root.is_dir():
        raise DiscoveryError("The selected atlas directory does not exist.")

    project_number = extract_project_number(atlas_root.name, profile)
    discovered: list[GridFolder] = []

    try:
        siblings = atlas_root.parent.iterdir()
    except OSError as exc:
        raise DiscoveryError(
            f"Could not read the atlas directory's parent folder: {exc}"
        ) from exc

    for candidate in siblings:
        if not candidate.is_dir():
            continue
        slot = profile.match_slot_directory(candidate.name, atlas_root.name)
        if slot is None:
            continue

        discovered.append(
            GridFolder(
                slot=slot,
                path=candidate,
                project_number=project_number,
                atlas_image=_find_atlas_image(atlas_root, slot, profile),
            )
        )

    discovered.sort(key=lambda grid: grid.slot)
    return discovered
