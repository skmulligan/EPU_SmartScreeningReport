from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from screening_report.discovery import discover_grid_folders
from screening_report.models import (
    DataImageRecord,
    FoilHoleRecord,
    GridSquareRecord,
)
from screening_report.overlays import (
    choose_pixel_center_transform,
    parse_atlas_positions,
    parse_data_area_shifts,
    parse_dm_refined_target_positions,
    register_pixel_centers_to_foil,
    render_atlas_overlay,
    render_data_overlay,
    render_grid_square_overlay,
)
from screening_report.session_content import discover_session_content


def test_chooses_transform_closest_to_fallback() -> None:
    name, coordinates = choose_pixel_center_transform(
        {"10": (0.25, 0.75)},
        {"10": (25.0, 75.0, True)},
        (100, 100),
    )

    assert name == "identity"
    assert coordinates["10"] == (25.0, 75.0, True)


def test_parses_and_scales_atlas_gridsquare_positions(tmp_path: Path) -> None:
    atlas_image = tmp_path / "Atlas_1.jpg"
    Image.new("RGB", (400, 200), "black").save(atlas_image, "JPEG")
    atlas_metadata = tmp_path / "Atlas.dm"
    atlas_metadata.write_text(
        """
        <Root>
          <AtlasPixelPosition>
            <x>0</x><y>0</y><width>1000</width><height>500</height>
          </AtlasPixelPosition>
          <KeyValuePairOfintNodeXml>
            <key>10</key>
            <value>
              <PositionOnTheAtlas>
                <Center><x>250</x><y>125</y></Center>
              </PositionOnTheAtlas>
            </value>
          </KeyValuePairOfintNodeXml>
        </Root>
        """,
        encoding="utf-8",
    )
    record = GridSquareRecord(
        grid_square_id="10",
        path=tmp_path / "GridSquare_10",
        image_path=None,
        xml_path=None,
        acquired_at=None,
        foil_holes=(),
    )

    centers, reference_size = parse_atlas_positions(atlas_metadata)
    result = render_atlas_overlay(atlas_image, (record,))

    assert centers == {"10": (250.0, 125.0)}
    assert reference_size == (1000.0, 500.0)
    assert len(result.markers) == 1
    assert result.markers[0].x == pytest.approx(100.0)
    assert result.markers[0].y == pytest.approx(50.0)
    assert result.markers[0].in_bounds
    assert result.warning is None


def test_parses_data_shifts_and_marks_field_of_view(tmp_path: Path) -> None:
    session = tmp_path / "EpuSession.dm"
    session.write_text(
        """
        <Root>
          <KeyValuePairOfintDataAcquisitionAreaXml>
            <key>900</key>
            <value><ShiftInPixels><height>-100</height><width>100</width></ShiftInPixels></value>
          </KeyValuePairOfintDataAcquisitionAreaXml>
        </Root>
        """,
        encoding="utf-8",
    )
    foil_image = tmp_path / "FoilHole_20_20260729_120000.jpg"
    Image.new("RGB", (100, 100), "gray").save(foil_image, "JPEG")
    foil_xml = foil_image.with_suffix(".xml")
    foil_xml.write_text(
        """
        <MicroscopeImage>
          <Center><x>500</x><y>500</y></Center>
          <SpatialScale><pixelSize><x><numericValue>1e-9</numericValue></x></pixelSize></SpatialScale>
          <ReadoutArea><width>1000</width><height>1000</height></ReadoutArea>
        </MicroscopeImage>
        """,
        encoding="utf-8",
    )
    data_image = tmp_path / "FoilHole_20_Data_900_0_20260729_120100.jpg"
    Image.new("RGB", (100, 100), "black").save(data_image, "JPEG")
    data_xml = data_image.with_suffix(".xml")
    data_xml.write_text(
        """
        <MicroscopeImage>
          <SpatialScale><pixelSize><x><numericValue>1e-10</numericValue></x></pixelSize></SpatialScale>
          <ReadoutArea><width>100</width><height>100</height></ReadoutArea>
        </MicroscopeImage>
        """,
        encoding="utf-8",
    )
    data_record = DataImageRecord(
        path=data_image,
        xml_path=data_xml,
        foil_id="20",
        acquisition_area_id="900",
        acquired_at=None,
    )
    foil = FoilHoleRecord(
        foil_id="20",
        image_path=foil_image,
        xml_path=foil_xml,
        acquired_at=None,
        data_images=(data_record,),
    )

    shifts = parse_data_area_shifts(session)
    result = render_data_overlay(foil, shifts)

    assert shifts == {"900": (100.0, -100.0)}
    assert len(result.markers) == 1
    assert result.markers[0].x == pytest.approx(60.0)
    assert result.markers[0].y == pytest.approx(40.0)
    assert result.warning is None


