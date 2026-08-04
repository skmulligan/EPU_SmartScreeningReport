import json
from datetime import datetime
from pathlib import Path

import pdfplumber
import pytest
from PIL import Image

from screening_report.discovery import discover_grid_folders
from screening_report.pdf_report import generate_basic_report
from screening_report.serialem import SerialEMSlotDetails, scan_serialem_session
from screening_report.serialem_pdf_report import generate_serialem_report
from screening_report.theme import (
    DEFAULT_REPORT_THEME,
    ThemeError,
    bundled_default_theme_path,
    default_theme_json,
    discover_report_themes,
    ensure_user_theme_directory,
    load_report_theme,
)


def _payload() -> dict[str, object]:
    return json.loads(bundled_default_theme_path().read_text(encoding="utf-8"))


def _write_theme(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _branded_theme(directory: Path, *, logo_name: str = "logo.png"):
    logo = directory / logo_name
    if logo.suffix.casefold() == ".png":
        Image.new("RGBA", (240, 80), (10, 80, 160, 180)).save(logo)
    else:
        Image.new("RGB", (240, 80), (10, 80, 160)).save(logo, "JPEG")
    payload = _payload()
    payload["name"] = "Laboratory Theme"
    payload["colors"]["primary"] = "#663399"  # type: ignore[index]
    payload["fonts"] = {
        "heading": "Courier-Bold",
        "body": "Courier",
        "bold": "Courier-Bold",
        "italic": "Courier-Oblique",
    }
    payload["branding"] = {"logo": logo.name, "footer_text": "Example Cryo-EM Facility"}
    return load_report_theme(_write_theme(directory / "laboratory.json", payload))


def test_bundled_default_matches_current_report_appearance() -> None:
    theme = DEFAULT_REPORT_THEME

    assert theme.schema_version == 1
    assert theme.colors.primary.hexval() == "0x17324d"
    assert theme.colors.secondary_text.hexval() == "0x475569"
    assert theme.colors.border.hexval() == "0xe2e8f0"
    assert theme.colors.surface.hexval() == "0xf8fafc"
    assert theme.colors.accent.hexval() == "0xc2410c"
    assert theme.fonts.heading == "Helvetica-Bold"
    assert theme.fonts.body == "Helvetica"
    assert theme.branding.logo is None
    assert theme.branding.footer_text is None
    assert json.loads(default_theme_json()) == json.loads(
        bundled_default_theme_path().read_text(encoding="utf-8")
    )


def test_starter_theme_is_created_once_and_not_overwritten(tmp_path: Path) -> None:
    themes = tmp_path / "themes"

    assert ensure_user_theme_directory(themes) == themes
    starter = themes / "current-look.json"
    assert starter.read_bytes() == bundled_default_theme_path().read_bytes()

    starter.write_text("user content", encoding="utf-8")
    ensure_user_theme_directory(themes)
    assert starter.read_text(encoding="utf-8") == "user content"


def test_starter_creation_does_not_require_bundled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "screening_report.theme.bundled_default_theme_path",
        lambda: (_ for _ in ()).throw(FileNotFoundError("frozen executable")),
    )

    ensure_user_theme_directory(tmp_path)

    assert json.loads((tmp_path / "current-look.json").read_text(encoding="utf-8"))[
        "name"
    ] == "Current Look"


def test_discovery_sorts_valid_themes_and_collects_errors(tmp_path: Path) -> None:
    second = _payload()
    second["name"] = "Zulu"
    _write_theme(tmp_path / "z.json", second)
    first = _payload()
    first["name"] = "alpha"
    _write_theme(tmp_path / "a.json", first)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")

    result = discover_report_themes(tmp_path)

    assert [item.label for item in result.themes] == ["alpha (a.json)", "Zulu (z.json)"]
    assert [path.name for path, _error in result.errors] == ["broken.json"]


def test_load_theme_resolves_relative_png_and_absolute_jpeg(tmp_path: Path) -> None:
    relative = _branded_theme(tmp_path)
    assert relative.branding.logo == (tmp_path / "logo.png").resolve()

    jpeg = tmp_path / "absolute.jpg"
    Image.new("RGB", (80, 30), "navy").save(jpeg, "JPEG")
    payload = _payload()
    payload["branding"] = {"logo": str(jpeg.resolve()), "footer_text": None}
    absolute = load_report_theme(_write_theme(tmp_path / "absolute.json", payload))
    assert absolute.branding.logo == jpeg.resolve()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version=2), "schema_version"),
        (lambda value: value.update(schema_version=True), "schema_version"),
        (lambda value: value.update(extra=True), "unknown fields"),
        (lambda value: value["colors"].update(primary="navy"), "#RRGGBB"),
        (lambda value: value["fonts"].update(body="Missing-Font"), "unknown ReportLab font"),
        (lambda value: value["branding"].update(logo="missing.png"), "does not exist"),
        (lambda value: value["fonts"].pop("italic"), "is missing"),
    ],
)
def test_theme_validation_errors_are_specific(tmp_path: Path, mutation, message: str) -> None:
    payload = _payload()
    mutation(payload)

    with pytest.raises(ThemeError, match=message):
        load_report_theme(_write_theme(tmp_path / "invalid.json", payload))


def test_invalid_json_reports_location(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"name": }', encoding="utf-8")

    with pytest.raises(ThemeError, match=r"line 1, column 10"):
        load_report_theme(path)


def test_custom_theme_brands_epu_cover_and_footers(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160230_example_atlases_20260729"
    atlas_root.mkdir()
    (tmp_path / f"{atlas_root.name}_Slot2").mkdir()
    theme = _branded_theme(tmp_path)
    output = tmp_path / "themed-epu.pdf"

    generate_basic_report(
        output,
        atlas_root,
        discover_grid_folders(atlas_root),
        generated_at=datetime(2026, 8, 3, 12, 0),
        theme=theme,
    )

    with pdfplumber.open(output) as pdf:
        assert len(pdf.pages) == 2
        assert all("Example Cryo-EM Facility" in (page.extract_text() or "") for page in pdf.pages)
        assert len(pdf.pages[0].images) >= 2
        assert len(pdf.pages[1].images) >= 1
        assert any(
            tuple(round(component, 3) for component in rect["non_stroking_color"])
            == (0.4, 0.2, 0.6)
            for rect in pdf.pages[0].rects
            if isinstance(rect.get("non_stroking_color"), tuple)
        )


def test_custom_theme_brands_serialem_and_fits_long_title(tmp_path: Path) -> None:
    root = tmp_path / "160230_serialem"
    image = root / "slot2" / "slot2_LMM.jpg"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (180, 140), "gray").save(image, "JPEG")
    session = scan_serialem_session(root)
    session.title = "An exceptionally long SerialEM screening report title " * 5
    session.slot_details[2] = SerialEMSlotDetails("Grid A", "")
    for assignment in session.assignments:
        assignment.confirmed = True
    theme = _branded_theme(tmp_path, logo_name="serialem-logo.jpg")
    output = tmp_path / "themed-serialem.pdf"

    generate_serialem_report(output, session, theme=theme)

    with pdfplumber.open(output) as pdf:
        cover_text = pdf.pages[0].extract_text() or ""
        assert "..." in cover_text
        assert "Example Cryo-EM Facility" in cover_text
        assert all("Example Cryo-EM Facility" in (page.extract_text() or "") for page in pdf.pages)
        assert len(pdf.pages[0].images) >= 2
