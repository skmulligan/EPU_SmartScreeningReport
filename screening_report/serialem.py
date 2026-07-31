"""Discovery and persistent user mapping for SerialEM screening images."""

from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from PIL import Image, UnidentifiedImageError


SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})
SERIALEM_MANIFEST_VERSION = 1


class SerialEMImageRole(str, Enum):
    """The position an image occupies in a SerialEM screening report."""

    PRIMARY_OVERVIEW = "primary_overview"
    SUPPLEMENTAL_OVERVIEW = "supplemental_overview"
    SQUARE = "square"
    RECORD = "record"
    EXCLUDED = "excluded"


@dataclass(slots=True)
class SerialEMImageAssignment:
    """One discovered image and its user-confirmed report assignment."""

    path: Path
    role: SerialEMImageRole | None = None
    slot: int | None = None
    square_id: str | None = None
    order: int = 0
    confirmed: bool = False
    readable: bool = True
    notices: tuple[str, ...] = ()
    review_reason: str | None = None

    @property
    def included(self) -> bool:
        return self.role is not SerialEMImageRole.EXCLUDED


@dataclass(slots=True)
class SerialEMSlotDetails:
    """Editable descriptive information for one autoloader slot."""

    label: str = ""
    notes: str = ""


@dataclass(slots=True)
class SerialEMSquare:
    """Normalized square image and associated record images."""

    square_id: str
    image: SerialEMImageAssignment | None
    records: tuple[SerialEMImageAssignment, ...]


@dataclass(slots=True)
class SerialEMSlot:
    """Normalized report content for one SerialEM autoloader slot."""

    number: int
    label: str
    notes: str
    primary_overview: SerialEMImageAssignment | None
    supplemental_overviews: tuple[SerialEMImageAssignment, ...]
    squares: tuple[SerialEMSquare, ...]


@dataclass(slots=True)
class SerialEMSession:
    """Editable SerialEM screening import workspace."""

    root: Path
    project_id: str
    title: str
    session_name: str
    assignments: list[SerialEMImageAssignment] = field(default_factory=list)
    slot_details: dict[int, SerialEMSlotDetails] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SerialEMValidationResult:
    """Validation errors and non-blocking notices for a mapping."""

    errors: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


_SLOT_RE = re.compile(r"(?:^|[^a-z0-9])slot[\s_-]*(?P<id>\d{1,2})(?!\d)", re.I)
_SQUARE_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:sq|square)[\s_-]*(?P<id>[a-z0-9]+)", re.I
)
_RECORD_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:rec|record)[\s_-]*(?P<id>[a-z0-9]+)", re.I
)
_PROJECT_RE = re.compile(r"(?<!\d)(160\d{3})(?!\d)")
_NATURAL_RE = re.compile(r"(\d+)")


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(piece) if piece.isdigit() else piece.casefold()
        for piece in _NATURAL_RE.split(value)
    )


def _inspect_image(path: Path) -> tuple[bool, tuple[str, ...]]:
    notices: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                frames = getattr(image, "n_frames", 1)
                if frames > 1:
                    notices.append(
                        f"{path.name}: TIFF contains {frames} frames; only the first will be used."
                    )
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return False, (f"{path.name}: image could not be read ({exc}).",)
    return True, tuple(notices)


def _candidate_paths(paths: Iterable[Path]) -> list[Path]:
    candidates: dict[str, Path] = {}
    for selected in paths:
        selected = selected.expanduser()
        if selected.is_dir():
            found = selected.rglob("*")
        else:
            found = (selected,)
        for path in found:
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
                continue
            key = os.path.normcase(str(path.resolve()))
            candidates.setdefault(key, path.resolve())
    return sorted(candidates.values(), key=lambda path: _natural_key(str(path)))