def test_parses_refined_target_stage_position_and_flags(tmp_path: Path) -> None:
    metadata = tmp_path / "GridSquare_10.dm"
    metadata.write_text(
        """
        <Root>
          <KeyValuePairOfintTargetLocationXml>
            <key>20</key>
            <value>
              <CorrectedStagePosition><X>9</X><Y>8</Y></CorrectedStagePosition>
              <IsPositionCorrected>true</IsPositionCorrected>
              <IsPositionRefined>true</IsPositionRefined>
              <StagePosition><X>1.25e-6</X><Y>-2.5e-6</Y></StagePosition>
            </value>
          </KeyValuePairOfintTargetLocationXml>
        </Root>
        """,
        encoding="utf-8",
    )

    targets = parse_dm_refined_target_positions(metadata)

    assert targets["20"].stage_x == pytest.approx(1.25e-6)
    assert targets["20"].stage_y == pytest.approx(-2.5e-6)
    assert targets["20"].is_corrected
    assert targets["20"].is_refined


def test_registers_complete_hole_cloud_to_visible_foil_center() -> None:
    image = Image.new("L", (100, 100), 0)
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 28, 72, 72), fill=100)
    coordinates = {
        str(index): (70.0 + index % 3 * 5, 20.0 + index // 3 * 5, True)
        for index in range(9)
    }
    for x, y, _ in coordinates.values():
        draw.ellipse((x - 27, y + 23, x - 23, y + 27), fill=220)

    registered, offset = register_pixel_centers_to_foil(
        image.convert("RGB"),
        coordinates,
    )

    assert offset is not None
    assert offset[0] == pytest.approx(-25.0, abs=2.0)
    assert offset[1] == pytest.approx(25.0, abs=2.0)
    assert len(registered) == len(coordinates)


def test_supplied_session_coordinate_mapping_when_available() -> None:
    example_root = Path("example-screening-session")
    atlas = next(example_root.glob("*_CL_apoSK04_SS_atlases_20260729"), None)
    if atlas is None:
        pytest.skip("Supplied example screening session is not available.")
    if not atlas.is_dir():
        pytest.skip("Supplied example screening session is not available.")
    grid = discover_grid_folders(atlas)[0]
    content = discover_session_content(grid)

    assert [
        (square.grid_square_id, square.has_image)
        for square in content.grid_squares
    ] == [
        ("30606223", True),
        ("30605871", True),
        ("30606121", False),
    ]
    assert sum(square.data_image_count for square in content.grid_squares) == 6
    assert grid.atlas_image is not None
    atlas_overlay = render_atlas_overlay(
        grid.atlas_image,
        content.grid_squares,
    )
    assert len(atlas_overlay.markers) == 3
    assert all(marker.in_bounds for marker in atlas_overlay.markers)
    for square in content.populated_grid_squares:
        overlay = render_grid_square_overlay(square, grid.path / "Metadata")
        assert len(overlay.markers) == 4
        assert all(marker.in_bounds for marker in overlay.markers)
        assert all(marker.detected_x is not None for marker in overlay.markers)
        assert all(marker.refined_x is not None for marker in overlay.markers)
        assert any(
            abs(marker.detected_x - marker.refined_x) > 1
            or abs(marker.detected_y - marker.refined_y) > 1
            for marker in overlay.markers
            if marker.detected_x is not None
            and marker.detected_y is not None
            and marker.refined_x is not None
            and marker.refined_y is not None
        )
