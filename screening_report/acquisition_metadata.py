"""Parse acquisition and microscope settings from EPU Data XML files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

from .models import AcquisitionMetadata


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _first_element(root: ET.Element, name: str) -> ET.Element | None:
    name = name.lower()
    return next(
        (element for element in root.iter() if _local_name(element.tag).lower() == name),
        None,
    )


def _nested_text(
    root: ET.Element,
    parent_name: str,
    child_name: str,
) -> str | None:
    parent_name = parent_name.lower()
    child_name = child_name.lower()
    for parent in root.iter():
        if _local_name(parent.tag).lower() != parent_name:
            continue
        for child in parent.iter():
            if _local_name(child.tag).lower() == child_name:
                value = _text(child)
                if value is not None:
                    return value
    return None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _custom_data(root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in root.iter():
        if _local_name(pair.tag).lower() != "keyvalueofstringanytype":
            continue
        children = {
            _local_name(child.tag).lower(): _text(child)
            for child in pair
        }
        key = children.get("key")
        value = children.get("value")
        if key is not None and value is not None:
            values[key] = value
    return values


def _pixel_sizes(root: ET.Element) -> tuple[float | None, float | None]:
    pixel_size = _first_element(root, "pixelSize")
    if pixel_size is None:
        return None, None

    values: dict[str, float | None] = {}
    for axis in pixel_size:
        axis_name = _local_name(axis.tag).lower()
        numeric_value = next(
            (
                _float(_text(element))
                for element in axis.iter()
                if _local_name(element.tag).lower() == "numericvalue"
            ),
            None,
        )
        values[axis_name] = numeric_value

    metres_to_angstrom = 1e10
    x_value = values.get("x")
    y_value = values.get("y")
    return (
        x_value * metres_to_angstrom if x_value is not None else None,
        y_value * metres_to_angstrom if y_value is not None else None,
    )


def _detector_total_dose(custom_data: dict[str, str]) -> float | None:
    detector_name = custom_data.get("BinaryResult.Detector")
    if detector_name:
        value = _float(
            custom_data.get(f"Detectors[{detector_name}].TotalDose")
        )
        if value is not None:
            return value

    for key, value in custom_data.items():
        if key.startswith("Detectors[") and key.endswith("].TotalDose"):
            parsed = _float(value)
            if parsed is not None:
                return parsed
    return _float(custom_data.get("DoseOnCamera"))


def parse_acquisition_metadata(
    path: str | Path | None,
) -> AcquisitionMetadata | None:
    """Return Data acquisition metadata, or ``None`` for unreadable XML."""

    if path is None:
        return None
    xml_path = Path(path)
    if not xml_path.is_file():
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return None

    custom_data = _custom_data(root)
    detector_name = custom_data.get("BinaryResult.Detector")
    detector = (
        custom_data.get("DetectorCommercialName")
        or (
            custom_data.get(f"Detectors[{detector_name}].CommercialName")
            if detector_name
            else None
        )
        or _nested_text(root, "camera", "Name")
    )
    pixel_size_x, pixel_size_y = _pixel_sizes(root)

    acceleration_voltage = _float(
        _nested_text(root, "gun", "AccelerationVoltage")
    )
    recorded_defocus = _float(_nested_text(root, "optics", "Defocus"))

    return AcquisitionMetadata(
        voltage_kv=(
            acceleration_voltage / 1000
            if acceleration_voltage is not None
            else None
        ),
        detector=detector,
        energy_filter_inserted=_bool(
            _nested_text(root, "EnergyFilter", "EnergySelectionSlitInserted")
        ),
        energy_filter_slit_width_ev=_float(
            _nested_text(root, "EnergyFilter", "EnergySelectionSlitWidth")
        ),
        instrument_model=_nested_text(root, "instrument", "InstrumentModel"),
        epu_software_version=_nested_text(
            root,
            "core",
            "ApplicationSoftwareVersion",
        ),
        magnification=_float(
            _nested_text(root, "TemMagnification", "NominalMagnification")
        ),
        pixel_size_x_angstrom=pixel_size_x,
        pixel_size_y_angstrom=pixel_size_y,
        total_dose_e_per_angstrom2=_detector_total_dose(custom_data),
        recorded_defocus_um=(
            recorded_defocus * 1e6
            if recorded_defocus is not None
            else None
        ),
        acquired_at=_datetime(
            _text(_first_element(root, "acquisitionDateTime"))
        ),
    )