def _suggest_assignment(path: Path, root: Path, order: int) -> SerialEMImageAssignment:
    try:
        searchable = str(path.relative_to(root))
    except ValueError:
        searchable = str(path)
    slot_match = _SLOT_RE.search(searchable)
    square_match = _SQUARE_RE.search(path.stem)
    slot = int(slot_match.group("id")) if slot_match else None
    square_id = square_match.group("id") if square_match else None
    stem = path.stem.casefold()
    if "lmm" in stem or "montage" in stem or "atlas" in stem:
        role: SerialEMImageRole | None = SerialEMImageRole.SUPPLEMENTAL_OVERVIEW
        square_id = None
    elif _RECORD_RE.search(path.stem):
        role = SerialEMImageRole.RECORD
    elif square_match:
        role = SerialEMImageRole.SQUARE
    else:
        role = None
    readable, notices = _inspect_image(path)
    valid_slot = slot is not None and 1 <= slot <= 12
    complete_role = role is not None and (
        role is not SerialEMImageRole.RECORD or square_id is not None
    )
    confirmed = readable and valid_slot and complete_role and role not in {
        SerialEMImageRole.PRIMARY_OVERVIEW,
        SerialEMImageRole.SUPPLEMENTAL_OVERVIEW,
    }
    if not readable:
        review_reason = "Image is unreadable"
    elif not valid_slot:
        review_reason = "Slot could not be assigned confidently"
    elif role is None:
        review_reason = "Image role is unknown"
    elif role is SerialEMImageRole.RECORD and square_id is None:
        review_reason = "Record needs a square ID"
    elif role in {
        SerialEMImageRole.PRIMARY_OVERVIEW,
        SerialEMImageRole.SUPPLEMENTAL_OVERVIEW,
    }:
        review_reason = "Overview role is being resolved"
    else:
        review_reason = None
    return SerialEMImageAssignment(
        path=path,
        role=role,
        slot=slot,
        square_id=square_id,
        order=order,
        readable=readable,
        notices=notices,
        confirmed=confirmed,
        review_reason=review_reason,
    )


def _select_primary_overviews(
    assignments: list[SerialEMImageAssignment],
    auto_confirm: Iterable[SerialEMImageAssignment],
) -> None:
    eligible = {id(item) for item in auto_confirm}
    by_slot: dict[int, list[SerialEMImageAssignment]] = {}
    for assignment in assignments:
        if (
            assignment.slot is not None
            and assignment.role in {
                SerialEMImageRole.PRIMARY_OVERVIEW,
                SerialEMImageRole.SUPPLEMENTAL_OVERVIEW,
            }
        ):
            by_slot.setdefault(assignment.slot, []).append(assignment)
    for candidates in by_slot.values():
        candidates.sort(
            key=lambda item: (
                0 if "screened" in item.path.stem.casefold() else 1,
                _natural_key(item.path.name),
            )
        )
        existing_primary = [
            item for item in candidates if item.role is SerialEMImageRole.PRIMARY_OVERVIEW
        ]
        screened = [item for item in candidates if "screened" in item.path.stem.casefold()]
        confident_choice = False
        if len(existing_primary) == 1:
            confident_choice = True
        elif len(existing_primary) > 1:
            confident_choice = False
        elif len(screened) == 1:
            screened[0].role = SerialEMImageRole.PRIMARY_OVERVIEW
            confident_choice = True
        elif len(candidates) == 1:
            candidates[0].role = SerialEMImageRole.PRIMARY_OVERVIEW
            confident_choice = True
        else:
            candidates[0].role = SerialEMImageRole.PRIMARY_OVERVIEW

        if confident_choice:
            for item in candidates:
                if id(item) in eligible and item.readable and item.slot is not None:
                    item.confirmed = True
                    item.review_reason = None
        else:
            for item in candidates:
                item.confirmed = False
                item.review_reason = "Overview candidates need primary/supplemental review"


def _demote_assignment_conflicts(assignments: list[SerialEMImageAssignment]) -> None:
    square_groups: dict[tuple[int, str], list[SerialEMImageAssignment]] = {}
    for item in assignments:
        if (
            item.role is SerialEMImageRole.SQUARE
            and item.slot is not None
            and item.square_id
        ):
            square_groups.setdefault((item.slot, item.square_id), []).append(item)
    for (slot, square_id), candidates in square_groups.items():
        if len(candidates) < 2:
            continue
        for item in candidates:
            item.confirmed = False
            item.review_reason = f"Slot {slot}, square {square_id} has multiple square images"


