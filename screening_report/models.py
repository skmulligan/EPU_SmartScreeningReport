"""Data models shared by discovery, reporting, and the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AcquisitionMetadata:
    """Microscope and imaging metadata parsed from one EPU Data XML file."""

    voltage_kv: float | None = None
    detector: str | None = None
    energy_filter_inserted: bool | None = None
    energy_filter_slit_width_ev: float | None = None
    instrument_model: str | None = None
    epu_software_version: str | None = None
    magnification: float | None = None
    pixel_size_x_angstrom: float | None = None
    pixel_size_y_angstrom: float | None = None
    total_dose_e_per_angstrom2: float | None = None
    recorded_defocus_um: float | None = None
    acquired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GridFolder:
    """One EPU grid output directory associated with an autoloader slot."""

    slot: int
    path: Path
    project_number: str
    atlas_image: Path | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def atlas_status(self) -> str:
        if self.atlas_image is None:
            return "Atlas image not found"
        return self.atlas_image.name


@dataclass(frozen=True, slots=True)
class DataImageRecord:
    """One high-magnification Data image associated with a FoilHole."""

    path: Path
    xml_path: Path | None
    foil_id: str
    acquisition_area_id: str
    acquired_at: datetime | None
    metadata: AcquisitionMetadata | None = None
    mrc_path: Path | None = None


@dataclass(frozen=True, slots=True)
class FoilHoleRecord:
    """The preferred FoilHole image and all of its matching Data images."""

    foil_id: str
    image_path: Path | None
    xml_path: Path | None
    acquired_at: datetime | None
    data_images: tuple[DataImageRecord, ...]


@dataclass(frozen=True, slots=True)
class GridSquareRecord:
    """One GridSquare directory and its nested screening imagery."""

    grid_square_id: str
    path: Path
    image_path: Path | None
    xml_path: Path | None
    acquired_at: datetime | None
    foil_holes: tuple[FoilHoleRecord, ...]
    warning: str | None = None

    @property
    def has_image(self) -> bool:
        return self.image_path is not None

    @property
    def data_image_count(self) -> int:
        return sum(len(foil.data_images) for foil in self.foil_holes)


@dataclass(frozen=True, slots=True)
class SlotContent:
    """Discovered report content for one autoloader slot."""

    grid: GridFolder
    grid_squares: tuple[GridSquareRecord, ...]
    warnings: tuple[str, ...] = ()

    @property
    def populated_grid_squares(self) -> tuple[GridSquareRecord, ...]:
        return tuple(square for square in self.grid_squares if square.has_image)

    @property
    def foil_hole_count(self) -> int:
        return sum(len(square.foil_holes) for square in self.populated_grid_squares)

    @property
    def data_image_count(self) -> int:
        return sum(square.data_image_count for square in self.populated_grid_squares)
