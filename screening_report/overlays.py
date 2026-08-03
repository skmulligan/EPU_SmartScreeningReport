"""Render EPU coordinate overlays for GridSquare and FoilHole images.

The GridSquare projection behavior is adapted from mvorlander/EPU_mapper at
revision cf6bb63e9a9d6c29b734424d3549415777127140. See
THIRD_PARTY_NOTICES.md for the MIT license notice.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .models import DataImageRecord, FoilHoleRecord, GridSquareRecord

MARKER_COLORS = (
    "#22C55E",
    "#F59E0B",
    "#38BDF8",
    "#F43F5E",
    "#A78BFA",
    "#14B8A6",
    "#FB7185",
    "#84CC16",
)


@dataclass(frozen=True, slots=True)
class MicroscopeGeometry:
    stage_x: float | None = None
    stage_y: float | None = None
    pixel_size: float | None = None
    readout_width: float | None = None
    readout_height: float | None = None
    ref_matrix: tuple[float, float, float, float] | None = None
    center_x: float | None = None
    center_y: float | None = None
    rotation: float | None = None


@dataclass(frozen=True, slots=True)
class FoilMarker:
    foil_id: str
    label: int
    detected_x: float | None
    detected_y: float | None
    detected_in_bounds: bool | None
    refined_x: float | None
    refined_y: float | None
    refined_in_bounds: bool | None
    registered_x: float | None = None
    registered_y: float | None = None
    registered_in_bounds: bool | None = None

    @property
    def x(self) -> float:
        """Retain the original marker API for callers that need one position."""

        if self.detected_x is not None:
            return self.detected_x
        assert self.refined_x is not None
        return self.refined_x

    @property
    def y(self) -> float:
        """Retain the original marker API for callers that need one position."""

        if self.detected_y is not None:
            return self.detected_y
        assert self.refined_y is not None
        return self.refined_y

    @property
    def in_bounds(self) -> bool:
        """Return the detected position status, or refined status as a fallback."""

        value = (
            self.detected_in_bounds
            if self.detected_in_bounds is not None
            else self.refined_in_bounds
        )
        return bool(value)


@dataclass(frozen=True, slots=True)
class TargetPosition:
    """Physical EPU target position and its refinement state."""

    stage_x: float
    stage_y: float
    is_corrected: bool
    is_refined: bool


@dataclass(frozen=True, slots=True)
class DataMarker:
    record: DataImageRecord
    label: int
    x: float
    y: float
    width: float
    height: float
    color: str


@dataclass(frozen=True, slots=True)
class AtlasMarker:
    grid_square_id: str
    label: int
    x: float
    y: float
    in_bounds: bool


@dataclass(frozen=True, slots=True)
class AtlasOverlayResult:
    image: Image.Image
    markers: tuple[AtlasMarker, ...]
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class GridOverlayResult:
    image: Image.Image
    markers: tuple[FoilMarker, ...]
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class DataOverlayResult:
    image: Image.Image
    markers: tuple[DataMarker, ...]
    warning: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _float_text(element: ET.Element | None) -> float | None:
    if element is None or not element.text:
        return None
    try:
        return float(element.text)
    except ValueError:
        return None


def _parse_xml(path: Path | None) -> ET.Element | None:
    if path is None or not path.is_file():
        return None
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def parse_microscope_geometry(path: Path | None) -> MicroscopeGeometry:
    """Extract stage, scale, detector, transform, and center information."""

    root = _parse_xml(path)
    if root is None:
        return MicroscopeGeometry()

    stage_x = stage_y = None
    pixel_size = None
    readout_width = readout_height = None
    matrix = None
    center_x = center_y = None
    rotation = None

    for element in root.iter():
        name = _local_name(element.tag)
        if name == "stage" and (stage_x is None or stage_y is None):
            position = next(
                (child for child in element.iter() if _local_name(child.tag) == "position"),
                None,
            )
            if position is not None:
                values = {
                    _local_name(child.tag): _float_text(child)
                    for child in position
                }
                stage_x = values.get("x")
                stage_y = values.get("y")
        elif name == "pixelsize" and pixel_size is None:
            pixel_size = next(
                (
                    value
                    for child in element.iter()
                    if _local_name(child.tag) == "numericvalue"
                    and (value := _float_text(child)) is not None
                ),
                None,
            )
        elif name == "readoutarea" and (
            readout_width is None or readout_height is None
        ):
            values = {
                _local_name(child.tag): _float_text(child)
                for child in element.iter()
            }
            readout_width = values.get("width")
            readout_height = values.get("height")
        elif name == "matrix" and matrix is None:
            values = {
                _local_name(child.tag): _float_text(child)
                for child in element
            }
            required = ("_m11", "_m12", "_m21", "_m22")
            if all(values.get(key) is not None for key in required):
                matrix = tuple(float(values[key]) for key in required)
        elif name == "center" and (center_x is None or center_y is None):
            values = {
                _local_name(child.tag): _float_text(child)
                for child in element
            }
            center_x = values.get("x")
            center_y = values.get("y")
        elif name == "rotation" and rotation is None:
            rotation = _float_text(element)

    return MicroscopeGeometry(
        stage_x=stage_x,
        stage_y=stage_y,
        pixel_size=pixel_size,
        readout_width=readout_width,
        readout_height=readout_height,
        ref_matrix=matrix,
        center_x=center_x,
        center_y=center_y,
        rotation=rotation,
    )


def parse_dm_pixel_centers(
    metadata_path: Path,
) -> dict[str, tuple[float, float]]:
    """Read FoilHole PixelCenter values from a GridSquare DM file."""

    root = _parse_xml(metadata_path)
    if root is None:
        return {}

    centers: dict[str, tuple[float, float]] = {}
    for pair in root.iter():
        if not _local_name(pair.tag).startswith(
            "keyvaluepairofinttargetlocation"
        ):
            continue
        value = next(
            (child for child in pair if _local_name(child.tag) == "value"),
            None,
        )
        if value is None:
            continue
        id_element = next(
            (child for child in value if _local_name(child.tag) == "id"),
            None,
        )
        pixel_center = next(
            (
                child
                for child in value
                if _local_name(child.tag) == "pixelcenter"
            ),
            None,
        )
        if (
            id_element is None
            or not id_element.text
            or pixel_center is None
        ):
            continue
        coordinates = {
            _local_name(child.tag): _float_text(child)
            for child in pixel_center
        }
        if coordinates.get("x") is None or coordinates.get("y") is None:
            continue
        centers[id_element.text.strip()] = (
            float(coordinates["x"]),
            float(coordinates["y"]),
        )
    return centers


def parse_dm_target_positions(
    metadata_path: Path,
) -> dict[str, tuple[float, float]]:
    """Read stage target values using EPU_mapper's serialization traversal."""

    root = _parse_xml(metadata_path)
    if root is None:
        return {}
    positions: dict[str, tuple[float, float]] = {}
    arrays = (
        element
        for element in root.iter()
        if _local_name(element.tag) == "m_serializationarray"
    )
    for array in arrays:
        for node in list(array):
            foil_id = None
            x = y = None
            for element in node.iter():
                name = _local_name(element.tag)
                if name == "key" and element.text:
                    foil_id = element.text.strip()
                elif name == "x" and element.text and x is None:
                    x = _float_text(element)
                elif name == "y" and element.text and y is None:
                    y = _float_text(element)
            if foil_id and x is not None and y is not None:
                positions[foil_id] = (x, y)
        if positions:
            break
    return positions


