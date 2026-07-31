"""Discover the GridSquare, FoilHole, and Data hierarchy for a slot."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .acquisition_metadata import parse_acquisition_metadata
from .models import (
    DataImageRecord,
    FoilHoleRecord,
    GridFolder,
    GridSquareRecord,
    SlotContent,
)
from .naming import DEFAULT_NAMING_PROFILE, NamingProfile


def _image_sort_key(
    item: tuple[Path, datetime | None],
) -> tuple[datetime, str]:
    path, acquired_at = item
    return acquired_at or datetime.max, path.name.lower()


def _identifier_sort_key(value: str) -> tuple[int, int, str]:
    """Sort numeric identifiers naturally while accepting lab-specific text IDs."""

    try:
        return 0, int(value), ""
    except ValueError:
        return 1, 0, value.casefold()


def _discover_data_images(
    grid_square_path: Path,
    profile: NamingProfile,
) -> dict[str, list[DataImageRecord]]:
    grouped: dict[str, list[DataImageRecord]] = defaultdict(list)
    data_directory = grid_square_path / profile.data_directory_name
    if not data_directory.is_dir():
        return grouped

    for path in data_directory.iterdir():
        if not path.is_file():
            continue
        match = profile.data_image_pattern.fullmatch(path.name)
        if not match:
            continue
        foil_id = match.group("foil_id")
        area_id = match.group("area_id")
        xml_path = profile.matching_sidecar(path, profile.metadata_suffix)
        grouped[foil_id].append(
            DataImageRecord(
                path=path,
                xml_path=xml_path,
                foil_id=foil_id,
                acquisition_area_id=area_id,
                acquired_at=profile.parse_timestamp(match),
                metadata=parse_acquisition_metadata(xml_path),
                mrc_path=profile.matching_sidecar(path, profile.micrograph_suffix),
            )
        )

    for records in grouped.values():
        records.sort(
            key=lambda record: (
                record.acquired_at or datetime.max,
                record.path.name.lower(),
            )
        )
    return grouped


def _discover_foil_holes(
    grid_square_path: Path,
    profile: NamingProfile,
) -> tuple[FoilHoleRecord, ...]:
    versions: dict[str, list[tuple[Path, datetime | None]]] = defaultdict(list)
    foil_directory = grid_square_path / profile.foil_directory_name
    if foil_directory.is_dir():
        for path in foil_directory.iterdir():
            if not path.is_file():
                continue
            match = profile.foil_image_pattern.fullmatch(path.name)
            if not match:
                continue
            foil_id = match.group("foil_id")
            versions[foil_id].append(
                (path, profile.parse_timestamp(match))
            )

    data_images = _discover_data_images(grid_square_path, profile)
    foil_ids = sorted(set(versions) | set(data_images), key=_identifier_sort_key)
    records: list[FoilHoleRecord] = []
    for foil_id in foil_ids:
        candidates = sorted(versions.get(foil_id, []), key=_image_sort_key)
        latest_path: Path | None = candidates[-1][0] if candidates else None
        latest_time: datetime | None = candidates[-1][1] if candidates else None
        records.append(
            FoilHoleRecord(
                foil_id=foil_id,
                image_path=latest_path,
                xml_path=(
                    profile.matching_sidecar(latest_path, profile.metadata_suffix)
                    if latest_path
                    else None
                ),
                acquired_at=latest_time,
                data_images=tuple(data_images.get(foil_id, [])),
            )
        )
    return tuple(records)


def _discover_grid_square(
    path: Path,
    grid_square_id: str,
    profile: NamingProfile,
) -> GridSquareRecord:
    candidates: list[tuple[Path, datetime | None]] = []
    for image_path in path.iterdir():
        if not image_path.is_file():
            continue
        match = profile.grid_image_pattern.fullmatch(image_path.name)
        if not match:
            continue
        candidates.append(
            (image_path, profile.parse_timestamp(match))
        )
    candidates.sort(key=_image_sort_key)
    image_path = candidates[0][0] if candidates else None
    acquired_at = candidates[0][1] if candidates else None
    warning = None
    if image_path is None:
        warning = f"{path.name}: grid image not found; detail pages skipped."

    return GridSquareRecord(
        grid_square_id=grid_square_id,
        path=path,
        image_path=image_path,
        xml_path=(
            profile.matching_sidecar(image_path, profile.metadata_suffix)
            if image_path
            else None
        ),
        acquired_at=acquired_at,
        foil_holes=_discover_foil_holes(path, profile) if image_path else (),
        warning=warning,
    )


def _grid_square_sort_key(
    record: GridSquareRecord,
) -> tuple[int, datetime, int, str]:
    try:
        numeric_id = int(record.grid_square_id)
    except ValueError:
        numeric_id = 2**63 - 1
    return (
        0 if record.acquired_at is not None else 1,
        record.acquired_at or datetime.max,
        numeric_id,
        record.path.name.lower(),
    )


def discover_session_content(
    grid: GridFolder,
    profile: NamingProfile = DEFAULT_NAMING_PROFILE,
) -> SlotContent:
    """Return the complete report hierarchy for one slot folder."""

    records: list[GridSquareRecord] = []
    warnings: list[str] = []
    disc_directories = sorted(
        (
            path
            for path in grid.path.iterdir()
            if path.is_dir() and profile.disc_directory_pattern.fullmatch(path.name)
        ),
        key=lambda path: path.name.lower(),
    )

    if not disc_directories:
        warnings.append(
            f"Slot {grid.slot}: no image-disc directories matched the "
            f"{profile.name!r} naming profile."
        )

    for disc_directory in disc_directories:
        for candidate in sorted(
            disc_directory.iterdir(),
            key=lambda path: path.name.lower(),
        ):
            if not candidate.is_dir():
                continue
            match = profile.grid_directory_pattern.fullmatch(candidate.name)
            if not match:
                continue
            record = _discover_grid_square(
                candidate,
                match.group("grid_square_id"),
                profile,
            )
            records.append(record)
            if record.warning:
                warnings.append(record.warning)

    records.sort(key=_grid_square_sort_key)
    return SlotContent(
        grid=grid,
        grid_squares=tuple(records),
        warnings=tuple(warnings),
    )
