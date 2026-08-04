"""Generate hierarchical PDF reports for EPU screening sessions."""

from __future__ import annotations

import os
import tempfile
import textwrap
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from math import ceil
from pathlib import Path

from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .fft_power import generate_fft_power_spectrum
from .models import (
    AcquisitionMetadata,
    DataImageRecord,
    FoilHoleRecord,
    GridFolder,
    GridSquareRecord,
    SlotContent,
)
from .naming import DEFAULT_NAMING_PROFILE, NamingProfile
from .overlays import (
    MARKER_COLORS,
    DataOverlayResult,
    GridOverlayResult,
    parse_data_area_shifts,
    render_atlas_overlay,
    render_data_overlay,
    render_grid_square_overlay,
)
from .session_content import discover_session_content
from .theme import DEFAULT_REPORT_THEME, ReportCanvas, ReportTheme

ProgressCallback = Callable[[str], None]
PORTRAIT = letter
LANDSCAPE = landscape(letter)
NAVY = colors.HexColor("#17324D")
SLATE = colors.HexColor("#475569")
LIGHT_SLATE = colors.HexColor("#E2E8F0")
PALE_BLUE = colors.HexColor("#F8FAFC")
ORANGE = colors.HexColor("#C2410C")
FOIL_DETAIL_SIZE = 82
DATA_DETAIL_SIZE = 176
DATA_IMAGES_PER_DETAIL_ROW = 3


@dataclass(frozen=True, slots=True)
class ImageQualityProfile:
    """Resolution and JPEG settings used for images embedded in a report."""

    label: str
    dpi: int
    jpeg_quality: int


IMAGE_QUALITY_PROFILES = {
    "email": ImageQualityProfile("Email - smallest file", 140, 72),
    "standard": ImageQualityProfile("Standard", 200, 82),
    "high": ImageQualityProfile("High detail - largest file", 300, 92),
}
DEFAULT_IMAGE_QUALITY = "email"


