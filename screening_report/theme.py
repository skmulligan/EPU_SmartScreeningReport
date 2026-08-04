"""Load, validate, and discover JSON themes for screening reports."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from PIL import Image
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

THEME_SCHEMA_VERSION = 1
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ThemeError(ValueError):
    """Raised when a report theme cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ThemeColors:
    primary: colors.Color
    text: colors.Color
    secondary_text: colors.Color
    muted_text: colors.Color
    border: colors.Color
    surface: colors.Color
    subtle_surface: colors.Color
    accent: colors.Color
    accent_surface: colors.Color
    accent_border: colors.Color
    inverse_text: colors.Color
    success: colors.Color
    inactive: colors.Color
    dark_surface: colors.Color
    dark_surface_text: colors.Color


@dataclass(frozen=True, slots=True)
class ThemeFonts:
    heading: str
    body: str
    bold: str
    italic: str


@dataclass(frozen=True, slots=True)
class ThemeBranding:
    logo: Path | None
    footer_text: str | None


@dataclass(frozen=True, slots=True)
class ReportTheme:
    schema_version: int
    name: str
    colors: ThemeColors
    fonts: ThemeFonts
    branding: ThemeBranding
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredTheme:
    path: Path
    theme: ReportTheme

    @property
    def label(self) -> str:
        return f"{self.theme.name} ({self.path.name})"


@dataclass(frozen=True, slots=True)
class ThemeDiscovery:
    themes: tuple[DiscoveredTheme, ...]
    errors: tuple[tuple[Path, str], ...]


_ROOT_KEYS = {"schema_version", "name", "colors", "fonts", "branding"}
_COLOR_KEYS = {
    "primary",
    "text",
    "secondary_text",
    "muted_text",
    "border",
    "surface",
    "subtle_surface",
    "accent",
    "accent_surface",
    "accent_border",
    "inverse_text",
    "success",
    "inactive",
    "dark_surface",
    "dark_surface_text",
}
_FONT_KEYS = {"heading", "body", "bold", "italic"}
_BRANDING_KEYS = {"logo", "footer_text"}
_DEFAULT_THEME_JSON = """{
  "schema_version": 1,
  "name": "Current Look",
  "colors": {
    "primary": "#17324D",
    "text": "#000000",
    "secondary_text": "#475569",
    "muted_text": "#64748B",
    "border": "#E2E8F0",
    "surface": "#F8FAFC",
    "subtle_surface": "#F1F5F9",
    "accent": "#C2410C",
    "accent_surface": "#FFF7ED",
    "accent_border": "#FED7AA",
    "inverse_text": "#FFFFFF",
    "success": "#22C55E",
    "inactive": "#94A3B8",
    "dark_surface": "#0F172A",
    "dark_surface_text": "#CBD5E1"
  },
  "fonts": {
    "heading": "Helvetica-Bold",
    "body": "Helvetica",
    "bold": "Helvetica-Bold",
    "italic": "Helvetica-Oblique"
  },
  "branding": {
    "logo": null,
    "footer_text": null
  }
}
"""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ThemeError(f"{field} must be a JSON object.")
    return value


def _validate_keys(payload: dict[str, Any], expected: set[str], field: str) -> None:
    missing = expected - payload.keys()
    unknown = payload.keys() - expected
    if missing:
        raise ThemeError(f"{field} is missing: {', '.join(sorted(missing))}.")
    if unknown:
        raise ThemeError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}.")


def _parse_color(value: Any, field: str) -> colors.Color:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ThemeError(f"{field} must be a color in #RRGGBB format.")
    return colors.HexColor(value)