def parse_dm_refined_target_positions(
    metadata_path: Path,
) -> dict[str, TargetPosition]:
    """Read final per-hole StagePosition values and correction flags.

    ``StagePosition`` is intentionally selected instead of
    ``CorrectedStagePosition``. EPU updates the former after hole-center
    refinement, while the latter represents the position-correction transform
    applied before that refinement image was evaluated.
    """

    root = _parse_xml(metadata_path)
    if root is None:
        return {}

    targets: dict[str, TargetPosition] = {}
    for pair in root.iter():
        if not _local_name(pair.tag).startswith(
            "keyvaluepairofinttargetlocation"
        ):
            continue
        key = next(
            (child for child in pair if _local_name(child.tag) == "key"),
            None,
        )
        value = next(
            (child for child in pair if _local_name(child.tag) == "value"),
            None,
        )
        if key is None or not key.text or value is None:
            continue
        stage = next(
            (
                child
                for child in value
                if _local_name(child.tag) == "stageposition"
            ),
            None,
        )
        if stage is None:
            continue
        coordinates = {
            _local_name(child.tag): _float_text(child)
            for child in stage
        }
        if coordinates.get("x") is None or coordinates.get("y") is None:
            continue

        def _flag(name: str) -> bool:
            element = next(
                (child for child in value if _local_name(child.tag) == name),
                None,
            )
            return bool(
                element is not None
                and (element.text or "").strip().lower() == "true"
            )

        targets[key.text.strip()] = TargetPosition(
            stage_x=float(coordinates["x"]),
            stage_y=float(coordinates["y"]),
            is_corrected=_flag("ispositioncorrected"),
            is_refined=_flag("ispositionrefined"),
        )
    return targets


