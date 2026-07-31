"""Generate PDF reports from user-confirmed SerialEM image mappings."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfgen import canvas

from .pdf_report import (
    DEFAULT_IMAGE_QUALITY,
    IMAGE_QUALITY_PROFILES,
    LANDSCAPE,
    LIGHT_SLATE,
    NAVY,
    ORANGE,
    PALE_BLUE,
    PORTRAIT,
    SLATE,
    ImageQualityProfile,
    _draw_fitted_image,
    _draw_wrapped,
    _finish_page,
    _fit_text_width,
)
from .serialem import (
    SerialEMSession,
    SerialEMSlot,
    SerialEMSquare,
    build_serialem_slots,
    validate_serialem_session,
)


ProgressCallback = Callable[[str], None]
RECORDS_PER_PAGE = 6


def _notify(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _draw_cover(
    pdf: canvas.Canvas,
    session: SerialEMSession,
    slots: tuple[SerialEMSlot, ...],
    generated_at: datetime,
) -> None:
    pdf.setPageSize(PORTRAIT)
    width, height = PORTRAIT
    margin = 48
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 23)
    pdf.drawString(
        margin,
        height - 70,
        _fit_text_width(session.title, width - 2 * margin, font="Helvetica-Bold", size=23),
    )
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(margin, height - 96, f"Project: {session.project_id}")
    pdf.drawString(margin, height - 112, f"Session: {session.session_name}")
    pdf.drawString(margin, height - 128, f"Generated: {generated_at:%Y-%m-%d %H:%M}")

    square_count = sum(len(slot.squares) for slot in slots)
    record_count = sum(len(square.records) for slot in slots for square in slot.squares)
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(margin, height - 170, "SerialEM session overview")
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(
        margin,
        height - 190,
        f"{len(slots)} slots  |  {square_count} squares  |  {record_count} record images",
    )

    y = height - 225
    columns = (margin, margin + 48, margin + 265, margin + 345, margin + 420)
    pdf.setFillColor(NAVY)
    pdf.rect(margin, y - 19, width - 2 * margin, 22, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 8)
    for x, label in zip(columns, ("Slot", "Label", "Overviews", "Squares", "Records")):
        pdf.drawString(x + 4, y - 11, label)
    y -= 22
    for index, slot in enumerate(slots):
        if index % 2:
            pdf.setFillColor(PALE_BLUE)
            pdf.rect(margin, y - 22, width - 2 * margin, 22, fill=1, stroke=0)
        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica", 8)
        overviews = (1 if slot.primary_overview else 0) + len(slot.supplemental_overviews)
        records = sum(len(square.records) for square in slot.squares)
        values = (
            str(slot.number),
            slot.label or "—",
            str(overviews),
            str(len(slot.squares)),
            str(records),
        )
        widths = (42, 210, 72, 70, 70)
        for x, value, available in zip(columns, values, widths):
            pdf.drawString(
                x + 4,
                y - 14,
                _fit_text_width(value, available, font="Helvetica", size=8),
            )
        pdf.setStrokeColor(LIGHT_SLATE)
        pdf.line(margin, y - 22, width - margin, y - 22)
        y -= 22
    _finish_page(pdf, PORTRAIT)


def _draw_placeholder(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    message: str,
) -> None:
    pdf.setFillColor(colors.HexColor("#FFF7ED"))
    pdf.setStrokeColor(colors.HexColor("#FED7AA"))
    pdf.roundRect(x, y, width, height, 8, fill=1, stroke=1)
    pdf.setFillColor(ORANGE)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawCentredString(x + width / 2, y + height / 2, message)


def _draw_overview_page(
    pdf: canvas.Canvas,
    slot: SerialEMSlot,
    path: Path | None,
    image_profile: ImageQualityProfile,
    *,
    supplemental_index: int | None = None,
) -> None:
    pdf.setPageSize(PORTRAIT)
    width, height = PORTRAIT
    title = f"Slot {slot.number}"
    if slot.label:
        title += f" — {slot.label}"
    if supplemental_index is not None:
        title += f" — Supplemental overview {supplemental_index}"
    else:
        title += " — Primary overview"
    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 19)
    pdf.drawString(48, height - 60, _fit_text_width(title, width - 96, font="Helvetica-Bold", size=19))
    if slot.notes and supplemental_index is None:
        _draw_wrapped(pdf, slot.notes, 48, height - 82, width_chars=90, size=8, max_lines=3)
    image_y = 80
    image_height = height - 180
    if path and path.is_file():
        _draw_fitted_image(
            pdf,
            path,
            48,
            image_y,
            width - 96,
            image_height,
            image_profile=image_profile,
            border=True,
        )
        pdf.setFillColor(SLATE)
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(
            width / 2,
            58,
            _fit_text_width(path.name, width - 96, font="Helvetica", size=8),
        )
    else:
        _draw_placeholder(pdf, 70, 235, width - 140, 250, "No primary overview selected")
    _finish_page(pdf, PORTRAIT)


def _draw_record(
    pdf: canvas.Canvas,
    path: Path,
    x: float,
    y: float,
    width: float,
    height: float,
    image_profile: ImageQualityProfile,
) -> None:
    _draw_fitted_image(
        pdf,
        path,
        x,
        y + 15,
        width,
        height - 15,
        image_profile=image_profile,
        border=True,
    )
    pdf.setFillColor(SLATE)
    pdf.setFont("Helvetica", 7)
    pdf.drawCentredString(
        x + width / 2,
        y + 3,
        _fit_text_width(path.name, width, font="Helvetica", size=7),
    )


def _draw_square_pages(
    pdf: canvas.Canvas,
    slot: SerialEMSlot,
    square: SerialEMSquare,
    image_profile: ImageQualityProfile,
) -> None:
    records = square.records
    chunks = [records[index : index + RECORDS_PER_PAGE] for index in range(0, len(records), RECORDS_PER_PAGE)]
    if not chunks:
        chunks = [()]
    for page_index, chunk in enumerate(chunks, start=1):
        pdf.setPageSize(LANDSCAPE)
        width, height = LANDSCAPE
        title = f"Slot {slot.number} — Square {square.square_id}"
        if len(chunks) > 1:
            title += f" ({page_index}/{len(chunks)})"
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(32, height - 38, title)

        left_x, left_y, left_w, left_h = 32, 64, 292, height - 120
        if page_index == 1 and square.image and square.image.path.is_file():
            _draw_fitted_image(
                pdf,
                square.image.path,
                left_x,
                left_y + 18,
                left_w,
                left_h - 18,
                image_profile=image_profile,
                border=True,
            )
            pdf.setFillColor(SLATE)
            pdf.setFont("Helvetica", 7)
            pdf.drawCentredString(
                left_x + left_w / 2,
                left_y + 4,
                _fit_text_width(square.image.path.name, left_w, font="Helvetica", size=7),
            )
        elif page_index == 1:
            _draw_placeholder(pdf, left_x, left_y + 65, left_w, 230, "Square image not available")
        else:
            pdf.setFillColor(PALE_BLUE)
            pdf.roundRect(left_x, left_y + 65, left_w, 230, 8, fill=1, stroke=0)
            pdf.setFillColor(SLATE)
            pdf.setFont("Helvetica", 10)
            pdf.drawCentredString(left_x + left_w / 2, left_y + 180, "Record images continued")

        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(350, height - 58, f"Record images ({len(records)})")
        record_w, record_h = 198, 158
        for index, assignment in enumerate(chunk):
            column = index % 2
            row = index // 2
            _draw_record(
                pdf,
                assignment.path,
                350 + column * 215,
                height - 88 - (row + 1) * record_h,
                record_w,
                record_h - 10,
                image_profile,
            )
        if not records:
            _draw_placeholder(pdf, 395, 205, 350, 170, "No record images assigned")
        _finish_page(pdf, LANDSCAPE)


def generate_serialem_report(
    output_path: str | Path,
    session: SerialEMSession,
    *,
    generated_at: datetime | None = None,
    progress_callback: ProgressCallback | None = None,
    image_quality: str = DEFAULT_IMAGE_QUALITY,
) -> Path:
    """Generate a report from a fully confirmed SerialEM mapping."""

    validation = validate_serialem_session(session)
    if not validation.valid:
        raise ValueError("SerialEM mapping is incomplete:\n- " + "\n- ".join(validation.errors))
    try:
        image_profile = IMAGE_QUALITY_PROFILES[image_quality]
    except KeyError as exc:
        raise ValueError(f"Unknown image quality {image_quality!r}.") from exc
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    slots = build_serialem_slots(session)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".pdf", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        pdf = canvas.Canvas(str(temporary), pagesize=PORTRAIT, pageCompression=1)
        pdf.setTitle(session.title)
        pdf.setAuthor("ScreeningReport")
        _draw_cover(pdf, session, slots, generated_at or datetime.now())
        for slot in slots:
            _notify(progress_callback, f"Rendering SerialEM slot {slot.number} overview...")
            _draw_overview_page(
                pdf,
                slot,
                slot.primary_overview.path if slot.primary_overview else None,
                image_profile,
            )
            for index, overview in enumerate(slot.supplemental_overviews, start=1):
                _draw_overview_page(
                    pdf,
                    slot,
                    overview.path,
                    image_profile,
                    supplemental_index=index,
                )
            for square in slot.squares:
                _notify(
                    progress_callback,
                    f"Rendering SerialEM slot {slot.number}, square {square.square_id}...",
                )
                _draw_square_pages(pdf, slot, square, image_profile)
        pdf.save()
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _notify(progress_callback, "SerialEM report complete.")
    return output
