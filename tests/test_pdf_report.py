from datetime import datetime
from pathlib import Path

import pdfplumber
from PIL import Image

from screening_report.discovery import discover_grid_folders
from screening_report.models import (
    AcquisitionMetadata,
    DataImageRecord,
    FoilHoleRecord,
    GridFolder,
    GridSquareRecord,
    SlotContent,
)
from screening_report.pdf_report import (
    DATA_IMAGES_PER_DETAIL_ROW,
    DATA_DETAIL_SIZE,
    FOIL_DETAIL_SIZE,
    _configuration_values,
    _foil_segments,
    generate_basic_report,
)


def _data_xml() -> str:
    return """
    <MicroscopeImage xmlns="urn:fei" xmlns:a="urn:arrays">
      <CustomData>
        <a:KeyValueOfstringanyType>
          <a:Key>BinaryResult.Detector</a:Key><a:Value>EF-Falcon</a:Value>
        </a:KeyValueOfstringanyType>
        <a:KeyValueOfstringanyType>
          <a:Key>DetectorCommercialName</a:Key><a:Value>Falcon 4i</a:Value>
        </a:KeyValueOfstringanyType>
        <a:KeyValueOfstringanyType>
          <a:Key>Detectors[EF-Falcon].TotalDose</a:Key>
          <a:Value>41.6376206913582</a:Value>
        </a:KeyValueOfstringanyType>
      </CustomData>
      <SpatialScale><pixelSize>
        <x><numericValue>1.1739527405740802E-10</numericValue></x>
        <y><numericValue>1.1739527405740802E-10</numericValue></y>
      </pixelSize></SpatialScale>
      <microscopeData>
        <acquisition>
          <acquisitionDateTime>2026-07-29T18:47:48.7130023-07:00</acquisitionDateTime>
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
          <TemMagnification><NominalMagnification>100000</NominalMagnification></TemMagnification>
        </optics>
      </microscopeData>
    </MicroscopeImage>
    """