def parse_data_area_shifts(
    session_path: Path,
) -> dict[str, tuple[float, float]]:
    """Read Data acquisition-area ShiftInPixels values from EpuSession.dm."""

    root = _parse_xml(session_path)
    if root is None:
        return {}
    shifts: dict[str, tuple[float, float]] = {}
    for pair in root.iter():
        if not _local_name(pair.tag).startswith(
            "keyvaluepairofintdataacquisitionarea"
        ):
            continue
        key = next(
            (child for child in pair if _local_name(child.tag) == "key"),
            None,
        )
        value = next(
            (child for child in pair if _local_name(child.tag) == "value"),
            None,
        )
        if key is None or not key.text or value is None:
            continue
        shift = next(
            (
                element
                for element in value.iter()
                if _local_name(element.tag) == "shiftinpixels"
            ),
            None,
        )
        if shift is None:
            continue
        dimensions = {
            _local_name(child.tag): _float_text(child)
            for child in shift
        }
        if dimensions.get("width") is None or dimensions.get("height") is None:
            continue
        shifts[key.text.strip()] = (
            float(dimensions["width"]),
            float(dimensions["height"]),
        )
    return shifts


def parse_atlas_positions(
    atlas_metadata_path: Path,
) -> tuple[dict[str, tuple[float, float]], tuple[float, float] | None]:
    """Read GridSquare centers and the atlas canvas extent from ``Atlas.dm``."""

    root = _parse_xml(atlas_metadata_path)
    if root is None:
        return {}, None

    centers: dict[str, tuple[float, float]] = {}
    for pair in root.iter():
        if not _local_name(pair.tag).startswith("keyvaluepairofintnodexml"):
            continue
        key = next(
            (child for child in pair if _local_name(child.tag) == "key"),
            None,
        )
        value = next(
            (child for child in pair if _local_name(child.tag) == "value"),
            None,
        )
        if key is None or not key.text or value is None:
            continue
        position = next(
            (
                element
                for element in value.iter()
                if _local_name(element.tag) == "positionontheatlas"
            ),
            None,
        )
        if position is None:
            continue
        center = next(
            (
                child
                for child in position
                if _local_name(child.tag) == "center"
            ),
            None,
        )
        if center is None:
            continue
        coordinates = {
            _local_name(child.tag): _float_text(child)
            for child in center
        }
        if coordinates.get("x") is None or coordinates.get("y") is None:
            continue
        centers[key.text.strip()] = (
            float(coordinates["x"]),
            float(coordinates["y"]),
        )

    maximum_x = maximum_y = 0.0
    for position in root.iter():
        if _local_name(position.tag) != "atlaspixelposition":
            continue
        values = {
            _local_name(child.tag): _float_text(child)
            for child in position
        }
        if all(values.get(name) is not None for name in ("x", "y", "width", "height")):
            maximum_x = max(
                maximum_x,
                float(values["x"]) + float(values["width"]),
            )
            maximum_y = max(
                maximum_y,
                float(values["y"]) + float(values["height"]),
            )

    reference_size = (
        (maximum_x, maximum_y)
        if maximum_x > 0 and maximum_y > 0
        else None
    )
    if reference_size is None and centers:
        reference_size = (
            max(center[0] for center in centers.values()) + 1.0,
            max(center[1] for center in centers.values()) + 1.0,
        )
    return centers, reference_size


