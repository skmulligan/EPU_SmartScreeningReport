from datetime import datetime
from pathlib import Path

import pdfplumber
from PIL import Image

from screening_report.serialem import (
    SerialEMImageRole,
    SerialEMSlotDetails,
    scan_serialem_session,
)
from screening_report.serialem_pdf_report import generate_serialem_report


def _image(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (180, 140), color).save(path, "JPEG")


def test_generates_adapted_serialem_report_with_partial_square(tmp_path: Path) -> None:
    root = tmp_path / "160230_serialem"
    _image(root / "slot2" / "slot2_LMM_screened.jpg", "gray")
    _image(root / "slot2" / "slot2_LMM_raw.jpg", "black")
    _image(root / "slot2" / "slot2_sq1.jpg", "lightgray")
    _image(root / "slot2" / "slot2_sq1_rec1.jpg", "darkgray")
    _image(root / "slot2" / "slot2_sq3_rec2.jpg", "white")
    session = scan_serialem_session(root)
    session.title = "160230 SerialEM Visual Report"
    session.slot_details[2] = SerialEMSlotDetails("Grid A", "Promising ice")
    for assignment in session.assignments:
        assignment.confirmed = True
    output = tmp_path / "serialem.pdf"

    result = generate_serialem_report(
        output,
        session,
        generated_at=datetime(2026, 7, 31, 10, 30),
        image_quality="email",
    )

    assert result == output
    assert output.read_bytes().startswith(b"%PDF")
    with pdfplumber.open(output) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        assert pdf.pages[0].width < pdf.pages[0].height
        assert any(page.width > page.height for page in pdf.pages)
    assert "160230 SerialEM Visual Report" in text
    assert "Grid A" in text
    assert "Supplemental overview 1" in text
    assert "Square 1" in text
    assert "Square 3" in text
    assert "Square image not available" in text
    assert "slot2_sq1_rec1.jpg" in text


def test_report_rejects_unconfirmed_mapping(tmp_path: Path) -> None:
    root = tmp_path / "session"
    _image(root / "slot2" / "slot2_LMM.jpg", "gray")
    session = scan_serialem_session(root)
    session.assignments[0].confirmed = False

    try:
        generate_serialem_report(tmp_path / "invalid.pdf", session)
    except ValueError as exc:
        assert "mapping is incomplete" in str(exc)
    else:
        raise AssertionError("Expected unconfirmed mapping to be rejected")


def test_report_allows_excluded_unknown_file(tmp_path: Path) -> None:
    root = tmp_path / "session"
    _image(root / "slot2" / "slot2_LMM.jpg", "gray")
    _image(root / "notes.jpg", "white")
    session = scan_serialem_session(root)
    for assignment in session.assignments:
        if assignment.role is None:
            assignment.role = SerialEMImageRole.EXCLUDED
        assignment.confirmed = True

    generate_serialem_report(tmp_path / "excluded.pdf", session)

    assert (tmp_path / "excluded.pdf").is_file()