def _parse_font(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThemeError(f"{field} must be a non-empty ReportLab font name.")
    try:
        pdfmetrics.getFont(value)
    except KeyError as exc:
        raise ThemeError(
            f"{field} names unknown ReportLab font {value!r}; custom font files are not supported."
        ) from exc
    return value


def _parse_branding(payload: dict[str, Any], source: Path | None) -> ThemeBranding:
    _validate_keys(payload, _BRANDING_KEYS, "branding")
    raw_logo = payload["logo"]
    if raw_logo is None:
        logo = None
    elif not isinstance(raw_logo, str) or not raw_logo.strip():
        raise ThemeError("branding.logo must be null or a non-empty path string.")
    else:
        logo = Path(raw_logo).expanduser()
        if not logo.is_absolute():
            if source is None:
                raise ThemeError("An embedded theme cannot use a relative branding.logo path.")
            logo = source.parent / logo
        logo = logo.resolve()
        if not logo.is_file():
            raise ThemeError(f"branding.logo does not exist: {logo}")
        if logo.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
            raise ThemeError("branding.logo must be a PNG or JPEG image.")
        try:
            with Image.open(logo) as image:
                image.verify()
        except Exception as exc:
            raise ThemeError(f"branding.logo is not a readable image: {logo}") from exc

    footer_text = payload["footer_text"]
    if footer_text is not None and not isinstance(footer_text, str):
        raise ThemeError("branding.footer_text must be null or a string.")
    if isinstance(footer_text, str):
        footer_text = footer_text.strip() or None
    return ThemeBranding(logo=logo, footer_text=footer_text)


def _report_theme_from_payload(payload: Any, source: Path | None) -> ReportTheme:
    root = _mapping(payload, "theme")
    _validate_keys(root, _ROOT_KEYS, "theme")
    if (
        not isinstance(root["schema_version"], int)
        or isinstance(root["schema_version"], bool)
        or root["schema_version"] != THEME_SCHEMA_VERSION
    ):
        raise ThemeError(
            f"Unsupported theme schema_version {root['schema_version']!r}; "
            f"expected {THEME_SCHEMA_VERSION}."
        )
    name = root["name"]
    if not isinstance(name, str) or not name.strip():
        raise ThemeError("name must be a non-empty string.")

    color_payload = _mapping(root["colors"], "colors")
    _validate_keys(color_payload, _COLOR_KEYS, "colors")
    parsed_colors = {
        key: _parse_color(color_payload[key], f"colors.{key}")
        for key in _COLOR_KEYS
    }

    font_payload = _mapping(root["fonts"], "fonts")
    _validate_keys(font_payload, _FONT_KEYS, "fonts")
    parsed_fonts = {
        key: _parse_font(font_payload[key], f"fonts.{key}")
        for key in _FONT_KEYS
    }

    return ReportTheme(
        schema_version=THEME_SCHEMA_VERSION,
        name=name.strip(),
        colors=ThemeColors(**parsed_colors),
        fonts=ThemeFonts(**parsed_fonts),
        branding=_parse_branding(_mapping(root["branding"], "branding"), source),
        source_path=source,
    )


def load_report_theme(path: str | Path) -> ReportTheme:
    """Load a strict version-1 report theme from JSON."""

    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ThemeError(f"Could not read theme {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ThemeError(
            f"Invalid JSON in {source.name} at line {exc.lineno}, column {exc.colno}: {exc.msg}."
        ) from exc
    return _report_theme_from_payload(payload, source)


def bundled_default_theme_path() -> Path:
    """Return the filesystem path of the packaged immutable default theme."""

    resource = files("screening_report").joinpath("themes/default.json")
    return Path(str(resource))


def default_theme_json() -> str:
    """Return the immutable default JSON without requiring a package-data file."""

    return _DEFAULT_THEME_JSON


DEFAULT_REPORT_THEME = _report_theme_from_payload(json.loads(_DEFAULT_THEME_JSON), None)


def _color_key(value: Any) -> tuple[float, float, float, float]:
    color = colors.toColor(value)
    return color.red, color.green, color.blue, color.alpha


_DEFAULT_COLOR_ROLES = {
    _color_key(DEFAULT_REPORT_THEME.colors.primary): "primary",
    _color_key(DEFAULT_REPORT_THEME.colors.text): "text",
    _color_key(DEFAULT_REPORT_THEME.colors.secondary_text): "secondary_text",
    _color_key(DEFAULT_REPORT_THEME.colors.muted_text): "muted_text",
    _color_key(DEFAULT_REPORT_THEME.colors.border): "border",
    _color_key(DEFAULT_REPORT_THEME.colors.surface): "surface",
    _color_key(DEFAULT_REPORT_THEME.colors.subtle_surface): "subtle_surface",
    _color_key(DEFAULT_REPORT_THEME.colors.accent): "accent",
    _color_key(DEFAULT_REPORT_THEME.colors.accent_surface): "accent_surface",
    _color_key(DEFAULT_REPORT_THEME.colors.accent_border): "accent_border",
    _color_key(DEFAULT_REPORT_THEME.colors.inverse_text): "inverse_text",
    _color_key(DEFAULT_REPORT_THEME.colors.success): "success",
    _color_key(DEFAULT_REPORT_THEME.colors.inactive): "inactive",
    _color_key(DEFAULT_REPORT_THEME.colors.dark_surface): "dark_surface",
    _color_key(DEFAULT_REPORT_THEME.colors.dark_surface_text): "dark_surface_text",
}


class ReportCanvas(canvas.Canvas):
    """Canvas that maps the legacy report palette and fonts onto a theme."""

    def __init__(self, *args: Any, theme: ReportTheme | None = None, **kwargs: Any) -> None:
        self.report_theme = theme or DEFAULT_REPORT_THEME
        super().__init__(*args, **kwargs)

    def themed_font(self, legacy_font: str, size: float) -> str:
        fonts = self.report_theme.fonts
        if legacy_font == "Helvetica":
            return fonts.body
        if legacy_font == "Helvetica-Oblique":
            return fonts.italic
        if legacy_font == "Helvetica-Bold":
            return fonts.heading if size >= 11 else fonts.bold
        return legacy_font

    def _themed_color(self, value: Any) -> Any:
        try:
            role = _DEFAULT_COLOR_ROLES.get(_color_key(value))
        except Exception:
            return value
        return getattr(self.report_theme.colors, role) if role else value

    def setFillColor(self, aColor: Any, alpha: float | None = None) -> None:  # noqa: N802
        super().setFillColor(self._themed_color(aColor), alpha=alpha)

    def setStrokeColor(self, aColor: Any, alpha: float | None = None) -> None:  # noqa: N802
        super().setStrokeColor(self._themed_color(aColor), alpha=alpha)

    def setFont(self, psfontname: str, size: float, leading: float | None = None) -> None:  # noqa: N802
        super().setFont(self.themed_font(psfontname, size), size, leading)

    def set_marker_fill_color(self, value: Any) -> None:
        """Set a scientific marker color without theme remapping."""

        canvas.Canvas.setFillColor(self, value)

    def set_marker_stroke_color(self, value: Any) -> None:
        """Set a scientific marker outline without theme remapping."""

        canvas.Canvas.setStrokeColor(self, value)


def user_theme_directory() -> Path:
    """Return the platform-native per-user report theme directory."""

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return base / "ScreeningReport" / "themes"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ScreeningReport/themes"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "screening-report" / "themes"


def ensure_user_theme_directory(directory: Path | None = None) -> Path:
    """Create the user theme folder and its non-overwritten starter theme."""

    target = directory or user_theme_directory()
    target.mkdir(parents=True, exist_ok=True)
    starter = target / "current-look.json"
    if not starter.exists():
        starter.write_text(default_theme_json(), encoding="utf-8")
    return target


def discover_report_themes(directory: Path | None = None) -> ThemeDiscovery:
    """Load valid top-level JSON themes and collect invalid-file errors."""

    target = directory or user_theme_directory()
    if not target.is_dir():
        return ThemeDiscovery((), ())
    discovered: list[DiscoveredTheme] = []
    errors: list[tuple[Path, str]] = []
    for path in target.glob("*.json"):
        try:
            discovered.append(DiscoveredTheme(path, load_report_theme(path)))
        except ThemeError as exc:
            errors.append((path, str(exc)))
    discovered.sort(key=lambda item: (item.theme.name.casefold(), item.path.name.casefold()))
    errors.sort(key=lambda item: item[0].name.casefold())
    return ThemeDiscovery(tuple(discovered), tuple(errors))