def test_generates_overview_and_one_page_per_grid(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160230_example_atlases_20260729"
    atlas_root.mkdir()
    (tmp_path / f"{atlas_root.name}_Slot2").mkdir()
    (tmp_path / f"{atlas_root.name}_Slot3").mkdir()
    atlas_image = atlas_root / "Sample2" / "Atlas" / "Atlas_123.jpg"
    atlas_image.parent.mkdir(parents=True)
    Image.new("RGB", (512, 512), color=(75, 105, 135)).save(atlas_image, "JPEG")
    grids = discover_grid_folders(atlas_root)
    output = tmp_path / "report.pdf"

    result = generate_basic_report(
        output,
        atlas_root,
        grids,
        generated_at=datetime(2026, 7, 30, 12, 0),
    )

    assert result == output
    assert output.read_bytes().startswith(b"%PDF")
    with pdfplumber.open(output) as pdf:
        assert len(pdf.pages) == 3
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "160230 Screening Report" in text
    assert "Slot 2" in text
    assert "Slot 3" in text
    assert "GridSquare summary" in text
    assert "No atlas image matched Thermo Fisher EPU" in text


def test_generates_landscape_gridsquare_and_foil_pages(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160230_session"
    atlas_root.mkdir()
    slot = tmp_path / "160230_session_Slot2"
    square = slot / "Images-Disc1" / "GridSquare_10"
    square.mkdir(parents=True)
    Image.new("RGB", (128, 128), "gray").save(
        square / "GridSquare_20260729_120000.jpg",
        "JPEG",
    )
    foil = square / "FoilHoles" / "FoilHole_20_20260729_120100.jpg"
    foil.parent.mkdir()
    Image.new("RGB", (128, 128), "gray").save(foil, "JPEG")
    data = square / "Data" / "FoilHole_20_Data_900_0_20260729_120200.jpg"
    data.parent.mkdir()
    Image.new("RGB", (128, 128), "gray").save(data, "JPEG")
    data.with_suffix(".xml").write_text(_data_xml())
    grids = discover_grid_folders(atlas_root)
    output = tmp_path / "detailed.pdf"

    generate_basic_report(output, atlas_root, grids)

    with pdfplumber.open(output) as pdf:
        assert len(pdf.pages) == 5
        assert pdf.pages[0].width < pdf.pages[0].height
        assert pdf.pages[1].width < pdf.pages[1].height
        assert pdf.pages[2].width > pdf.pages[2].height
        assert pdf.pages[3].width > pdf.pages[3].height
        assert pdf.pages[4].width > pdf.pages[4].height
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "GridSquare 10" in text
    assert "FoilHole 20" in text
    assert "Data acquisition configuration" in text
    assert "Microscope voltage: 200 kV" in text
    assert "Detector: Falcon 4i" in text
    assert "Energy filter: Inserted, 10 eV slit" in text
    assert "Instrument model: GLACIOS-9961023" in text
    assert "EPU software version: 3.16.0.12329" in text
    assert "Mag 100,000x" in text
    assert "Pixel 1.174 Å/px" in text
    assert "Dose 41.64 e-/Å²" in text
    assert "Defocus -2.67 µm" in text
    assert "2026-07-29 18:47:48" in text
    assert "FFT power spectra" in text
    assert "MRC unavailable" in text
    assert "Nyquist 2.348 Å" in text

    without_fft = tmp_path / "without-fft.pdf"
    generate_basic_report(
        without_fft,
        atlas_root,
        grids,
        include_fft=False,
    )
    with pdfplumber.open(without_fft) as pdf:
        assert len(pdf.pages) == 4
        text_without_fft = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )
    assert "FFT power spectra" not in text_without_fft


def test_cover_preserves_double_digit_slot_folder_names(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160230_CL_apoSK04_SS_atlases_20260729"
    atlas_root.mkdir()
    expected_names = []
    for slot in (10, 11, 12):
        folder = tmp_path / f"{atlas_root.name}_Slot{slot}"
        folder.mkdir()
        expected_names.append(folder.name)

    output = tmp_path / "double-digit-slots.pdf"
    generate_basic_report(
        output,
        atlas_root,
        discover_grid_folders(atlas_root),
        generated_at=datetime(2026, 7, 30, 12, 0),
    )

    with pdfplumber.open(output) as pdf:
        cover_text = pdf.pages[0].extract_text() or ""

    for folder_name in expected_names:
        assert folder_name in cover_text


def test_foil_rows_prioritize_large_data_images(tmp_path: Path) -> None:
    data_images = tuple(
        DataImageRecord(
            path=tmp_path / f"data-{index}.jpg",
            xml_path=None,
            foil_id="20",
            acquisition_area_id=str(index),
            acquired_at=None,
        )
        for index in range(1, 8)
    )
    foil = FoilHoleRecord(
        foil_id="20",
        image_path=None,
        xml_path=None,
        acquired_at=None,
        data_images=data_images,
    )

    segments = _foil_segments(foil)

    assert DATA_DETAIL_SIZE == 176
    assert FOIL_DETAIL_SIZE == 82
    assert DATA_IMAGES_PER_DETAIL_ROW == 3
    assert [len(chunk) for _, chunk in segments] == [3, 3, 1]
    assert [continued for continued, _ in segments] == [False, True, True]


def _slot_content_with_metadata(
    tmp_path: Path,
    metadata: tuple[AcquisitionMetadata | None, ...],
) -> SlotContent:
    data_images = tuple(
        DataImageRecord(
            path=tmp_path / f"data-{index}.jpg",
            xml_path=None,
            foil_id="20",
            acquisition_area_id=str(index),
            acquired_at=None,
            metadata=item,
        )
        for index, item in enumerate(metadata)
    )
    foil = FoilHoleRecord(
        foil_id="20",
        image_path=None,
        xml_path=None,
        acquired_at=None,
        data_images=data_images,
    )
    grid = GridFolder(
        slot=2,
        path=tmp_path,
        project_number="160230",
        atlas_image=None,
    )
    square = GridSquareRecord(
        grid_square_id="10",
        path=tmp_path,
        image_path=None,
        xml_path=None,
        acquired_at=None,
        foil_holes=(foil,),
    )
    return SlotContent(grid=grid, grid_squares=(square,))


def test_cover_configuration_aggregates_mixed_and_unavailable_values(
    tmp_path: Path,
) -> None:
    content = _slot_content_with_metadata(
        tmp_path,
        (
            AcquisitionMetadata(
                voltage_kv=200,
                detector="Falcon 4i",
                energy_filter_inserted=True,
                energy_filter_slit_width_ev=10,
            ),
            AcquisitionMetadata(
                voltage_kv=300,
                detector="Falcon 4i",
                energy_filter_inserted=True,
                energy_filter_slit_width_ev=20,
            ),
        ),
    )

    values = dict(_configuration_values((content,)))

    assert values["Microscope voltage"] == "Mixed (200 kV; 300 kV)"
    assert values["Detector"] == "Falcon 4i"
    assert values["Energy filter"] == (
        "Mixed (Inserted, 10 eV slit; Inserted, 20 eV slit)"
    )
    assert values["Instrument model"] == "Unavailable"
    assert values["EPU software version"] == "Unavailable"


def test_email_quality_report_is_smaller_than_high_quality(tmp_path: Path) -> None:
    atlas_root = tmp_path / "160230_quality_test"
    atlas_root.mkdir()
    (tmp_path / f"{atlas_root.name}_Slot2").mkdir()
    atlas_image = atlas_root / "Sample2" / "Atlas" / "Atlas_123.jpg"
    atlas_image.parent.mkdir(parents=True)
    Image.effect_noise((1200, 1200), 64).convert("RGB").save(
        atlas_image,
        "JPEG",
        quality=95,
    )
    grids = discover_grid_folders(atlas_root)
    email_output = tmp_path / "email.pdf"
    high_output = tmp_path / "high.pdf"

    generate_basic_report(
        email_output,
        atlas_root,
        grids,
        image_quality="email",
    )
    generate_basic_report(
        high_output,
        atlas_root,
        grids,
        image_quality="high",
    )

    assert email_output.stat().st_size < high_output.stat().st_size
