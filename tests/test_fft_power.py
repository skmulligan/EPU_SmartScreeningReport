from pathlib import Path

import mrcfile
import numpy as np
import pytest

from screening_report.fft_power import FFTGenerationError, generate_fft_power_spectrum


def _write_mrc(path: Path, data: np.ndarray) -> None:
    with mrcfile.new(path, overwrite=True) as output:
        output.set_data(data)


def test_generates_centered_spectrum_with_expected_sinusoid_peaks(
    tmp_path: Path,
) -> None:
    size = 64
    cycles = 8
    x = np.arange(size, dtype=np.float32)
    image = np.cos(2 * np.pi * cycles * x / size)
    image = np.repeat(image[None, :], size, axis=0).astype(np.float32)
    path = tmp_path / "sinusoid.mrc"
    _write_mrc(path, image)

    spectrum = generate_fft_power_spectrum(path)
    pixels = np.asarray(spectrum)

    assert spectrum.mode == "L"
    assert spectrum.size == (size, size)
    assert pixels[size // 2, size // 2 - cycles] > np.percentile(pixels, 95)
    assert pixels[size // 2, size // 2 + cycles] > np.percentile(pixels, 95)


def test_uniform_and_partially_nonfinite_images_are_supported(tmp_path: Path) -> None:
    uniform_path = tmp_path / "uniform.mrc"
    _write_mrc(uniform_path, np.full((32, 48), 7, dtype=np.float32))

    uniform = np.asarray(generate_fft_power_spectrum(uniform_path))

    assert uniform.shape == (32, 48)
    assert not uniform.any()

    nonfinite_path = tmp_path / "nonfinite.mrc"
    nonfinite = np.arange(32 * 32, dtype=np.float32).reshape(32, 32)
    nonfinite[3, 4] = np.nan
    nonfinite[7, 8] = np.inf
    _write_mrc(nonfinite_path, nonfinite)

    result = np.asarray(generate_fft_power_spectrum(nonfinite_path))

    assert result.shape == (32, 32)
    assert np.isfinite(result).all()


def test_singleton_axes_are_squeezed_but_stacks_are_rejected(tmp_path: Path) -> None:
    singleton_path = tmp_path / "singleton.mrc"
    _write_mrc(singleton_path, np.ones((1, 16, 20), dtype=np.float32))

    singleton = generate_fft_power_spectrum(singleton_path)

    assert singleton.size == (20, 16)

    stack_path = tmp_path / "stack.mrc"
    _write_mrc(stack_path, np.ones((2, 16, 20), dtype=np.float32))

    with pytest.raises(FFTGenerationError, match="Expected a 2-D"):
        generate_fft_power_spectrum(stack_path)


def test_unusable_and_corrupt_mrc_files_raise_clear_errors(tmp_path: Path) -> None:
    nonfinite_path = tmp_path / "all-nonfinite.mrc"
    _write_mrc(nonfinite_path, np.full((16, 16), np.nan, dtype=np.float32))
    with pytest.raises(FFTGenerationError, match="no finite pixels"):
        generate_fft_power_spectrum(nonfinite_path)

    corrupt_path = tmp_path / "corrupt.mrc"
    corrupt_path.write_bytes(b"not an MRC file")
    with pytest.raises(FFTGenerationError, match="Could not read MRC"):
        generate_fft_power_spectrum(corrupt_path)
