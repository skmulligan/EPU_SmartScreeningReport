import re
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from screening_report.discovery import (
    DiscoveryError,
    discover_grid_folders,
    extract_project_number,
)
from screening_report.naming import DEFAULT_NAMING_PROFILE


def _write_jpg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=(40, 80, 120)).save(path, "JPEG")


def test_extract_project_number() -> None:
    assert extract_project_number("160800_example_atlases_20260729") == "160800"
    assert extract_project_number("160801ProjectName") == "160801"


@pytest.mark.parametrize(
    "name",
    ["project_160800", "16080_too_short", "1608000_too_long", "161800_wrong_series"],
)
def test_extract_project_number_rejects_invalid_names(name: str) -> None:
    with pytest.raises(DiscoveryError):
        extract_project_number(name)


def test_discovers_slots_in_numeric_order_and_matches_sample_atlas(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160800_example_atlases_20260729"
    atlas_root.mkdir()
    (tmp_path / f"{atlas_root.name}_Slot10").mkdir()
    (tmp_path / f"{atlas_root.name}_Slot2").mkdir()
    (tmp_path / f"{atlas_root.name}_Slot13").mkdir()
    (tmp_path / f"{atlas_root.name}_Slot2_extra").mkdir()
    (tmp_path / "160800_other_session_Slot3").mkdir()
    atlas_image = atlas_root / "Sample2" / "Atlas" / "Atlas_123.jpg"
    _write_jpg(atlas_image)

    grids = discover_grid_folders(atlas_root)

    assert [grid.slot for grid in grids] == [2, 10]
    assert grids[0].atlas_image == atlas_image
    assert grids[1].atlas_image is None
    assert all(grid.project_number == "160800" for grid in grids)


def test_rejects_missing_atlas_directory(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="does not exist"):
        discover_grid_folders(tmp_path / "160800_missing")


def test_custom_profile_discovers_non_epu_session_names(tmp_path: Path) -> None:
    profile = replace(
        DEFAULT_NAMING_PROFILE,
        name="Example Lab",
        project_pattern=re.compile(r"^(?P<project>LAB\d+)-"),
        slot_directory_pattern=re.compile(
            r"^(?P<session>.+)__position-(?P<slot>\d+)$"
        ),
        atlas_directory_template="Maps/position-{slot}",
        atlas_image_pattern=re.compile(r"^overview-.*\.jpe?g$", re.IGNORECASE),
        maximum_slot=24,
    )
    atlas_root = tmp_path / "LAB42-run"
    atlas_root.mkdir()
    slot_root = tmp_path / "LAB42-run__position-3"
    slot_root.mkdir()
    atlas_image = atlas_root / "Maps" / "position-3" / "overview-1.jpeg"
    _write_jpg(atlas_image)

    grids = discover_grid_folders(atlas_root, profile)

    assert len(grids) == 1
    assert grids[0].slot == 3
    assert grids[0].project_number == "LAB42"
    assert grids[0].atlas_image == atlas_image
