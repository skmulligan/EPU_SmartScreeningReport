# ScreeningReport

A Windows-friendly Python application for building PDF summaries of cryo-EM
screening sessions collected with EPU.

The application provides:

- A Tkinter desktop interface.
- Atlas session selection with a native directory browser.
- Automatic discovery of sibling grid folders ending in `_Slot1` through
  `_Slot12`.
- Numeric slot ordering, including sessions where slot 1 is absent.
- Automatic matching of slot N to `SampleN/Atlas/Atlas_*.jpg`.
- A hierarchical PDF containing:
  - A session and slot overview with Data-acquisition microscope configuration.
  - The atlas matched to each slot, marked with numbered acquired GridSquare
    positions from `Atlas.dm`.
  - GridSquare images marked with every screened FoilHole location.
  - Compact FoilHole sections using the latest image for each FoilHole ID.
  - Every matching Data image, with numbered acquisition fields marked on the
    FoilHole image when EPU metadata is available.
  - Optional FFT power-spectrum pages generated from same-stem Data MRC files.
  - Magnification, pixel size, total dose, recorded defocus, and acquisition
    time captions parsed from each Data image's matching EPU XML file.

GridSquares are ordered by acquisition time. GridSquare directories without a
primary JPG remain visible in the slot summary and are skipped in the detailed
pages.

## PDF image quality

The GUI provides three image-quality choices. Compression applies only to the
copies embedded in the PDF; the original EPU session files are never changed.

- **Email - smallest file** is the default and embeds images at 140 DPI with
  stronger JPEG compression.
- **Standard** uses 200 DPI for a balance between detail and file size.
- **High detail - largest file** uses 300 DPI and lighter compression.

Choose the quality before clicking **Generate PDF Report**. Report size still
depends on how many Data images were collected, but downsampling avoids
embedding full-resolution microscope JPGs in small page regions.

## FFT power spectra

The **Include FFT power spectra** option is enabled by default. For each Data
JPG, the report looks for a same-stem `.mrc` file and generates a centered,
log-scaled power spectrum on dedicated landscape pages. The calculation removes
the image mean and applies a Hann window before the two-dimensional FFT.

Spectra are held only in memory while the PDF is generated. The EPU session is
never modified and no derived PNG files are written. A missing, unreadable, or
unsupported MRC produces a placeholder for that acquisition while the rest of
the report continues. Clear the checkbox when a faster report without FFT pages
is preferred.

## Expected directory layout

```text
session-parent/
├── 160230_example_atlases_20260729/
│   ├── Sample2/Atlas/Atlas_12345678.jpg
│   └── Sample3/Atlas/Atlas_23456789.jpg
├── 160230_example_atlases_20260729_Slot2/
│   └── Images-Disc1/GridSquare_.../Data/
│       ├── FoilHole_..._Data_...jpg
│       └── FoilHole_..._Data_...mrc
└── 160230_example_atlases_20260729_Slot3/
    └── Images-Disc1/GridSquare_.../
```

Select `160230_example_atlases_20260729` as the Atlas Directory. The associated
slot folders are found automatically.

## Naming profiles

Directory and filename conventions are represented by a `NamingProfile`.
The built-in `Thermo Fisher EPU` profile preserves the default layout described
above. Discovery functions accept an alternate profile, allowing another lab's
names to be normalized into the same project, slot, GridSquare, FoilHole, and
acquisition-area identifiers used by the report.

Patterns use regular expressions with named groups. Depending on the file type,
profiles provide groups such as `project`, `session`, `slot`,
`grid_square_id`, `foil_id`, and `area_id`. Image patterns may provide either a
`timestamp` group or separate `date` and `time` groups. For example:

```python
import re
from dataclasses import replace

from screening_report import DEFAULT_NAMING_PROFILE

lab_profile = replace(
    DEFAULT_NAMING_PROFILE,
    name="Example Lab",
    grid_directory_pattern=re.compile(
        r"^well-(?P<grid_square_id>\d+)$"
    ),
    foil_image_pattern=re.compile(
        r"^hole-(?P<foil_id>\d+)_(?P<timestamp>\d{14})\.jpg$"
    ),
)
```

Pass the profile to `discover_grid_folders`, `discover_session_content`, or
`generate_screening_report`. Profile definitions are validated when created;
missing required groups raise `NamingProfileError` rather than silently
mis-associating images. A profile-file loader and GUI profile selector are
planned as the next rollout stage.

## Run from source

Python 3.10 or newer is recommended.

On the microscope Windows PC, double-click `Launch_ScreeningReport.bat`. The
launcher uses the existing virtual environment at
`C:\Users\supervisor.GLACIOS-9961023\Documents\sean\.venv`. A command window
remains open while the GUI runs and displays any startup error instead of
closing silently. The launcher recognizes the project and an optional `.venv`
inside `C:\Users\supervisor.GLACIOS-9961023\Documents\sean\SmartScreeningReport`.

When updating the microscope installation, replace the complete
`screening_report` package directory rather than copying individual `.py`
files. The modules are versioned together, and a partial copy can produce
missing-module or incompatible-model errors.

Alternatively, run it manually:

```powershell
python -m pip install -r requirements.txt
python -m screening_report
```

For development in this repository:

```bash
conda activate cryoem
python -m pip install -r requirements-dev.txt
python -m screening_report
```

## Tests

```bash
conda activate cryoem
python -m pytest
```

## Third-party coordinate mapping

The GridSquare/FoilHole coordinate behavior is adapted from
[`mvorlander/EPU_mapper`](https://github.com/mvorlander/EPU_mapper). See
`THIRD_PARTY_NOTICES.md` for the pinned revision and MIT license notice.
