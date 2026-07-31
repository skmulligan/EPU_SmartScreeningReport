"""Generate display-ready FFT power spectra from MRC acquisition images."""

from __future__ import annotations

from pathlib import Path

import mrcfile
import numpy as np
from PIL import Image


class FFTGenerationError(ValueError):
    """Raised when an MRC cannot produce a usable two-dimensional spectrum."""


def _two_dimensional_image(data: np.ndarray) -> np.ndarray:
    if not np.issubdtype(data.dtype, np.number):
        raise FFTGenerationError(f"Unsupported MRC data type: {data.dtype}")

    squeezed = np.squeeze(data)
    if squeezed.ndim != 2:
        raise FFTGenerationError(
            "Expected a 2-D acquisition image after removing singleton axes; "
            f"found shape {data.shape}."
        )
    if 0 in squeezed.shape:
        raise FFTGenerationError("The MRC image is empty.")
    return np.array(squeezed, dtype=np.float32, copy=True)


def generate_fft_power_spectrum(path: str | Path) -> Image.Image:
    """Return a centered, logarithmically scaled FFT power-spectrum image."""

    mrc_path = Path(path)
    try:
        with mrcfile.open(mrc_path, mode="r", permissive=True) as opened:
            image = _two_dimensional_image(opened.data)
    except FFTGenerationError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise FFTGenerationError(f"Could not read MRC image: {exc}") from exc

    finite = np.isfinite(image)
    if not finite.any():
        raise FFTGenerationError("The MRC image contains no finite pixels.")
    finite_mean = float(np.mean(image[finite], dtype=np.float64))
    if not finite.all():
        image = image.copy()
        image[~finite] = finite_mean

    image -= finite_mean
    height, width = image.shape
    image *= np.hanning(height).astype(np.float32)[:, None]
    image *= np.hanning(width).astype(np.float32)[None, :]

    transformed = np.fft.fftshift(np.fft.fft2(image))
    power = np.log1p(
        transformed.real * transformed.real
        + transformed.imag * transformed.imag
    )
    del transformed

    low, high = np.percentile(power, (1.0, 99.8))
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        display = np.zeros(power.shape, dtype=np.uint8)
    else:
        scaled = (power - low) * (255.0 / (high - low))
        display = np.clip(scaled, 0, 255).astype(np.uint8)
    return Image.fromarray(display)
