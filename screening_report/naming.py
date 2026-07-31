"""Configurable directory and filename conventions for screening sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Match, Pattern


class NamingProfileError(ValueError):
    """Raised when a naming profile or filename does not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class NamingProfile:
    """Translate one lab's filesystem conventions into normalized identifiers.

    Patterns use named groups such as ``project``, ``session``, ``slot``,
    ``grid_square_id``, ``foil_id``, and ``area_id``. Image patterns may also
    provide either ``timestamp`` or the pair ``date`` and ``time``.
    """

    name: str
    project_pattern: Pattern[str]
    slot_directory_pattern: Pattern[str]
    atlas_directory_template: str
    atlas_image_pattern: Pattern[str]
    disc_directory_pattern: Pattern[str]
    grid_directory_pattern: Pattern[str]
    grid_image_pattern: Pattern[str]
    foil_directory_name: str
    foil_image_pattern: Pattern[str]
    data_directory_name: str
    data_image_pattern: Pattern[str]
    timestamp_format: str = "%Y%m%d%H%M%S"
    metadata_suffix: str = ".xml"
    micrograph_suffix: str = ".mrc"
    atlas_metadata_name: str = "Atlas.dm"
    grid_metadata_template: str = "GridSquare_{grid_square_id}.dm"
    session_metadata_name: str = "EpuSession.dm"
    metadata_directory_name: str = "Metadata"
    minimum_slot: int = 1
    maximum_slot: int = 12

    def __post_init__(self) -> None:
        required_groups = {
            "project_pattern": (self.project_pattern, {"project"}),
            "slot_directory_pattern": (
                self.slot_directory_pattern,
                {"session", "slot"},
            ),
            "grid_directory_pattern": (
                self.grid_directory_pattern,
                {"grid_square_id"},
            ),
            "foil_image_pattern": (self.foil_image_pattern, {"foil_id"}),
            "data_image_pattern": (
                self.data_image_pattern,
                {"foil_id", "area_id"},
            ),
        }
        for field_name, (pattern, required) in required_groups.items():
            missing = required.difference(pattern.groupindex)
            if missing:
                names = ", ".join(sorted(missing))
                raise NamingProfileError(
                    f"{field_name} is missing required named group(s): {names}."
                )
        if self.minimum_slot < 1 or self.maximum_slot < self.minimum_slot:
            raise NamingProfileError("The configured slot range is invalid.")

    def extract_project(self, directory_name: str) -> str | None:
        match = self.project_pattern.match(directory_name)
        return match.group("project") if match else None

    def match_slot_directory(
        self,
        directory_name: str,
        session_name: str,
    ) -> int | None:
        match = self.slot_directory_pattern.fullmatch(directory_name)
        if not match or match.group("session").casefold() != session_name.casefold():
            return None
        slot = int(match.group("slot"))
        if not self.minimum_slot <= slot <= self.maximum_slot:
            return None
        return slot

    def atlas_directory(self, atlas_root: Path, slot: int) -> Path:
        relative = self.atlas_directory_template.format(slot=slot)
        return atlas_root / Path(relative)

    def parse_timestamp(self, match: Match[str]) -> datetime | None:
        groups = match.groupdict()
        value = groups.get("timestamp")
        if value is None:
            date_text = groups.get("date")
            time_text = groups.get("time")
            if date_text is None or time_text is None:
                return None
            value = f"{date_text}{time_text}"
        try:
            return datetime.strptime(value, self.timestamp_format)
        except ValueError:
            return None

    def matching_sidecar(self, image_path: Path, suffix: str) -> Path | None:
        """Find a same-stem sidecar with a case-insensitive extension."""

        candidate = image_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
        stem = image_path.stem.casefold()
        wanted_suffix = suffix.casefold()
        try:
            return next(
                (
                    path
                    for path in image_path.parent.iterdir()
                    if path.is_file()
                    and path.stem.casefold() == stem
                    and path.suffix.casefold() == wanted_suffix
                ),
                None,
            )
        except OSError:
            return None


DEFAULT_NAMING_PROFILE = NamingProfile(
    name="Thermo Fisher EPU",
    project_pattern=re.compile(r"^(?P<project>160\d{3})(?!\d)"),
    slot_directory_pattern=re.compile(
        r"^(?P<session>.+)_Slot(?P<slot>\d{1,2})$",
        re.IGNORECASE,
    ),
    atlas_directory_template="Sample{slot}/Atlas",
    atlas_image_pattern=re.compile(r"^Atlas_.*\.jpe?g$", re.IGNORECASE),
    disc_directory_pattern=re.compile(r"^Images-Disc.*$", re.IGNORECASE),
    grid_directory_pattern=re.compile(
        r"^GridSquare_(?P<grid_square_id>\d+)$",
        re.IGNORECASE,
    ),
    grid_image_pattern=re.compile(
        r"^GridSquare_(?P<date>\d{8})_(?P<time>\d{6}).*\.jpe?g$",
        re.IGNORECASE,
    ),
    foil_directory_name="FoilHoles",
    foil_image_pattern=re.compile(
        r"^FoilHole_(?P<foil_id>\d+)_(?P<date>\d{8})_"
        r"(?P<time>\d{6}).*\.jpe?g$",
        re.IGNORECASE,
    ),
    data_directory_name="Data",
    data_image_pattern=re.compile(
        r"^FoilHole_(?P<foil_id>\d+)_Data_(?P<area_id>\d+).*_"
        r"(?P<date>\d{8})_(?P<time>\d{6})\.jpe?g$",
        re.IGNORECASE,
    ),
)

