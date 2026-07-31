import re
from dataclasses import replace
from pathlib import Path

from PIL import Image

from screening_report.models import GridFolder
from screening_report.naming import DEFAULT_NAMING_PROFILE
from screening_report.session_content import discover_session_content


def _jpg(path: Path, color: tuple[int, int, int] = (80, 100, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=color).save(path, "JPEG")


def test_discovers_hierarchy_and_orders_squares_by_acquisition(tmp_path: Path) -> None:
    slot_path = tmp_path / "160800_session_Slot2"
    early = slot_path / "Images-Disc2" / "GridSquare_200"
    late = slot_path / "Images-Disc1" / "GridSquare_100"
    missing = slot_path / "Images-Disc1" / "GridSquare_300"
    missing.mkdir(parents=True)
    _jpg(early / "GridSquare_20260729_120000.jpg")
    _jpg(late / "GridSquare_20260729_130000.jpg")
    _jpg(early / "FoilHoles" / "FoilHole_20_20260729_120100.jpg")
    latest_foil = early / "FoilHoles" / "FoilHole_20_20260729_120200.jpg"
    _jpg(latest_foil)
    data_image = early / "Data" / "FoilHole_20_Data_900_0_20260729_120300.jpg"
    _jpg(data_image)
    uppercase_mrc = data_image.with_suffix(".MRC")
    uppercase_mrc.write_bytes(b"paired for discovery")

    content = discover_session_content(
        GridFolder(
            slot=2,
            path=slot_path,
            project_number="160800",
            atlas_image=None,
        )
    )

    assert [square.grid_square_id for square in content.grid_squares] == [
        "200",
        "100",
        "300",
    ]
    assert content.grid_squares[0].foil_holes[0].image_path == latest_foil
    data_record = content.grid_squares[0].foil_holes[0].data_images[0]
    assert data_record.path == data_image
    assert data_record.mrc_path is not None
    assert data_record.mrc_path.samefile(uppercase_mrc)
    assert content.grid_squares[2].has_image is False
    assert "detail pages skipped" in content.warnings[0]


def test_missing_data_mrc_is_recorded_as_none(tmp_path: Path) -> None:
    slot_path = tmp_path / "160800_session_Slot2"
    square = slot_path / "Images-Disc1" / "GridSquare_100"
    _jpg(square / "GridSquare_20260729_120000.jpg")
    data_image = square / "Data" / "FoilHole_20_Data_900_0_20260729_120300.jpg"
    _jpg(data_image)

    content = discover_session_content(
        GridFolder(
            slot=2,
            path=slot_path,
            project_number="160800",
            atlas_image=None,
        )
    )

    record = content.grid_squares[0].foil_holes[0].data_images[0]
    assert record.mrc_path is None


def test_custom_profile_associates_images_using_named_groups(tmp_path: Path) -> None:
    profile = replace(
        DEFAULT_NAMING_PROFILE,
        name="Example Lab",
        disc_directory_pattern=re.compile(r"^AcquisitionSet.*$"),
        grid_directory_pattern=re.compile(r"^well-(?P<grid_square_id>\d+)$"),
        grid_image_pattern=re.compile(
            r"^square_(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.jpg$"
        ),
        foil_directory_name="Targets",
        foil_image_pattern=re.compile(
            r"^h(?P<foil_id>\d+)__(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.jpg$"
        ),
        data_directory_name="Movies",
        data_image_pattern=re.compile(
            r"^h(?P<foil_id>\d+)_a(?P<area_id>\d+)__(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.jpg$"
        ),
        timestamp_format="%Y-%m-%dT%H-%M-%S",
    )
    slot_path = tmp_path / "LAB42-run__position-3"
    square = slot_path / "AcquisitionSetA" / "well-7"
    _jpg(square / "square_2026-07-31T09-30-00.jpg")
    _jpg(square / "Targets" / "h55__2026-07-31T09-31-00.jpg")
    data_image = square / "Movies" / "h55_a2__2026-07-31T09-32-00.jpg"
    _jpg(data_image)
    data_image.with_suffix(".XML").write_text("<metadata />", encoding="utf-8")
    data_image.with_suffix(".MRC").write_bytes(b"sidecar")

    content = discover_session_content(
        GridFolder(
            slot=3,
            path=slot_path,
            project_number="LAB42",
            atlas_image=None,
        ),
        profile,
    )

    record = content.grid_squares[0]
    foil = record.foil_holes[0]
    data = foil.data_images[0]
    assert record.grid_square_id == "7"
    assert foil.foil_id == "55"
    assert data.acquisition_area_id == "2"
    assert data.acquired_at is not None
    assert data.xml_path is not None
    assert data.xml_path.samefile(data_image.with_suffix(".XML"))
    assert data.mrc_path is not None
    assert data.mrc_path.samefile(data_image.with_suffix(".MRC"))