def _notify(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _shorten(value: str, width: int) -> str:
    return textwrap.shorten(value, width=width, placeholder="...")


def _fit_text_width(
    value: str,
    max_width: float,
    *,
    font: str,
    size: float,
) -> str:
    """Fit a filename by rendered width while preserving both ends."""

    if stringWidth(value, font, size) <= max_width:
        return value

    placeholder = "..."
    for retained in range(len(value) - 1, 0, -1):
        prefix_length = (retained + 1) // 2
        suffix_length = retained // 2
        suffix = value[-suffix_length:] if suffix_length else ""
        candidate = f"{value[:prefix_length]}{placeholder}{suffix}"
        if stringWidth(candidate, font, size) <= max_width:
            return candidate
    return placeholder


def _themed_font(pdf: canvas.Canvas, legacy_font: str, size: float) -> str:
    if isinstance(pdf, ReportCanvas):
        return pdf.themed_font(legacy_font, size)
    return legacy_font


def _set_marker_fill_color(pdf: canvas.Canvas, value) -> None:
    if isinstance(pdf, ReportCanvas):
        pdf.set_marker_fill_color(value)
    else:
        pdf.setFillColor(value)


def _set_marker_stroke_color(pdf: canvas.Canvas, value) -> None:
    if isinstance(pdf, ReportCanvas):
        pdf.set_marker_stroke_color(value)
    else:
        pdf.setStrokeColor(value)


def _draw_logo(
    pdf: canvas.Canvas,
    path: Path,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    """Draw a PNG/JPEG logo inside a bounding box while preserving alpha."""

    with PILImage.open(path) as opened:
        prepared = ImageOps.exif_transpose(opened)
        prepared = prepared.convert("RGBA" if "A" in prepared.getbands() else "RGB")
        source_width, source_height = prepared.size
        encoded = BytesIO()
        prepared.save(encoded, format="PNG")
    encoded.seek(0)
    scale = min(width / source_width, height / source_height)
    drawn_width = source_width * scale
    drawn_height = source_height * scale
    pdf.drawImage(
        ImageReader(encoded),
        x + (width - drawn_width) / 2,
        y + (height - drawn_height) / 2,
        width=drawn_width,
        height=drawn_height,
        preserveAspectRatio=True,
        mask="auto",
    )


def _draw_footer(pdf: canvas.Canvas, page_size: tuple[float, float]) -> None:
    width, _ = page_size
    theme = pdf.report_theme if isinstance(pdf, ReportCanvas) else DEFAULT_REPORT_THEME
    pdf.saveState()
    pdf.setFillColor(colors.HexColor("#64748B"))
    pdf.setFont("Helvetica", 8)
    page_label = f"Page {pdf.getPageNumber()}"
    if theme.branding.logo is None and theme.branding.footer_text is None:
        pdf.drawCentredString(width / 2, 18, page_label)
    else:
        if theme.branding.logo is not None:
            _draw_logo(pdf, theme.branding.logo, 24, 10, 36, 14)
        if theme.branding.footer_text:
            pdf.drawCentredString(
                width / 2,
                18,
                _fit_text_width(
                    theme.branding.footer_text,
                    width - 180,
                    font=_themed_font(pdf, "Helvetica", 8),
                    size=8,
                ),
            )
        pdf.drawRightString(width - 24, 18, page_label)
    pdf.restoreState()


def _finish_page(pdf: canvas.Canvas, page_size: tuple[float, float]) -> None:
    _draw_footer(pdf, page_size)
    pdf.showPage()


def _draw_fitted_image(
    pdf: canvas.Canvas,
    source: Path | PILImage.Image,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    image_profile: ImageQualityProfile,
    border: bool = False,
) -> None:
    if isinstance(source, Path):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
            with PILImage.open(source) as opened:
                opened.seek(0)
                opened.draft("RGB", maximum_pixels := (
                    max(1, ceil(width * image_profile.dpi / 72)),
                    max(1, ceil(height * image_profile.dpi / 72)),
                ))
                prepared = opened.convert("RGB")
    else:
        prepared = source.convert("RGB")

    source_width, source_height = prepared.size
    if not isinstance(source, Path):
        maximum_pixels = (
            max(1, ceil(width * image_profile.dpi / 72)),
            max(1, ceil(height * image_profile.dpi / 72)),
        )
    prepared.thumbnail(maximum_pixels, PILImage.Resampling.LANCZOS)
    encoded = BytesIO()
    prepared.save(
        encoded,
        format="JPEG",
        quality=image_profile.jpeg_quality,
        optimize=True,
    )
    encoded.seek(0)
    reader = ImageReader(encoded)
    scale = min(width / source_width, height / source_height)
    drawn_width = source_width * scale
    drawn_height = source_height * scale
    drawn_x = x + (width - drawn_width) / 2
    drawn_y = y + (height - drawn_height) / 2
    pdf.drawImage(
        reader,
        drawn_x,
        drawn_y,
        width=drawn_width,
        height=drawn_height,
        preserveAspectRatio=True,
        mask="auto",
    )
    if border:
        pdf.setStrokeColor(LIGHT_SLATE)
        pdf.rect(drawn_x, drawn_y, drawn_width, drawn_height, fill=0, stroke=1)


def _draw_wrapped(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    width_chars: int,
    font: str = "Helvetica",
    size: float = 9,
    leading: float = 11,
    color=SLATE,
    max_lines: int | None = None,
) -> float:
    lines = textwrap.wrap(text, width=max(1, width_chars)) or [""]
    if max_lines is not None:
        lines = lines[:max_lines]
    pdf.setFillColor(color)
    pdf.setFont(font, size)
    for line in lines:
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _format_decimal(value: float, decimals: int = 2) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")


def _aggregate_display(values: Sequence[str | None]) -> str:
    available = sorted(
        {value for value in values if value},
        key=str.casefold,
    )
    if not available:
        return "Unavailable"
    if len(available) == 1:
        return available[0]
    return f"Mixed ({'; '.join(available)})"


def _data_metadata(
    contents: Sequence[SlotContent],
) -> tuple[AcquisitionMetadata, ...]:
    return tuple(
        data.metadata
        for content in contents
        for square in content.grid_squares
        for foil in square.foil_holes
        for data in foil.data_images
        if data.metadata is not None
    )


def _energy_filter_display(metadata: AcquisitionMetadata) -> str | None:
    inserted = metadata.energy_filter_inserted
    width = metadata.energy_filter_slit_width_ev
    if inserted is None and width is None:
        return None
    if inserted is False:
        return "Not inserted"
    status = "Inserted" if inserted is True else "Insertion unknown"
    if width is not None:
        status += f", {_format_decimal(width)} eV slit"
    return status


def _configuration_values(
    contents: Sequence[SlotContent],
) -> tuple[tuple[str, str], ...]:
    metadata = _data_metadata(contents)
    return (
        (
            "Microscope voltage",
            _aggregate_display(
                [
                    (
                        f"{_format_decimal(item.voltage_kv)} kV"
                        if item.voltage_kv is not None
                        else None
                    )
                    for item in metadata
                ]
            ),
        ),
        (
            "Detector",
            _aggregate_display([item.detector for item in metadata]),
        ),
        (
            "Energy filter",
            _aggregate_display(
                [_energy_filter_display(item) for item in metadata]
            ),
        ),
        (
            "Instrument model",
            _aggregate_display([item.instrument_model for item in metadata]),
        ),
        (
            "EPU software version",
            _aggregate_display(
                [item.epu_software_version for item in metadata]
            ),
        ),
    )


def _draw_configuration_card(
    pdf: canvas.Canvas,
    contents: Sequence[SlotContent],
    x: float,
    y: float,
    width: float,
) -> float:
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(x, y, "Data acquisition configuration")

    values = dict(_configuration_values(contents))
    card_top = y - 10
    card_height = 60
    pdf.setFillColor(PALE_BLUE)
    pdf.setStrokeColor(LIGHT_SLATE)
    pdf.roundRect(
        x,
        card_top - card_height,
        width,
        card_height,
        5,
        fill=1,
        stroke=1,
    )

    column_width = width / 2 - 18
    fields = (
        (x + 10, card_top - 15, "Microscope voltage"),
        (x + width / 2 + 4, card_top - 15, "Detector"),
        (x + 10, card_top - 32, "Energy filter"),
        (x + 10, card_top - 49, "Instrument model"),
        (x + width / 2 + 4, card_top - 49, "EPU software version"),
    )
    for field_x, field_y, label in fields:
        available_width = (
            width - 20 if label == "Energy filter" else column_width
        )
        text = f"{label}: {values[label]}"
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(
            field_x,
            field_y,
            _fit_text_width(
                text,
                available_width,
                font=_themed_font(pdf, "Helvetica", 7),
                size=7,
            ),
        )
    return card_top - card_height - 6


def _draw_cover(
    pdf: canvas.Canvas,
    atlas_root: Path,
    contents: Sequence[SlotContent],
    generated_at: datetime,
) -> None:
    pdf.setPageSize(PORTRAIT)
    width, height = PORTRAIT
    margin = 48
    theme = pdf.report_theme if isinstance(pdf, ReportCanvas) else DEFAULT_REPORT_THEME
    title = f"{contents[0].grid.project_number} Screening Report"
    title_width = width - 2 * margin
    if theme.branding.logo is not None:
        title_width -= 132
        _draw_logo(pdf, theme.branding.logo, width - margin - 120, height - 80, 120, 48)
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 25)
    pdf.drawString(
        margin,
        height - 72,
        _fit_text_width(
            title,
            title_width,
            font=_themed_font(pdf, "Helvetica-Bold", 25),
            size=25,
        ),
    )
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(margin, height - 96, f"Atlas session: {atlas_root.name}")
    pdf.drawString(
        margin,
        height - 112,
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M')}",
    )

    y = height - 152
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(margin, y, "Session overview")
    y -= 24

    column_x = (margin, margin + 42, margin + 300, margin + 382, margin + 452)
    headers = ("Slot", "Grid folder", "Squares", "Foils", "Data")
    pdf.setFillColor(NAVY)
    pdf.rect(margin, y - 19, width - 2 * margin, 22, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 8)
    for x, header in zip(column_x, headers):
        pdf.drawString(x + 4, y - 11, header)
    y -= 22

    for index, content in enumerate(contents):
        if y < 90:
            _finish_page(pdf, PORTRAIT)
            pdf.setPageSize(PORTRAIT)
            y = height - 60
        if index % 2:
            pdf.setFillColor(PALE_BLUE)
            pdf.rect(margin, y - 22, width - 2 * margin, 22, fill=1, stroke=0)
        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(column_x[0] + 8, y - 14, str(content.grid.slot))
        pdf.setFont("Helvetica", 7)
        pdf.drawString(
            column_x[1] + 4,
            y - 14,
            _fit_text_width(
                content.grid.name,
                column_x[2] - column_x[1] - 8,
                font=_themed_font(pdf, "Helvetica", 7),
                size=7,
            ),
        )
        values = (
            len(content.grid_squares),
            content.foil_hole_count,
            content.data_image_count,
        )
        for x, value in zip(column_x[2:], values):
            pdf.drawString(x + 10, y - 14, str(value))
        pdf.setStrokeColor(LIGHT_SLATE)
        pdf.line(margin, y - 22, width - margin, y - 22)
        y -= 22

    y -= 14
    y = _draw_configuration_card(
        pdf,
        contents,
        margin,
        y,
        width - 2 * margin,
    )

    warnings = [warning for content in contents for warning in content.warnings]
    if warnings:
        y -= 16
        pdf.setFillColor(ORANGE)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(margin, y, "Discovery notices")
        y -= 15
        for warning in warnings[:16]:
            if y < 48:
                break
            y = _draw_wrapped(
                pdf,
                f"- {warning}",
                margin,
                y,
                width_chars=88,
                size=7,
                leading=9,
                color=ORANGE,
                max_lines=2,
            )
    _finish_page(pdf, PORTRAIT)


def _draw_grid_summary_rows(
    pdf: canvas.Canvas,
    records: Sequence[GridSquareRecord],
    x: float,
    y: float,
    *,
    max_rows: int,
    marker_labels: dict[str, int],
) -> int:
    displayed = records[:max_rows]
    pdf.setFillColor(NAVY)
    pdf.rect(x, y - 17, 516, 20, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 7)
    for offset, label in (
        (4, "#"),
        (24, "GridSquare"),
        (170, "Acquired"),
        (285, "FoilHoles"),
        (355, "Data"),
        (405, "Status"),
    ):
        pdf.drawString(x + offset, y - 10, label)
    y -= 20
    for index, record in enumerate(displayed):
        if index % 2:
            pdf.setFillColor(PALE_BLUE)
            pdf.rect(x, y - 17, 516, 18, fill=1, stroke=0)
        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica", 7)
        acquired = record.acquired_at.strftime("%Y-%m-%d %H:%M:%S") if record.acquired_at else "-"
        values = (
            (6, str(marker_labels.get(record.grid_square_id, "-"))),
            (24, record.path.name),
            (170, acquired),
            (308, str(len(record.foil_holes))),
            (370, str(record.data_image_count)),
            (405, "Ready" if record.has_image else "Missing JPG"),
        )
        for offset, value in values:
            displayed = (
                _fit_text_width(
                    value,
                    140,
                    font=_themed_font(pdf, "Helvetica", 7),
                    size=7,
                )
                if offset == 24
                else _shorten(value, 26)
            )
            pdf.drawString(x + offset, y - 11, displayed)
        pdf.setStrokeColor(LIGHT_SLATE)
        pdf.line(x, y - 17, x + 516, y - 17)
        y -= 18
    return len(displayed)


def _draw_slot_atlas_page(
    pdf: canvas.Canvas,
    content: SlotContent,
    image_profile: ImageQualityProfile,
    naming_profile: NamingProfile,
) -> None:
    pdf.setPageSize(PORTRAIT)
    width, height = PORTRAIT
    margin = 48
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(margin, height - 62, f"Slot {content.grid.slot}")
    _draw_wrapped(
        pdf,
        content.grid.name,
        margin,
        height - 82,
        width_chars=82,
        size=8,
        leading=10,
        max_lines=2,
    )

    image_y = 330
    marker_labels: dict[str, int] = {}
    if content.grid.atlas_image and content.grid.atlas_image.is_file():
        atlas_source: Path | PILImage.Image = content.grid.atlas_image
        atlas_warning = None
        try:
            atlas_overlay = render_atlas_overlay(
                content.grid.atlas_image,
                content.grid_squares,
                metadata_name=naming_profile.atlas_metadata_name,
            )
            atlas_source = atlas_overlay.image
            marker_labels = {
                marker.grid_square_id: marker.label
                for marker in atlas_overlay.markers
            }
            atlas_warning = atlas_overlay.warning
        except Exception as exc:
            atlas_warning = f"GridSquare Atlas overlay unavailable: {exc}"
        _draw_fitted_image(
            pdf,
            atlas_source,
            76,
            image_y,
            460,
            380,
            image_profile=image_profile,
            border=True,
        )
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(width / 2, image_y - 14, content.grid.atlas_image.name)
        if atlas_warning:
            pdf.setFillColor(ORANGE)
            pdf.setFont("Helvetica", 7)
            pdf.drawCentredString(
                width / 2,
                image_y - 25,
                _shorten(atlas_warning, 88),
            )
    else:
        pdf.setFillColor(colors.HexColor("#FFF7ED"))
        pdf.roundRect(76, image_y + 90, 460, 170, 8, fill=1, stroke=0)
        pdf.setFillColor(ORANGE)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawCentredString(
            width / 2,
            image_y + 180,
            f"No atlas image matched {naming_profile.name}",
        )

    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(margin, 286, "GridSquare summary")
    consumed = _draw_grid_summary_rows(
        pdf,
        content.grid_squares,
        margin,
        266,
        max_rows=11,
        marker_labels=marker_labels,
    )
    _finish_page(pdf, PORTRAIT)

    remaining = content.grid_squares[consumed:]
    while remaining:
        pdf.setPageSize(PORTRAIT)
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(
            margin,
            height - 58,
            f"Slot {content.grid.slot} GridSquares (continued)",
        )
        consumed = _draw_grid_summary_rows(
            pdf,
            remaining,
            margin,
            height - 88,
            max_rows=34,
            marker_labels=marker_labels,
        )
        remaining = remaining[consumed:]
        _finish_page(pdf, PORTRAIT)


def _draw_grid_overview_page(
    pdf: canvas.Canvas,
    content: SlotContent,
    record: GridSquareRecord,
    overlay: GridOverlayResult,
    image_profile: ImageQualityProfile,
) -> None:
    pdf.setPageSize(LANDSCAPE)
    width, height = LANDSCAPE
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(34, height - 42, f"Slot {content.grid.slot} - GridSquare {record.grid_square_id}")
    acquired = record.acquired_at.strftime("%Y-%m-%d %H:%M:%S") if record.acquired_at else "Unknown"
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(34, height - 58, f"Acquired: {acquired}  |  {record.image_path.name if record.image_path else ''}")

    _draw_fitted_image(
        pdf,
        overlay.image,
        34,
        48,
        500,
        490,
        image_profile=image_profile,
        border=True,
    )
    legend_x = 558
    legend_y = height - 92
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(legend_x, legend_y, "Screened FoilHoles")
    legend_y -= 17
    pdf.setStrokeColor(colors.HexColor("#F59E0B"))
    pdf.setLineWidth(1.5)
    pdf.rect(legend_x, legend_y - 3, 12, 12, fill=0, stroke=1)
    pdf.line(legend_x, legend_y + 3, legend_x + 12, legend_y + 3)
    pdf.line(legend_x + 6, legend_y - 3, legend_x + 6, legend_y + 9)
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(legend_x + 17, legend_y, "Image-registered position")
    legend_y -= 14
    pdf.setStrokeColor(colors.HexColor("#22C55E"))
    pdf.circle(legend_x + 6, legend_y + 3, 5, fill=0, stroke=1)
    pdf.line(legend_x + 1, legend_y + 3, legend_x + 11, legend_y + 3)
    pdf.line(legend_x + 6, legend_y - 2, legend_x + 6, legend_y + 8)
    pdf.setFillColor(SLATE)
    pdf.drawString(legend_x + 17, legend_y, "Original metadata fallback")
    legend_y -= 19

    marker_by_id = {marker.foil_id: marker for marker in overlay.markers}
    for foil in record.foil_holes[:18]:
        marker = marker_by_id.get(foil.foil_id)
        label = marker.label if marker else "-"
        color = colors.HexColor("#22C55E") if marker else colors.HexColor("#94A3B8")
        pdf.setFillColor(color)
        pdf.circle(legend_x + 7, legend_y + 3, 6, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(legend_x + 7, legend_y + 1, str(label))
        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(
            legend_x + 20,
            legend_y,
            f"{foil.foil_id}  |  {len(foil.data_images)} Data image(s)",
        )
        legend_y -= 21
    if len(record.foil_holes) > 18:
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(
            legend_x,
            legend_y,
            f"+ {len(record.foil_holes) - 18} additional FoilHoles",
        )
        legend_y -= 18
    if overlay.warning:
        _draw_wrapped(
            pdf,
            overlay.warning,
            legend_x,
            max(48, legend_y - 8),
            width_chars=38,
            size=8,
            leading=10,
            color=ORANGE,
            max_lines=4,
        )
    _finish_page(pdf, LANDSCAPE)


def _draw_data_thumbnail(
    pdf: canvas.Canvas,
    record: DataImageRecord,
    label: int,
    x: float,
    y: float,
    size: float,
    image_profile: ImageQualityProfile,
) -> None:
    color = colors.HexColor(MARKER_COLORS[(label - 1) % len(MARKER_COLORS)])
    try:
        _draw_fitted_image(
            pdf,
            record.path,
            x,
            y,
            size,
            size,
            image_profile=image_profile,
            border=False,
        )
    except Exception:
        pdf.setFillColor(colors.HexColor("#F1F5F9"))
        pdf.rect(x, y, size, size, fill=1, stroke=0)
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica", 7)
        pdf.drawCentredString(x + size / 2, y + size / 2, "Image unavailable")
    _set_marker_stroke_color(pdf, color)
    pdf.setLineWidth(2)
    pdf.rect(x, y, size, size, fill=0, stroke=1)
    _set_marker_fill_color(pdf, color)
    pdf.circle(x + 9, y + size - 9, 8, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawCentredString(x + 9, y + size - 11, str(label))

    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 6)
    pdf.drawCentredString(
        x + size / 2,
        y + size + 6,
        f"Area {record.acquisition_area_id}",
    )
    line_one, line_two = _data_caption_lines(record)
    _draw_fitted_centered_text(
        pdf,
        line_one,
        x + size / 2,
        y - 9,
        max_width=size + 20,
    )
    _draw_fitted_centered_text(
        pdf,
        line_two,
        x + size / 2,
        y - 18,
        max_width=size + 20,
    )


def _draw_fitted_centered_text(
    pdf: canvas.Canvas,
    value: str,
    center_x: float,
    y: float,
    *,
    max_width: float,
) -> None:
    font = _themed_font(pdf, "Helvetica", 6)
    font_size = 6.0
    while (
        font_size > 4.5
        and stringWidth(value, font, font_size) > max_width
    ):
        font_size -= 0.25
    pdf.setFillColor(SLATE)
    pdf.setFont(font, font_size)
    pdf.drawCentredString(center_x, y, value)


def _data_caption_lines(record: DataImageRecord) -> tuple[str, str]:
    metadata = record.metadata
    if metadata is None:
        acquired_at = record.acquired_at
        return (
            "Mag unavailable | Pixel unavailable | Dose unavailable",
            (
                f"Defocus unavailable | {acquired_at:%Y-%m-%d %H:%M:%S}"
                if acquired_at is not None
                else "Defocus unavailable | Acquisition time unavailable"
            ),
        )

    magnification = (
        f"Mag {_format_decimal(metadata.magnification)}x"
        if metadata.magnification is not None
        else "Mag unavailable"
    )
    pixel_x = metadata.pixel_size_x_angstrom
    pixel_y = metadata.pixel_size_y_angstrom
    if pixel_x is None and pixel_y is None:
        pixel_size = "Pixel unavailable"
    elif pixel_x is None or pixel_y is None:
        pixel_size = (
            f"Pixel {(pixel_x if pixel_x is not None else pixel_y):.3f} Å/px"
        )
    elif abs(pixel_x - pixel_y) <= max(abs(pixel_x), abs(pixel_y), 1) * 1e-6:
        pixel_size = f"Pixel {pixel_x:.3f} Å/px"
    else:
        pixel_size = f"Pixel {pixel_x:.3f} x {pixel_y:.3f} Å/px"

    dose = (
        f"Dose {metadata.total_dose_e_per_angstrom2:.2f} e-/Å²"
        if metadata.total_dose_e_per_angstrom2 is not None
        else "Dose unavailable"
    )
    defocus = (
        f"Defocus {metadata.recorded_defocus_um:.2f} µm"
        if metadata.recorded_defocus_um is not None
        else "Defocus unavailable"
    )
    acquired_at = metadata.acquired_at or record.acquired_at
    acquisition_time = (
        f"{acquired_at:%Y-%m-%d %H:%M:%S}"
        if acquired_at is not None
        else "Acquisition time unavailable"
    )
    return (
        f"{magnification} | {pixel_size} | {dose}",
        f"{defocus} | {acquisition_time}",
    )


def _draw_foil_row(
    pdf: canvas.Canvas,
    foil: FoilHoleRecord,
    overlay: DataOverlayResult | None,
    data_chunk: Sequence[tuple[int, DataImageRecord]],
    y_top: float,
    *,
    continued: bool,
    image_profile: ImageQualityProfile,
) -> None:
    row_x = 28
    row_width = LANDSCAPE[0] - 56
    row_height = 238
    row_bottom = y_top - row_height
    pdf.setFillColor(PALE_BLUE)
    pdf.roundRect(row_x, row_bottom, row_width, row_height, 7, fill=1, stroke=0)

    title = f"FoilHole {foil.foil_id}"
    if continued:
        title += " (continued)"
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(row_x + 10, y_top - 18, title)

    foil_box_x = row_x + 10
    foil_box_y = row_bottom + 70
    foil_box_size = FOIL_DETAIL_SIZE
    if overlay is not None:
        _draw_fitted_image(
            pdf,
            overlay.image,
            foil_box_x,
            foil_box_y,
            foil_box_size,
            foil_box_size,
            image_profile=image_profile,
            border=True,
        )
    elif foil.image_path:
        _draw_fitted_image(
            pdf,
            foil.image_path,
            foil_box_x,
            foil_box_y,
            foil_box_size,
            foil_box_size,
            image_profile=image_profile,
            border=True,
        )
    else:
        pdf.setFillColor(colors.HexColor("#E2E8F0"))
        pdf.rect(foil_box_x, foil_box_y, foil_box_size, foil_box_size, fill=1, stroke=0)
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(
            foil_box_x + foil_box_size / 2,
            foil_box_y + foil_box_size / 2,
            "FoilHole image missing",
        )

    data_x = row_x + 112
    if not foil.data_images:
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica-Oblique", 10)
        pdf.drawString(data_x, y_top - 65, "No Data images collected.")
        return

    if overlay and overlay.warning:
        pdf.setFillColor(ORANGE)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(data_x, y_top - 19, _shorten(overlay.warning, 82))

    thumb_size = DATA_DETAIL_SIZE
    column_step = 202
    thumb_y = row_bottom + 25
    for local_index, (label, data_record) in enumerate(data_chunk):
        thumb_x = data_x + local_index * column_step
        _draw_data_thumbnail(
            pdf,
            data_record,
            label,
            thumb_x,
            thumb_y,
            thumb_size,
            image_profile,
        )


def _foil_segments(
    foil: FoilHoleRecord,
) -> list[tuple[bool, tuple[tuple[int, DataImageRecord], ...]]]:
    numbered = tuple(enumerate(foil.data_images, start=1))
    if not numbered:
        return [(False, ())]
    return [
        (
            start > 0,
            numbered[start : start + DATA_IMAGES_PER_DETAIL_ROW],
        )
        for start in range(0, len(numbered), DATA_IMAGES_PER_DETAIL_ROW)
    ]


def _draw_foil_detail_pages(
    pdf: canvas.Canvas,
    content: SlotContent,
    record: GridSquareRecord,
    overlays: dict[str, DataOverlayResult | None],
    image_profile: ImageQualityProfile,
) -> None:
    segments = [
        (foil, continued, chunk)
        for foil in record.foil_holes
        for continued, chunk in _foil_segments(foil)
    ]
    if not segments:
        return

    for page_start in range(0, len(segments), 2):
        pdf.setPageSize(LANDSCAPE)
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(
            28,
            LANDSCAPE[1] - 32,
            f"Slot {content.grid.slot} - GridSquare {record.grid_square_id} - FoilHole details",
        )
        page_segments = segments[page_start : page_start + 2]
        for row_index, (foil, continued, chunk) in enumerate(page_segments):
            y_top = LANDSCAPE[1] - 50 - row_index * 248
            _draw_foil_row(
                pdf,
                foil,
                overlays.get(foil.foil_id),
                chunk,
                y_top,
                continued=continued,
                image_profile=image_profile,
            )
        _finish_page(pdf, LANDSCAPE)


def _nyquist_caption(record: DataImageRecord) -> str:
    metadata = record.metadata
    if metadata is None:
        return "Nyquist unavailable"
    pixel_x = metadata.pixel_size_x_angstrom
    pixel_y = metadata.pixel_size_y_angstrom
    if pixel_x is None and pixel_y is None:
        return "Nyquist unavailable"
    if pixel_x is None or pixel_y is None:
        pixel = pixel_x if pixel_x is not None else pixel_y
        assert pixel is not None
        return f"Nyquist {2 * pixel:.3f} Å"
    if abs(pixel_x - pixel_y) <= max(abs(pixel_x), abs(pixel_y), 1) * 1e-6:
        return f"Nyquist {2 * pixel_x:.3f} Å"
    return f"Nyquist {2 * pixel_x:.3f} x {2 * pixel_y:.3f} Å"


def _draw_fft_thumbnail(
    pdf: canvas.Canvas,
    record: DataImageRecord,
    label: int,
    x: float,
    y: float,
    size: float,
    image_profile: ImageQualityProfile,
) -> None:
    color = colors.HexColor(MARKER_COLORS[(label - 1) % len(MARKER_COLORS)])
    unavailable_message: str | None = None
    if record.mrc_path is None:
        unavailable_message = "MRC unavailable"
    else:
        try:
            spectrum = generate_fft_power_spectrum(record.mrc_path)
            try:
                _draw_fitted_image(
                    pdf,
                    spectrum,
                    x,
                    y,
                    size,
                    size,
                    image_profile=image_profile,
                    border=False,
                )
            finally:
                spectrum.close()
        except Exception:
            unavailable_message = "FFT unavailable"

    if unavailable_message is not None:
        pdf.setFillColor(colors.HexColor("#0F172A"))
        pdf.rect(x, y, size, size, fill=1, stroke=0)
        pdf.setFillColor(colors.HexColor("#CBD5E1"))
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(
            x + size / 2,
            y + size / 2,
            unavailable_message,
        )

    _set_marker_stroke_color(pdf, color)
    pdf.setLineWidth(2)
    pdf.rect(x, y, size, size, fill=0, stroke=1)
    _set_marker_fill_color(pdf, color)
    pdf.circle(x + 9, y + size - 9, 8, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawCentredString(x + 9, y + size - 11, str(label))

    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 6)
    pdf.drawCentredString(
        x + size / 2,
        y + size + 6,
        f"Area {record.acquisition_area_id}",
    )
    _draw_fitted_centered_text(
        pdf,
        _nyquist_caption(record),
        x + size / 2,
        y - 9,
        max_width=size + 20,
    )
    filename = record.mrc_path.name if record.mrc_path is not None else "MRC not found"
    _draw_fitted_centered_text(
        pdf,
        filename,
        x + size / 2,
        y - 18,
        max_width=size + 20,
    )


def _draw_fft_row(
    pdf: canvas.Canvas,
    foil: FoilHoleRecord,
    data_chunk: Sequence[tuple[int, DataImageRecord]],
    y_top: float,
    *,
    continued: bool,
    image_profile: ImageQualityProfile,
    progress_callback: ProgressCallback | None,
) -> None:
    row_x = 28
    row_width = LANDSCAPE[0] - 56
    row_height = 238
    row_bottom = y_top - row_height
    pdf.setFillColor(PALE_BLUE)
    pdf.roundRect(row_x, row_bottom, row_width, row_height, 7, fill=1, stroke=0)

    title = f"FFT power spectra - FoilHole {foil.foil_id}"
    if continued:
        title += " (continued)"
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(row_x + 10, y_top - 18, title)

    thumb_size = DATA_DETAIL_SIZE
    column_step = 202
    thumb_y = row_bottom + 25
    data_x = row_x + 112
    for local_index, (label, data_record) in enumerate(data_chunk):
        _notify(
            progress_callback,
            (
                f"Generating FFT for FoilHole {foil.foil_id}, "
                f"Area {data_record.acquisition_area_id}..."
            ),
        )
        thumb_x = data_x + local_index * column_step
        _draw_fft_thumbnail(
            pdf,
            data_record,
            label,
            thumb_x,
            thumb_y,
            thumb_size,
            image_profile,
        )


def _draw_paired_foil_fft_pages(
    pdf: canvas.Canvas,
    content: SlotContent,
    record: GridSquareRecord,
    overlays: dict[str, DataOverlayResult | None],
    image_profile: ImageQualityProfile,
    progress_callback: ProgressCallback | None,
) -> None:
    segments = [
        (foil, continued, chunk)
        for foil in record.foil_holes
        for continued, chunk in _foil_segments(foil)
    ]
    if not segments:
        return

    for foil, continued, chunk in segments:
        pdf.setPageSize(LANDSCAPE)
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(
            28,
            LANDSCAPE[1] - 32,
            (
                f"Slot {content.grid.slot} - GridSquare "
                f"{record.grid_square_id} - FoilHole details and FFT power spectra"
            ),
        )
        _draw_foil_row(
            pdf,
            foil,
            overlays.get(foil.foil_id),
            chunk,
            LANDSCAPE[1] - 50,
            continued=continued,
            image_profile=image_profile,
        )
        if chunk:
            _draw_fft_row(
                pdf,
                foil,
                chunk,
                LANDSCAPE[1] - 298,
                continued=continued,
                image_profile=image_profile,
                progress_callback=progress_callback,
            )
        _finish_page(pdf, LANDSCAPE)


def generate_screening_report(
    output_path: str | Path,
    atlas_directory: str | Path,
    grids: list[GridFolder],
    *,
    generated_at: datetime | None = None,
    progress_callback: ProgressCallback | None = None,
    image_quality: str = DEFAULT_IMAGE_QUALITY,
    include_fft: bool = True,
    naming_profile: NamingProfile = DEFAULT_NAMING_PROFILE,
    theme: ReportTheme | None = None,
) -> Path:
    """Generate the complete Atlas > GridSquare > FoilHole > Data report."""

    if not grids:
        raise ValueError("At least one grid folder is required to generate a report.")
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    atlas_root = Path(atlas_directory).expanduser()
    timestamp = generated_at or datetime.now()
    try:
        image_profile = IMAGE_QUALITY_PROFILES[image_quality]
    except KeyError as exc:
        choices = ", ".join(IMAGE_QUALITY_PROFILES)
        raise ValueError(
            f"Unknown image quality {image_quality!r}; choose one of: {choices}."
        ) from exc

    contents: list[SlotContent] = []
    for grid in grids:
        _notify(progress_callback, f"Scanning slot {grid.slot}...")
        contents.append(discover_session_content(grid, naming_profile))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-",
        suffix=".pdf",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)

    try:
        pdf = ReportCanvas(
            str(temporary_output),
            pagesize=PORTRAIT,
            pageCompression=1,
            theme=theme,
        )
        pdf.setTitle(f"{grids[0].project_number} Screening Report")
        pdf.setAuthor("CryoEM Screening Report")
        _draw_cover(pdf, atlas_root, contents, timestamp)

        for content in contents:
            _notify(progress_callback, f"Rendering slot {content.grid.slot} atlas...")
            _draw_slot_atlas_page(pdf, content, image_profile, naming_profile)
            data_shifts = parse_data_area_shifts(
                content.grid.path / naming_profile.session_metadata_name
            )
            metadata_directory = (
                content.grid.path / naming_profile.metadata_directory_name
            )

            for square_index, record in enumerate(
                content.populated_grid_squares,
                start=1,
            ):
                _notify(
                    progress_callback,
                    (
                        f"Rendering slot {content.grid.slot}, GridSquare "
                        f"{square_index}/{len(content.populated_grid_squares)}..."
                    ),
                )
                try:
                    grid_overlay = render_grid_square_overlay(
                        record,
                        metadata_directory,
                        metadata_name=naming_profile.grid_metadata_template.format(
                            grid_square_id=record.grid_square_id
                        ),
                    )
                except Exception as exc:
                    assert record.image_path is not None
                    with PILImage.open(record.image_path) as source:
                        fallback_image = source.convert("RGB")
                    grid_overlay = GridOverlayResult(
                        image=fallback_image,
                        markers=(),
                        warning=f"GridSquare overlay unavailable: {exc}",
                    )
                _draw_grid_overview_page(
                    pdf,
                    content,
                    record,
                    grid_overlay,
                    image_profile,
                )

                foil_overlays: dict[str, DataOverlayResult | None] = {}
                for foil in record.foil_holes:
                    if foil.image_path is None:
                        foil_overlays[foil.foil_id] = None
                        continue
                    try:
                        foil_overlays[foil.foil_id] = render_data_overlay(
                            foil,
                            data_shifts,
                        )
                    except Exception:
                        foil_overlays[foil.foil_id] = None
                if include_fft:
                    _draw_paired_foil_fft_pages(
                        pdf,
                        content,
                        record,
                        foil_overlays,
                        image_profile,
                        progress_callback,
                    )
                else:
                    _draw_foil_detail_pages(
                        pdf,
                        content,
                        record,
                        foil_overlays,
                        image_profile,
                    )

        pdf.save()
        os.replace(temporary_output, output)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise

    _notify(progress_callback, "Report complete.")
    return output


def generate_basic_report(
    output_path: str | Path,
    atlas_directory: str | Path,
    grids: list[GridFolder],
    *,
    generated_at: datetime | None = None,
    image_quality: str = DEFAULT_IMAGE_QUALITY,
    include_fft: bool = True,
    naming_profile: NamingProfile = DEFAULT_NAMING_PROFILE,
    theme: ReportTheme | None = None,
) -> Path:
    """Compatibility wrapper for callers of the original basic generator."""

    return generate_screening_report(
        output_path,
        atlas_directory,
        grids,
        generated_at=generated_at,
        image_quality=image_quality,
        include_fft=include_fft,
        naming_profile=naming_profile,
        theme=theme,
    )