def scan_serialem_session(root: str | Path) -> SerialEMSession:
    """Recursively scan ``root`` and return editable, unconfirmed suggestions."""

    session_root = Path(root).expanduser().resolve()
    if not session_root.is_dir():
        raise ValueError("The selected SerialEM session directory does not exist.")
    project_match = _PROJECT_RE.search(session_root.name)
    project_id = project_match.group(1) if project_match else session_root.name
    assignments = [
        _suggest_assignment(path, session_root, index)
        for index, path in enumerate(_candidate_paths((session_root,)), start=1)
    ]
    _select_primary_overviews(assignments, assignments)
    _demote_assignment_conflicts(assignments)
    slots = sorted({item.slot for item in assignments if item.slot is not None})
    return SerialEMSession(
        root=session_root,
        project_id=project_id,
        title=f"{project_id} Screening Report",
        session_name=session_root.name,
        assignments=assignments,
        slot_details={slot: SerialEMSlotDetails() for slot in slots},
    )


def add_serialem_paths(
    session: SerialEMSession,
    paths: Iterable[str | Path],
) -> list[SerialEMImageAssignment]:
    """Add unique images or recursively scanned folders to ``session``."""

    existing = {os.path.normcase(str(item.path.resolve())) for item in session.assignments}
    added: list[SerialEMImageAssignment] = []
    next_order = max((item.order for item in session.assignments), default=0) + 1
    for path in _candidate_paths(Path(value) for value in paths):
        key = os.path.normcase(str(path.resolve()))
        if key in existing:
            continue
        assignment = _suggest_assignment(path, session.root, next_order)
        session.assignments.append(assignment)
        added.append(assignment)
        existing.add(key)
        next_order += 1
    _select_primary_overviews(session.assignments, added)
    _demote_assignment_conflicts(session.assignments)
    for item in added:
        if item.slot is not None:
            session.slot_details.setdefault(item.slot, SerialEMSlotDetails())
    return added


def build_serialem_slots(session: SerialEMSession) -> tuple[SerialEMSlot, ...]:
    """Build the confirmed hierarchy consumed by the PDF renderer."""

    included = [item for item in session.assignments if item.included]
    slot_numbers = sorted({item.slot for item in included if item.slot is not None})
    slots: list[SerialEMSlot] = []
    for number in slot_numbers:
        slot_items = [item for item in included if item.slot == number]
        primary = next(
            (item for item in slot_items if item.role is SerialEMImageRole.PRIMARY_OVERVIEW),
            None,
        )
        supplemental = tuple(
            sorted(
                (
                    item
                    for item in slot_items
                    if item.role is SerialEMImageRole.SUPPLEMENTAL_OVERVIEW
                ),
                key=lambda item: (item.order, _natural_key(item.path.name)),
            )
        )
        square_ids = sorted(
            {
                item.square_id
                for item in slot_items
                if item.square_id
                and item.role in {SerialEMImageRole.SQUARE, SerialEMImageRole.RECORD}
            },
            key=_natural_key,
        )
        squares: list[SerialEMSquare] = []
        for square_id in square_ids:
            square_items = [item for item in slot_items if item.square_id == square_id]
            image = next(
                (item for item in square_items if item.role is SerialEMImageRole.SQUARE),
                None,
            )
            records = tuple(
                sorted(
                    (
                        item
                        for item in square_items
                        if item.role is SerialEMImageRole.RECORD
                    ),
                    key=lambda item: (item.order, _natural_key(item.path.name)),
                )
            )
            squares.append(SerialEMSquare(square_id, image, records))
        details = session.slot_details.get(number, SerialEMSlotDetails())
        slots.append(
            SerialEMSlot(
                number=number,
                label=details.label,
                notes=details.notes,
                primary_overview=primary,
                supplemental_overviews=supplemental,
                squares=tuple(squares),
            )
        )
    return tuple(slots)


