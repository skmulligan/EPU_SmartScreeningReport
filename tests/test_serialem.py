from pathlib import Path

from PIL import Image

from screening_report.serialem import (
    SerialEMImageRole,
    SerialEMSlotDetails,
    add_serialem_paths,
    build_serialem_slots,
    load_serialem_manifest,
    save_serialem_manifest,
    scan_serialem_session,
    validate_serialem_session,
)


EXAMPLE_SESSION = (
    Path(__file__).parents[1]
    / "example-screening-session"
    / "160230_serialem_screening"
)


def _image(path: Path, *, format: str = "JPEG") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), (80, 100, 120)).save(path, format)


def test_example_session_suggestions_match_expected_hierarchy() -> None:
    session = scan_serialem_session(EXAMPLE_SESSION)

    roles = [assignment.role for assignment in session.assignments]
    assert len(session.assignments) == 71
    assert roles.count(SerialEMImageRole.PRIMARY_OVERVIEW) == 11
    assert roles.count(SerialEMImageRole.SUPPLEMENTAL_OVERVIEW) == 3
    assert roles.count(SerialEMImageRole.SQUARE) == 17
    assert roles.count(SerialEMImageRole.RECORD) == 40
    assert sorted(session.slot_details) == list(range(2, 13))
    assert all(assignment.confirmed for assignment in session.assignments)

    slots = build_serialem_slots(session)
    slot4 = next(slot for slot in slots if slot.number == 4)
    square3 = next(square for square in slot4.squares if square.square_id == "3")
    assert square3.image is None
    assert len(square3.records) == 2
    for number in (7, 8):
        slot = next(item for item in slots if item.number == number)
        assert slot.primary_overview is not None
        assert slot.squares == ()


def test_confident_partial_hierarchy_is_auto_confirmed(tmp_path: Path) -> None:
    root = tmp_path / "160999_serialem"
    _image(root / "slot4" / "slot4_sq3_rec1.jpg")
    _image(root / "slot7" / "grid_LMM_slot7.jpg")
    session = scan_serialem_session(root)

    result = validate_serialem_session(session)
    assert result.valid
    assert all(assignment.confirmed for assignment in session.assignments)
    slots = build_serialem_slots(session)
    assert slots[0].squares[0].image is None
    assert slots[1].squares == ()


def test_validation_rejects_ambiguous_and_conflicting_assignments(tmp_path: Path) -> None:
    root = tmp_path / "session"
    _image(root / "slot2" / "unknown.jpg")
    _image(root / "slot2" / "a_LMM.jpg")
    _image(root / "slot2" / "b_LMM.jpg")
    session = scan_serialem_session(root)
    for assignment in session.assignments:
        assignment.confirmed = True
    overviews = [item for item in session.assignments if item.role is not None]
    overviews[1].role = SerialEMImageRole.PRIMARY_OVERVIEW

    result = validate_serialem_session(session)

    assert any("choose a role" in error for error in result.errors)
    assert any("only one primary" in error for error in result.errors)


def test_ambiguous_overviews_and_duplicate_squares_require_review(tmp_path: Path) -> None:
    root = tmp_path / "session"
    _image(root / "slot2" / "a_LMM.jpg")
    _image(root / "slot2" / "b_LMM.jpg")
    _image(root / "slot2" / "slot2_sq1.jpg")
    _image(root / "slot2" / "copy_slot2_sq1.jpg")

    session = scan_serialem_session(root)

    overviews = [
        item
        for item in session.assignments
        if item.role in {
            SerialEMImageRole.PRIMARY_OVERVIEW,
            SerialEMImageRole.SUPPLEMENTAL_OVERVIEW,
        }
    ]
    squares = [item for item in session.assignments if item.role is SerialEMImageRole.SQUARE]
    assert all(not item.confirmed for item in overviews)
    assert all("overview" in (item.review_reason or "").casefold() for item in overviews)
    assert all(not item.confirmed for item in squares)
    assert all("multiple square" in (item.review_reason or "") for item in squares)


def test_add_paths_deduplicates_and_supports_mixed_case_extensions(tmp_path: Path) -> None:
    root = tmp_path / "session"
    _image(root / "slot2" / "slot2_sq1.JPG")
    session = scan_serialem_session(root)
    _image(root / "extra" / "slot2_sq1_rec1.PNG", format="PNG")

    added = add_serialem_paths(session, (root, root / "extra" / "slot2_sq1_rec1.PNG"))

    assert len(added) == 1
    assert len(session.assignments) == 2
    assert added[0].role is SerialEMImageRole.RECORD


def test_unreadable_image_can_be_explicitly_excluded(tmp_path: Path) -> None:
    root = tmp_path / "session"
    bad = root / "slot2" / "slot2_sq1.jpg"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a jpeg")
    session = scan_serialem_session(root)
    assignment = session.assignments[0]
    assignment.confirmed = True

    assert not validate_serialem_session(session).valid
    assignment.role = SerialEMImageRole.EXCLUDED
    assert not validate_serialem_session(session).valid  # no included images remain

    _image(root / "slot2" / "slot2_LMM.jpg")
    add_serialem_paths(session, (root,))
    session.assignments[-1].confirmed = True
    assert validate_serialem_session(session).valid


def test_multiframe_tiff_records_first_frame_notice(tmp_path: Path) -> None:
    root = tmp_path / "session" / "slot2"
    root.mkdir(parents=True)
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    path = root / "slot2_sq1.tiff"
    frames[0].save(path, save_all=True, append_images=frames[1:])

    session = scan_serialem_session(root.parent)

    assert "only the first" in session.assignments[0].notices[0]


def test_manifest_round_trip_and_relocated_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "session"
    image_path = root / "slot2" / "slot2_LMM.jpg"
    _image(image_path)
    session = scan_serialem_session(root)
    session.assignments[0].confirmed = True
    session.title = "SerialEM Test Report"
    session.slot_details[2] = SerialEMSlotDetails("Sample A", "Good ice")
    manifest = tmp_path / "report.serialem.json"

    save_serialem_manifest(session, manifest)
    loaded = load_serialem_manifest(manifest)

    assert loaded.title == "SerialEM Test Report"
    assert loaded.assignments[0].path == image_path.resolve()
    assert loaded.assignments[0].confirmed
    assert loaded.slot_details[2].notes == "Good ice"

    image_path.unlink()
    missing = load_serialem_manifest(manifest)
    assert not missing.assignments[0].readable
    assert not missing.assignments[0].confirmed
    assert "missing" in missing.assignments[0].notices[0]
