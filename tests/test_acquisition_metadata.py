from datetime import timedelta
from pathlib import Path

import pytest

from screening_report.acquisition_metadata import parse_acquisition_metadata


def _xml(*, include_detector_dose: bool = True) -> str:
    dose_pair = (
        """
        <a:KeyValueOfstringanyType>
          <a:Key>Detectors[EF-Falcon].TotalDose</a:Key>
          <a:Value>41.6376206913582</a:Value>
        </a:KeyValueOfstringanyType>
        """
        if include_detector_dose
        else ""
    )
    return f"""
    <MicroscopeImage xmlns="urn:fei"
      xmlns:a="urn:arrays"
      xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
      <CustomData>
        <a:KeyValueOfstringanyType>
          <a:Key>BinaryResult.Detector</a:Key>
          <a:Value>EF-Falcon</a:Value>
        </a:KeyValueOfstringanyType>
        <a:KeyValueOfstringanyType>
          <a:Key>DetectorCommercialName</a:Key>
          <a:Value>Falcon 4i</a:Value>
        </a:KeyValueOfstringanyType>
        {dose_pair}
        <a:KeyValueOfstringanyType>
          <a:Key>DoseOnCamera</a:Key>
          <a:Value>39.5</a:Value>
        </a:KeyValueOfstringanyType>
      </CustomData>
      <SpatialScale>
        <pixelSize>
          <x><numericValue>1.1739527405740802E-10</numericValue></x>
          <y><numericValue>1.1739527405740802E-10</numericValue></y>
        </pixelSize>
      </SpatialScale>
      <microscopeData>
        <acquisition>
          <acquisitionDateTime>2026-07-29T18:47:48.7130023-07:00</acquisitionDateTime>
          <camera><Name>EF-Falcon</Name></camera>
        </acquisition>
        <core><ApplicationSoftwareVersion>3.16.0.12329</ApplicationSoftwareVersion></core>
        <gun><AccelerationVoltage>200000</AccelerationVoltage></gun>
        <instrument><InstrumentModel>GLACIOS-9961023</InstrumentModel></instrument>
        <optics>
          <Defocus>-2.6722127807656159E-06</Defocus>
          <EnergyFilter>
            <EnergySelectionSlitInserted>true</EnergySelectionSlitInserted>
            <EnergySelectionSlitWidth>10</EnergySelectionSlitWidth>
          </EnergyFilter>
          <TemMagnification>
            <NominalMagnification>100000</NominalMagnification>
          </TemMagnification>
        </optics>
      </microscopeData>
    </MicroscopeImage>
    """


def test_parses_namespaced_data_acquisition_xml(tmp_path: Path) -> None:
    xml_path = tmp_path / "data.xml"
    xml_path.write_text(_xml())

    metadata = parse_acquisition_metadata(xml_path)

    assert metadata is not None
    assert metadata.voltage_kv == 200
    assert metadata.detector == "Falcon 4i"
    assert metadata.energy_filter_inserted is True
    assert metadata.energy_filter_slit_width_ev == 10
    assert metadata.instrument_model == "GLACIOS-9961023"
    assert metadata.epu_software_version == "3.16.0.12329"
    assert metadata.magnification == 100000
    assert metadata.pixel_size_x_angstrom == pytest.approx(1.1739527406)
    assert metadata.pixel_size_y_angstrom == pytest.approx(1.1739527406)
    assert metadata.total_dose_e_per_angstrom2 == pytest.approx(41.6376206914)
    assert metadata.recorded_defocus_um == pytest.approx(-2.6722127808)
    assert metadata.acquired_at is not None
    assert metadata.acquired_at.utcoffset() == -timedelta(hours=7)
    assert metadata.acquired_at.microsecond == 713002


def test_falls_back_to_dose_on_camera(tmp_path: Path) -> None:
    xml_path = tmp_path / "fallback.xml"
    xml_path.write_text(_xml(include_detector_dose=False))

    metadata = parse_acquisition_metadata(xml_path)

    assert metadata is not None
    assert metadata.total_dose_e_per_angstrom2 == 39.5


def test_missing_or_malformed_xml_returns_none(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<MicroscopeImage>")

    assert parse_acquisition_metadata(None) is None
    assert parse_acquisition_metadata(tmp_path / "missing.xml") is None
    assert parse_acquisition_metadata(malformed) is None