def validate_serialem_session(session: SerialEMSession) -> SerialEMValidationResult:
    """Return blocking mapping errors and non-blocking image notices."""

    errors: list[str] = []
    notices: list[str] = []
    if not session.title.strip():
        errors.append("Report title is required.")
    if not session.project_id.strip():
        errors.append("Project ID is required.")
    included = [item for item in session.assignments if item.included]
    if not included:
        errors.append("At least one image must be included.")
    for item in included:
        notices.extend(item.notices)
        if not item.readable:
            errors.append(f"{item.path.name}: the image is unreadable.")
        if item.role is None:
            errors.append(f"{item.path.name}: choose a role or exclude the file.")
        if item.slot is None or not 1 <= item.slot <= 12:
            errors.append(f"{item.path.name}: choose a slot from 1 through 12.")
        if not item.confirmed:
            errors.append(f"{item.path.name}: confirm or exclude this assignment.")
        if item.role is SerialEMImageRole.RECORD and not (item.square_id or "").strip():
            errors.append(f"{item.path.name}: record images require a square ID.")
    for slot in range(1, 13):
        primaries = [
            item
            for item in included
            if item.slot == slot and item.role is SerialEMImageRole.PRIMARY_OVERVIEW
        ]
        if len(primaries) > 1:
            errors.append(f"Slot {slot}: choose only one primary overview.")
        square_images: dict[str, int] = {}
        for item in included:
            if item.slot == slot and item.role is SerialEMImageRole.SQUARE and item.square_id:
                square_images[item.square_id] = square_images.get(item.square_id, 0) + 1
        for square_id, count in square_images.items():
            if count > 1:
                errors.append(
                    f"Slot {slot}, square {square_id}: choose only one square image."
                )
    return SerialEMValidationResult(tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(notices)))


def _stored_path(path: Path, root: Path) -> dict[str, str]:
    try:
        return {"kind": "relative", "value": str(path.resolve().relative_to(root.resolve()))}
    except ValueError:
        return {"kind": "absolute", "value": str(path.resolve())}


def save_serialem_manifest(session: SerialEMSession, output_path: str | Path) -> Path:
    """Atomically save a versioned SerialEM mapping manifest."""

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SERIALEM_MANIFEST_VERSION,
        "session_root": str(session.root.resolve()),
        "project_id": session.project_id,
        "title": session.title,
        "session_name": session.session_name,
        "slot_details": {str(slot): asdict(details) for slot, details in session.slot_details.items()},
        "assignments": [
            {
                "path": _stored_path(item.path, session.root),
                "role": item.role.value if item.role else None,
                "slot": item.slot,
                "square_id": item.square_id,
                "order": item.order,
                "confirmed": item.confirmed,
            }
            for item in session.assignments
        ],
    }
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".json", dir=output.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def load_serialem_manifest(
    manifest_path: str | Path,
    *,
    relocated_root: str | Path | None = None,
) -> SerialEMSession:
    """Load a mapping, optionally rebasing relative images to a relocated root."""

    manifest = Path(manifest_path).expanduser()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SERIALEM_MANIFEST_VERSION:
        raise ValueError("Unsupported SerialEM manifest schema version.")
    root = Path(relocated_root or payload["session_root"]).expanduser().resolve()
    assignments: list[SerialEMImageAssignment] = []
    for stored in payload.get("assignments", []):
        path_data = stored["path"]
        path = (
            root / path_data["value"]
            if path_data["kind"] == "relative"
            else Path(path_data["value"]).expanduser()
        )
        readable, notices = _inspect_image(path) if path.is_file() else (
            False,
            (f"{path.name}: mapped image is missing.",),
        )
        role_value = stored.get("role")
        assignments.append(
            SerialEMImageAssignment(
                path=path,
                role=SerialEMImageRole(role_value) if role_value else None,
                slot=stored.get("slot"),
                square_id=stored.get("square_id"),
                order=int(stored.get("order", 0)),
                confirmed=bool(stored.get("confirmed")) and readable,
                readable=readable,
                notices=notices,
                review_reason=(None if stored.get("confirmed") and readable else "Saved assignment needs review"),
            )
        )
    details = {
        int(slot): SerialEMSlotDetails(**values)
        for slot, values in payload.get("slot_details", {}).items()
    }
    return SerialEMSession(
        root=root,
        project_id=str(payload.get("project_id", "")),
        title=str(payload.get("title", "")),
        session_name=str(payload.get("session_name", root.name)),
        assignments=assignments,
        slot_details=details,
    )
