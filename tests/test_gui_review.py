import json
from pathlib import Path

from screening_report.gui import ScreeningReportApp
from screening_report.serialem import SerialEMImageAssignment, SerialEMImageRole
from screening_report.theme import bundled_default_theme_path


class _Value:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _Combo:
    def __init__(self) -> None:
        self.values: tuple[str, ...] = ()

    def configure(self, *, values: tuple[str, ...]) -> None:
        self.values = values


class _Root:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.minimum: tuple[int, int] | None = None
        self.requested_geometry: str | None = None

    def update_idletasks(self) -> None:
        pass

    def winfo_screenwidth(self) -> int:
        return self.width

    def winfo_screenheight(self) -> int:
        return self.height

    def minsize(self, width: int, height: int) -> None:
        self.minimum = width, height

    def geometry(self, value: str) -> None:
        self.requested_geometry = value


def _filter_app() -> ScreeningReportApp:
    app = object.__new__(ScreeningReportApp)
    app.serialem_review_only_var = _Value(False)
    app.serialem_filter_slot_var = _Value("All slots")
    app.serialem_filter_role_var = _Value("All roles")
    app.serialem_filter_filename_var = _Value("")
    return app


def test_centers_default_window_on_monitor() -> None:
    app = object.__new__(ScreeningReportApp)
    root = _Root(1920, 1080)
    app.root = root

    app._center_window()

    assert root.minimum == (980, 680)
    assert root.requested_geometry == "1240x800+340+140"


def test_review_filters_combine_slot_role_filename_and_review_state() -> None:
    app = _filter_app()
    assignment = SerialEMImageAssignment(
        path=Path("slot2/slot2_sq1_rec3.jpg"),
        role=SerialEMImageRole.RECORD,
        slot=2,
        square_id="1",
        confirmed=False,
    )

    assert app._serialem_assignment_is_visible(assignment)

    app.serialem_review_only_var.value = True
    app.serialem_filter_slot_var.value = "2"
    app.serialem_filter_role_var.value = "record"
    app.serialem_filter_filename_var.value = "REC3"
    assert app._serialem_assignment_is_visible(assignment)

    assignment.confirmed = True
    assert not app._serialem_assignment_is_visible(assignment)
    assignment.confirmed = False
    app.serialem_filter_slot_var.value = "3"
    assert not app._serialem_assignment_is_visible(assignment)


def test_refresh_theme_choices_discovers_valid_and_reports_invalid(tmp_path: Path) -> None:
    payload = json.loads(bundled_default_theme_path().read_text(encoding="utf-8"))
    payload["name"] = "Facility Blue"
    (tmp_path / "facility.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    app = object.__new__(ScreeningReportApp)
    app.theme_directory = tmp_path
    app._theme_directory_error = None
    app._browsed_theme = None
    app.theme_var = _Value("Default")
    app.status_var = _Value("")
    app.theme_combobox = _Combo()

    app._refresh_theme_choices()

    assert app.theme_combobox.values == ("Default", "Facility Blue (facility.json)")
    assert app.theme_var.get() == "Default"
    assert app._selected_report_theme().name == "Current Look"
    assert app.status_var.get() == "Ignored invalid theme file(s): broken.json"


def test_refresh_theme_choices_keeps_browsed_theme_selected(tmp_path: Path) -> None:
    from screening_report.theme import DEFAULT_REPORT_THEME

    app = object.__new__(ScreeningReportApp)
    app.theme_directory = tmp_path
    app._theme_directory_error = None
    app._browsed_theme = ("Browsed (elsewhere.json)", DEFAULT_REPORT_THEME)
    app.theme_var = _Value("Browsed (elsewhere.json)")
    app.status_var = _Value("")
    app.theme_combobox = _Combo()

    app._refresh_theme_choices()

    assert app.theme_combobox.values == ("Default", "Browsed (elsewhere.json)")
    assert app._selected_report_theme() is DEFAULT_REPORT_THEME