def _inverse_matrix(
    matrix: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if matrix is None:
        return None
    m11, m12, m21, m22 = matrix
    determinant = m11 * m22 - m12 * m21
    if abs(determinant) < 1e-30:
        return None
    return (
        m22 / determinant,
        -m12 / determinant,
        -m21 / determinant,
        m11 / determinant,
    )


def _fallback_foil_position(
    grid_geometry: MicroscopeGeometry,
    foil_geometry: MicroscopeGeometry,
    grid_image_size: tuple[int, int],
    foil_image_size: tuple[int, int] | None = None,
) -> tuple[float, float, bool] | None:
    if (
        grid_geometry.stage_x is None
        or grid_geometry.stage_y is None
        or foil_geometry.stage_x is None
        or foil_geometry.stage_y is None
    ):
        return None

    foil_width = float(foil_image_size[0]) if foil_image_size else (
        foil_geometry.readout_width or 4096.0
    )
    foil_height = float(foil_image_size[1]) if foil_image_size else (
        foil_geometry.readout_height or 4096.0
    )
    center_x = foil_geometry.center_x or foil_width / 2.0
    center_y = foil_geometry.center_y or foil_height / 2.0
    offset_x = center_x - foil_width / 2.0
    offset_y = center_y - foil_height / 2.0
    center_stage_x = foil_geometry.stage_x
    center_stage_y = foil_geometry.stage_y

    if foil_geometry.ref_matrix:
        fm11, fm12, fm21, fm22 = foil_geometry.ref_matrix
        center_stage_x += fm11 * offset_x + fm12 * offset_y
        center_stage_y += fm21 * offset_x + fm22 * offset_y
    elif foil_geometry.pixel_size:
        center_stage_x += offset_x * foil_geometry.pixel_size
        center_stage_y -= offset_y * foil_geometry.pixel_size

    delta_x = center_stage_x - grid_geometry.stage_x
    delta_y = center_stage_y - grid_geometry.stage_y
    grid_width = grid_geometry.readout_width or 4096.0
    grid_height = grid_geometry.readout_height or 4096.0
    inverse = _inverse_matrix(grid_geometry.ref_matrix)
    if inverse:
        i11, i12, i21, i22 = inverse
        pixel_dx = i11 * delta_x + i12 * delta_y
        pixel_dy = i21 * delta_x + i22 * delta_y
        raw_x = grid_width / 2.0 + pixel_dx
        raw_y = grid_height / 2.0 + pixel_dy
    elif grid_geometry.pixel_size:
        raw_x = grid_width / 2.0 + delta_x / grid_geometry.pixel_size
        raw_y = grid_height / 2.0 - delta_y / grid_geometry.pixel_size
    else:
        return None

    image_width, image_height = grid_image_size
    x = raw_x * image_width / grid_width
    y = raw_y * image_height / grid_height
    return x, y, 0 <= x < image_width and 0 <= y < image_height


def _project_stage_position(
    grid_geometry: MicroscopeGeometry,
    stage_x: float,
    stage_y: float,
    grid_image_size: tuple[int, int],
) -> tuple[float, float, bool] | None:
    """Project a physical stage position onto the captured GridSquare image."""

    if grid_geometry.stage_x is None or grid_geometry.stage_y is None:
        return None
    grid_width = grid_geometry.readout_width or 4096.0
    grid_height = grid_geometry.readout_height or 4096.0
    delta_x = stage_x - grid_geometry.stage_x
    delta_y = stage_y - grid_geometry.stage_y
    inverse = _inverse_matrix(grid_geometry.ref_matrix)
    if inverse:
        i11, i12, i21, i22 = inverse
        raw_x = grid_width / 2.0 + i11 * delta_x + i12 * delta_y
        raw_y = grid_height / 2.0 + i21 * delta_x + i22 * delta_y
    elif grid_geometry.pixel_size:
        raw_x = grid_width / 2.0 + delta_x / grid_geometry.pixel_size
        raw_y = grid_height / 2.0 - delta_y / grid_geometry.pixel_size
    else:
        return None

    image_width, image_height = grid_image_size
    x = raw_x * image_width / grid_width
    y = raw_y * image_height / grid_height
    return x, y, 0 <= x < image_width and 0 <= y < image_height


TRANSFORMS: dict[str, Callable[[float, float], tuple[float, float]]] = {
    "identity": lambda u, v: (u, v),
    "rot90": lambda u, v: (v, 1.0 - u),
    "rot180": lambda u, v: (1.0 - u, 1.0 - v),
    "rot270": lambda u, v: (1.0 - v, u),
    "mirror_x": lambda u, v: (1.0 - u, v),
    "mirror_y": lambda u, v: (u, 1.0 - v),
    "mirror_diag": lambda u, v: (v, u),
    "mirror_diag_inv": lambda u, v: (1.0 - v, 1.0 - u),
}


def choose_pixel_center_transform(
    normalized_centers: dict[str, tuple[float, float]],
    fallbacks: dict[str, tuple[float, float, bool]],
    image_size: tuple[int, int],
) -> tuple[str, dict[str, tuple[float, float, bool]]]:
    """Choose the rotation/mirror that best agrees with stage fallbacks."""

    image_width, image_height = image_size
    best_name = "identity"
    best_coordinates: dict[str, tuple[float, float, bool]] = {}
    best_key: tuple[int, float, int] | None = None

    for order, (name, transform) in enumerate(TRANSFORMS.items()):
        coordinates: dict[str, tuple[float, float, bool]] = {}
        squared_error = 0.0
        matches = 0
        in_bounds_count = 0
        for foil_id, (u, v) in normalized_centers.items():
            transformed_u, transformed_v = transform(u, v)
            x = transformed_u * image_width
            y = transformed_v * image_height
            in_bounds = 0 <= x < image_width and 0 <= y < image_height
            coordinates[foil_id] = (x, y, in_bounds)
            if in_bounds:
                in_bounds_count += 1
            fallback = fallbacks.get(foil_id)
            if fallback:
                squared_error += (x - fallback[0]) ** 2 + (y - fallback[1]) ** 2
                matches += 1
        mean_error = squared_error / matches if matches else float("inf")
        ranking = (
            -matches,
            mean_error if matches else -float(in_bounds_count),
            order,
        )
        if best_key is None or ranking < best_key:
            best_key = ranking
            best_name = name
            best_coordinates = coordinates
    return best_name, best_coordinates


def _otsu_threshold(values: "np.ndarray") -> float:
    """Return an Otsu threshold for an 8-bit grayscale array."""

    histogram = np.bincount(values.ravel(), minlength=256).astype(float)
    total = histogram.sum()
    if total == 0:
        return 0.0
    probability = histogram / total
    cumulative = np.cumsum(probability)
    means = np.cumsum(probability * np.arange(256))
    global_mean = means[-1]
    denominator = cumulative * (1.0 - cumulative)
    variance = np.zeros(256, dtype=float)
    valid = denominator > 0
    variance[valid] = (
        (global_mean * cumulative[valid] - means[valid]) ** 2
        / denominator[valid]
    )
    return float(np.argmax(variance))


def _largest_component_center(mask: "np.ndarray") -> tuple[float, float] | None:
    """Return the center of the largest connected foreground component."""

    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    largest: list[tuple[int, int]] = []
    for start_y, start_x in zip(*np.nonzero(mask & ~visited)):
        if visited[start_y, start_x]:
            continue
        component: list[tuple[int, int]] = []
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        while stack:
            y, x = stack.pop()
            component.append((y, x))
            for next_y in range(max(0, y - 1), min(height, y + 2)):
                for next_x in range(max(0, x - 1), min(width, x + 2)):
                    if mask[next_y, next_x] and not visited[next_y, next_x]:
                        visited[next_y, next_x] = True
                        stack.append((next_y, next_x))
        if len(component) > len(largest):
            largest = component
    if not largest:
        return None
    coordinates = np.asarray(largest, dtype=float)
    return float(np.median(coordinates[:, 1])), float(np.median(coordinates[:, 0]))


def register_pixel_centers_to_foil(
    image: Image.Image,
    coordinates: dict[str, tuple[float, float, bool]],
) -> tuple[dict[str, tuple[float, float, bool]], tuple[float, float] | None]:
    """Translate the complete EPU hole cloud onto the largest visible foil.

    This addresses GridSquare images whose recorded optical center disagrees
    with the target map. It intentionally estimates only a global translation;
    rotation and mirroring remain determined from EPU's physical metadata.
    """

    if len(coordinates) < 8:
        return {}, None
    analysis_size = 128
    grayscale_small = image.convert("L").resize(
        (analysis_size, analysis_size),
        Image.Resampling.BILINEAR,
    )
    blurred = grayscale_small.filter(ImageFilter.GaussianBlur(radius=3.0))
    pixels = np.asarray(blurred, dtype=np.uint8)
    threshold = _otsu_threshold(pixels)
    center = _largest_component_center(pixels > threshold)
    if center is None:
        return {}, None

    scale_x = image.width / analysis_size
    scale_y = image.height / analysis_size
    foil_x = center[0] * scale_x
    foil_y = center[1] * scale_y
    points = np.asarray(
        [(x, y) for x, y, _ in coordinates.values()],
        dtype=float,
    )
    target_x = float(np.median(points[:, 0]))
    target_y = float(np.median(points[:, 1]))
    gross_offset_threshold = min(image.size) * 0.12
    if math.hypot(foil_x - target_x, foil_y - target_y) < gross_offset_threshold:
        return {}, None

    grayscale = image.convert("L")
    small_radius = max(1.0, min(image.size) / 256.0)
    large_radius = max(5.0, min(image.size) / 36.0)
    fine = np.asarray(
        grayscale.filter(ImageFilter.GaussianBlur(radius=small_radius)),
        dtype=float,
    )
    background = np.asarray(
        grayscale.filter(ImageFilter.GaussianBlur(radius=large_radius)),
        dtype=float,
    )
    response = fine - background
    response_scale = max(float(np.std(response)), 1.0)
    minimum_matches = max(6, math.ceil(len(points) * 0.65))

    def _score(offset_x: int, offset_y: int) -> float:
        moved_x = np.rint(points[:, 0] + offset_x).astype(int)
        moved_y = np.rint(points[:, 1] + offset_y).astype(int)
        valid = (
            (moved_x >= 0)
            & (moved_x < image.width)
            & (moved_y >= 0)
            & (moved_y < image.height)
        )
        if int(valid.sum()) < minimum_matches:
            return -float("inf")
        contrast = float(np.mean(response[moved_y[valid], moved_x[valid]]))
        center_distance = math.hypot(
            target_x + offset_x - foil_x,
            target_y + offset_y - foil_y,
        )
        return contrast - 0.12 * response_scale * center_distance / min(image.size)

    coarse_step = max(2, int(round(min(image.size) / 128)))
    limit_x = int(image.width * 0.45)
    limit_y = int(image.height * 0.45)
    best_x = best_y = 0
    best_score = _score(0, 0)
    for offset_y in range(-limit_y, limit_y + 1, coarse_step):
        for offset_x in range(-limit_x, limit_x + 1, coarse_step):
            score = _score(offset_x, offset_y)
            if score > best_score:
                best_x, best_y, best_score = offset_x, offset_y, score
    for offset_y in range(best_y - coarse_step, best_y + coarse_step + 1):
        for offset_x in range(best_x - coarse_step, best_x + coarse_step + 1):
            score = _score(offset_x, offset_y)
            if score > best_score:
                best_x, best_y, best_score = offset_x, offset_y, score

    offset_x = float(best_x)
    offset_y = float(best_y)
    # A periodic hole lattice commonly produces a stronger adjacent-hole peak.
    # This registration is therefore limited to gross square-center failures;
    # small corrections are less trustworthy than EPU's direct PixelCenter.
    if math.hypot(offset_x, offset_y) < gross_offset_threshold:
        return {}, None

    registered = {}
    for foil_id, (x, y, _) in coordinates.items():
        moved_x = x + offset_x
        moved_y = y + offset_y
        registered[foil_id] = (
            moved_x,
            moved_y,
            0 <= moved_x < image.width and 0 <= moved_y < image.height,
        )
    return registered, (offset_x, offset_y)


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        ("arialbd.ttf", "arial.ttf")
        if bold
        else ("arial.ttf", "DejaVuSans.ttf")
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_numbered_cross(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: int,
    color: str,
    radius: int,
) -> None:
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        outline=color,
        width=max(2, radius // 4),
    )
    draw.line((x - radius, y, x + radius, y), fill=color, width=2)
    draw.line((x, y - radius, x, y + radius), fill=color, width=2)
    text = str(label)
    font = _font(max(12, radius), bold=True)
    text_x = x + radius + 3
    text_y = y - radius
    draw.text(
        (text_x, text_y),
        text,
        fill="white",
        font=font,
        stroke_width=2,
        stroke_fill="black",
    )


def _draw_numbered_circle(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: int,
    color: str,
    radius: int,
) -> None:
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=color,
        outline="white",
        width=max(2, radius // 4),
    )
    text = str(label)
    font = _font(max(11, radius + 2), bold=True)
    box = draw.textbbox((0, 0), text, font=font)
    text_x = x - (box[2] - box[0]) / 2 - box[0]
    text_y = y - (box[3] - box[1]) / 2 - box[1]
    draw.text(
        (text_x, text_y),
        text,
        fill="white",
        font=font,
        stroke_width=1,
        stroke_fill="black",
    )


def _draw_numbered_diamond(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: int,
    color: str,
    radius: int,
) -> None:
    """Draw the refined/acquired position distinctly from detected crosses."""

    points = (
        (x, y - radius),
        (x + radius, y),
        (x, y + radius),
        (x - radius, y),
    )
    draw.line(
        (*points, points[0]),
        fill=color,
        width=max(2, radius // 4),
        joint="curve",
    )
    inner = max(3, radius // 2)
    draw.line((x - inner, y - inner, x + inner, y + inner), fill=color, width=2)
    draw.line((x - inner, y + inner, x + inner, y - inner), fill=color, width=2)
    font = _font(max(10, radius - 1), bold=True)
    draw.text(
        (x + radius + 3, y),
        f"{label}R",
        fill="white",
        font=font,
        stroke_width=2,
        stroke_fill="black",
        anchor="lm",
    )


def _draw_numbered_square(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: int,
    color: str,
    radius: int,
) -> None:
    """Draw an automatically image-registered position."""

    draw.rectangle(
        (x - radius, y - radius, x + radius, y + radius),
        outline=color,
        width=max(2, radius // 4),
    )
    draw.line((x - radius, y, x + radius, y), fill=color, width=2)
    draw.line((x, y - radius, x, y + radius), fill=color, width=2)
    font = _font(max(10, radius - 1), bold=True)
    draw.text(
        (x + radius + 3, y),
        str(label),
        fill="white",
        font=font,
        stroke_width=2,
        stroke_fill="black",
        anchor="lm",
    )


def render_atlas_overlay(
    atlas_image_path: Path,
    grid_squares: tuple[GridSquareRecord, ...],
    *,
    metadata_name: str = "Atlas.dm",
) -> AtlasOverlayResult:
    """Draw acquired GridSquare locations on a slot Atlas image."""

    with Image.open(atlas_image_path) as source:
        image = source.convert("RGB")

    metadata_candidates = (
        atlas_image_path.with_suffix(".dm"),
        atlas_image_path.parent / metadata_name,
    )
    metadata_path = next(
        (path for path in metadata_candidates if path.is_file()),
        None,
    )
    if metadata_path is None:
        return AtlasOverlayResult(
            image=image,
            markers=(),
            warning="GridSquare locations unavailable: Atlas.dm was not found.",
        )

    centers, reference_size = parse_atlas_positions(metadata_path)
    if not centers or reference_size is None:
        return AtlasOverlayResult(
            image=image,
            markers=(),
            warning="GridSquare locations unavailable: Atlas.dm positions could not be parsed.",
        )

    reference_width, reference_height = reference_size
    scale_x = image.width / reference_width
    scale_y = image.height / reference_height
    markers: list[AtlasMarker] = []
    for label, record in enumerate(grid_squares, start=1):
        center = centers.get(record.grid_square_id)
        if center is None:
            continue
        x = center[0] * scale_x
        y = center[1] * scale_y
        in_bounds = 0 <= x < image.width and 0 <= y < image.height
        markers.append(
            AtlasMarker(
                grid_square_id=record.grid_square_id,
                label=label,
                x=min(max(x, 0.0), float(image.width - 1)),
                y=min(max(y, 0.0), float(image.height - 1)),
                in_bounds=in_bounds,
            )
        )

    draw = ImageDraw.Draw(image)
    radius = max(7, int(min(image.size) * 0.016))
    for marker in markers:
        _draw_numbered_circle(
            draw,
            marker.x,
            marker.y,
            marker.label,
            "#38BDF8" if marker.in_bounds else "#EF4444",
            radius,
        )

    warning = None
    if not markers and grid_squares:
        warning = "GridSquare locations unavailable: selected IDs were not found in Atlas.dm."
    elif len(markers) < len(grid_squares):
        warning = (
            f"Located {len(markers)} of {len(grid_squares)} GridSquares on the Atlas."
        )
    return AtlasOverlayResult(
        image=image,
        markers=tuple(markers),
        warning=warning,
    )


def render_grid_square_overlay(
    record: GridSquareRecord,
    metadata_directory: Path,
    *,
    metadata_name: str | None = None,
) -> GridOverlayResult:
    """Draw numbered screened FoilHole positions on a GridSquare image."""

    if record.image_path is None:
        raise ValueError("GridSquare image is required for an overlay.")
    with Image.open(record.image_path) as source:
        image = source.convert("RGB")
    grid_geometry = parse_microscope_geometry(record.xml_path)

    metadata_path = metadata_directory / (
        metadata_name or f"GridSquare_{record.grid_square_id}.dm"
    )
    refined_targets = parse_dm_refined_target_positions(metadata_path)
    fallbacks: dict[str, tuple[float, float, bool]] = {}
    for foil in record.foil_holes:
        foil_image_size = None
        if foil.image_path:
            try:
                with Image.open(foil.image_path) as foil_image:
                    foil_image_size = foil_image.size
            except OSError:
                foil_image_size = None
        fallback = _fallback_foil_position(
            grid_geometry,
            parse_microscope_geometry(foil.xml_path),
            image.size,
            foil_image_size,
        )
        if fallback is None:
            target = refined_targets.get(foil.foil_id)
            if target is not None and (target.is_corrected or target.is_refined):
                fallback = _project_stage_position(
                    grid_geometry,
                    target.stage_x,
                    target.stage_y,
                    image.size,
                )
        if fallback:
            fallbacks[foil.foil_id] = fallback

    refined_positions = dict(fallbacks)

    dm_centers = parse_dm_pixel_centers(metadata_path)
    base_width = grid_geometry.readout_width or 4096.0
    base_height = grid_geometry.readout_height or 4096.0
    normalized = {
        foil_id: (x / base_width, y / base_height)
        for foil_id, (x, y) in dm_centers.items()
        if math.isfinite(x) and math.isfinite(y)
    }
    _, transformed = choose_pixel_center_transform(
        normalized,
        fallbacks,
        image.size,
    )
    registered, _ = register_pixel_centers_to_foil(image, transformed)

    markers: list[FoilMarker] = []
    for label, foil in enumerate(record.foil_holes, start=1):
        detected = transformed.get(foil.foil_id)
        refined = refined_positions.get(foil.foil_id)
        image_registered = registered.get(foil.foil_id)
        if detected is None and image_registered is None:
            continue

        def _clamp(
            coordinates: tuple[float, float, bool] | None,
        ) -> tuple[float | None, float | None, bool | None]:
            if coordinates is None:
                return None, None, None
            x, y, in_bounds = coordinates
            return (
                min(max(x, 0.0), float(image.width - 1)),
                min(max(y, 0.0), float(image.height - 1)),
                in_bounds,
            )

        detected_x, detected_y, detected_in_bounds = _clamp(detected)
        refined_x, refined_y, refined_in_bounds = _clamp(refined)
        registered_x, registered_y, registered_in_bounds = _clamp(
            image_registered
        )
        markers.append(
            FoilMarker(
                foil_id=foil.foil_id,
                label=label,
                detected_x=detected_x,
                detected_y=detected_y,
                detected_in_bounds=detected_in_bounds,
                refined_x=refined_x,
                refined_y=refined_y,
                refined_in_bounds=refined_in_bounds,
                registered_x=registered_x,
                registered_y=registered_y,
                registered_in_bounds=registered_in_bounds,
            )
        )

    draw = ImageDraw.Draw(image)
    radius = max(9, int(min(image.size) * 0.018))
    for marker in markers:
        if marker.registered_x is not None and marker.registered_y is not None:
            _draw_numbered_square(
                draw,
                marker.registered_x,
                marker.registered_y,
                marker.label,
                "#F59E0B" if marker.registered_in_bounds else "#EF4444",
                radius,
            )
        elif marker.detected_x is not None and marker.detected_y is not None:
            _draw_numbered_cross(
                draw,
                marker.detected_x,
                marker.detected_y,
                marker.label,
                "#22C55E" if marker.detected_in_bounds else "#EF4444",
                radius,
            )

    warning = None
    if not markers and record.foil_holes:
        warning = "FoilHole locations unavailable: coordinate metadata could not be parsed."
    elif len(markers) < len(record.foil_holes):
        warning = (
            f"Located {len(markers)} of {len(record.foil_holes)} screened FoilHoles."
        )
    return GridOverlayResult(image=image, markers=tuple(markers), warning=warning)


def render_data_overlay(
    foil: FoilHoleRecord,
    data_area_shifts: dict[str, tuple[float, float]],
) -> DataOverlayResult:
    """Draw numbered Data fields of view on the latest FoilHole image."""

    if foil.image_path is None:
        raise ValueError("FoilHole image is required for a Data overlay.")
    with Image.open(foil.image_path) as source:
        image = source.convert("RGB")
    foil_geometry = parse_microscope_geometry(foil.xml_path)
    base_width = foil_geometry.readout_width or 4096.0
    base_height = foil_geometry.readout_height or 4096.0
    center_x = foil_geometry.center_x or base_width / 2.0
    center_y = foil_geometry.center_y or base_height / 2.0
    scale_x = image.width / base_width
    scale_y = image.height / base_height

    markers: list[DataMarker] = []
    for label, data_record in enumerate(foil.data_images, start=1):
        shift = data_area_shifts.get(data_record.acquisition_area_id)
        data_geometry = parse_microscope_geometry(data_record.xml_path)
        if (
            shift is None
            or foil_geometry.pixel_size is None
            or data_geometry.pixel_size is None
        ):
            continue
        data_width = data_geometry.readout_width or 4096.0
        data_height = data_geometry.readout_height or 4096.0
        field_width = (
            data_width
            * data_geometry.pixel_size
            / foil_geometry.pixel_size
            * scale_x
        )
        field_height = (
            data_height
            * data_geometry.pixel_size
            / foil_geometry.pixel_size
            * scale_y
        )
        x = (center_x + shift[0]) * scale_x
        y = (center_y + shift[1]) * scale_y
        markers.append(
            DataMarker(
                record=data_record,
                label=label,
                x=x,
                y=y,
                width=field_width,
                height=field_height,
                color=MARKER_COLORS[(label - 1) % len(MARKER_COLORS)],
            )
        )

    draw = ImageDraw.Draw(image)
    for marker in markers:
        half_width = marker.width / 2.0
        half_height = marker.height / 2.0
        line_width = max(2, int(min(image.size) * 0.007))
        draw.rectangle(
            (
                marker.x - half_width,
                marker.y - half_height,
                marker.x + half_width,
                marker.y + half_height,
            ),
            outline=marker.color,
            width=line_width,
        )
        center_radius = max(3, line_width)
        draw.ellipse(
            (
                marker.x - center_radius,
                marker.y - center_radius,
                marker.x + center_radius,
                marker.y + center_radius,
            ),
            fill=marker.color,
        )
        font = _font(max(12, int(min(image.size) * 0.03)), bold=True)
        draw.text(
            (
                marker.x + half_width + 3,
                marker.y - half_height,
            ),
            str(marker.label),
            fill="white",
            font=font,
            stroke_width=2,
            stroke_fill="black",
        )

    warning = None
    if foil.data_images and not markers:
        warning = (
            "Data locations unavailable: EpuSession shift or image-scale "
            "metadata could not be parsed."
        )
    elif len(markers) < len(foil.data_images):
        warning = (
            f"Marked {len(markers)} of {len(foil.data_images)} Data locations."
        )
    return DataOverlayResult(image=image, markers=tuple(markers), warning=warning)
