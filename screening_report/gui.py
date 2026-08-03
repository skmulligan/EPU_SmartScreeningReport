"""Tkinter desktop interface for EPU and SerialEM screening reports."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import warnings
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import cast

from PIL import Image, ImageTk

from .discovery import DiscoveryError, discover_grid_folders, extract_project_number
from .models import GridFolder
from .naming import DEFAULT_NAMING_PROFILE, NamingProfile
from .pdf_report import (
    DEFAULT_IMAGE_QUALITY,
    IMAGE_QUALITY_PROFILES,
    generate_screening_report,
)
from .serialem import (
    SerialEMImageAssignment,
    SerialEMImageRole,
    SerialEMSession,
    SerialEMSlotDetails,
    add_serialem_paths,
    load_serialem_manifest,
    save_serialem_manifest,
    scan_serialem_session,
    validate_serialem_session,
)
from .serialem_pdf_report import generate_serialem_report
from .theme import (
    DEFAULT_REPORT_THEME,
    ReportTheme,
    ThemeError,
    discover_report_themes,
    ensure_user_theme_directory,
    load_report_theme,
    user_theme_directory,
)


class ScreeningReportApp:
    """Main application window with independent EPU and SerialEM workflows."""

    def __init__(
        self,
        root: tk.Tk,
        naming_profile: NamingProfile = DEFAULT_NAMING_PROFILE,
    ) -> None:
        self.root = root
        self.naming_profile = naming_profile
        self.root.title("CryoEM Screening Report")

        self.atlas_directory: Path | None = None
        self.grids: list[GridFolder] = []
        self.serialem_session: SerialEMSession | None = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_assignment: SerialEMImageAssignment | None = None
        self._preview_resize_job: str | None = None
        self.theme_directory = user_theme_directory()
        self._theme_directory_error: str | None = None
        try:
            ensure_user_theme_directory(self.theme_directory)
        except OSError as exc:
            self._theme_directory_error = str(exc)
        self._themes_by_label: dict[str, ReportTheme] = {
            "Default": DEFAULT_REPORT_THEME
        }
        self._browsed_theme: tuple[str, ReportTheme] | None = None

        self.mode_var = tk.StringVar(value="EPU")
        self.atlas_path_var = tk.StringVar()
        self.serialem_path_var = tk.StringVar()
        self.serialem_project_var = tk.StringVar()
        self.serialem_title_var = tk.StringVar()
        self.serialem_name_var = tk.StringVar()
        self.serialem_role_var = tk.StringVar(value="unassigned")
        self.serialem_slot_var = tk.StringVar()
        self.serialem_square_var = tk.StringVar()
        self.serialem_order_var = tk.StringVar()
        self.serialem_confirmed_var = tk.BooleanVar(value=False)
        self.serialem_slot_label_var = tk.StringVar()
        self.serialem_review_only_var = tk.BooleanVar(value=False)
        self.serialem_filter_slot_var = tk.StringVar(value="All slots")
        self.serialem_filter_role_var = tk.StringVar(value="All roles")
        self.serialem_filter_filename_var = tk.StringVar()
        self.save_manifest_var = tk.BooleanVar(value=True)
        self.image_quality_var = tk.StringVar(
            value=IMAGE_QUALITY_PROFILES[DEFAULT_IMAGE_QUALITY].label
        )
        self.theme_var = tk.StringVar(value="Default")
        self.include_fft_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(
            value="Select an EPU atlas session directory to find associated grid folders."
        )

        self._configure_style()
        self._build_interface()
        self._center_window()

    def _center_window(self) -> None:
        """Size and center the initial window on the current Tk monitor."""

        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1240, max(1, screen_width - 80))
        height = min(800, max(1, screen_height - 100))
        self.root.minsize(min(980, width), min(680, height))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", foreground="#475569")
        style.configure("Treeview", rowheight=27)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Generate.TButton", font=("Segoe UI", 10, "bold"))

    def _build_interface(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(outer, text="CryoEM Screening Report", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        mode_frame = ttk.Frame(outer)
        mode_frame.grid(row=1, column=0, sticky="w", pady=(4, 8))
        ttk.Label(mode_frame, text="Session mode:").grid(row=0, column=0, padx=(0, 8))
        for column, mode in enumerate(("EPU", "SerialEM"), start=1):
            ttk.Radiobutton(
                mode_frame,
                text=mode,
                value=mode,
                variable=self.mode_var,
                command=self._mode_changed,
            ).grid(row=0, column=column, padx=(0, 10))

        self.subtitle_label = ttk.Label(
            outer,
            text=(
                "Select the EPU atlas session. Matching autoloader slot folders "
                "will be loaded automatically."
            ),
            style="Subtitle.TLabel",
        )
        self.subtitle_label.grid(row=2, column=0, sticky="w", pady=(0, 10))

        self.workspace = ttk.Frame(outer)
        self.workspace.grid(row=3, column=0, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self.epu_frame = ttk.Frame(self.workspace)
        self.serialem_frame = ttk.Frame(self.workspace)
        self._build_epu_workspace(self.epu_frame)
        self._build_serialem_workspace(self.serialem_frame)
        self.epu_frame.grid(row=0, column=0, sticky="nsew")

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        quality_frame = ttk.Frame(footer)
        quality_frame.grid(row=0, column=1, padx=12)
        ttk.Label(quality_frame, text="PDF image quality:").grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Combobox(
            quality_frame,
            textvariable=self.image_quality_var,
            values=tuple(profile.label for profile in IMAGE_QUALITY_PROFILES.values()),
            state="readonly",
            width=25,
        ).grid(row=0, column=1)
        self.fft_checkbutton = ttk.Checkbutton(
            quality_frame,
            text="Include FFT power spectra",
            variable=self.include_fft_var,
        )
        self.fft_checkbutton.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.manifest_checkbutton = ttk.Checkbutton(
            quality_frame,
            text="Save reusable SerialEM mapping",
            variable=self.save_manifest_var,
        )
        theme_frame = ttk.Frame(footer)
        theme_frame.grid(
            row=1,
            column=1,
            columnspan=2,
            sticky="e",
            pady=(7, 0),
        )
        ttk.Label(theme_frame, text="Report theme:").grid(
            row=0, column=0, padx=(0, 6)
        )
        self.theme_combobox = ttk.Combobox(
            theme_frame,
            textvariable=self.theme_var,
            values=("Default",),
            state="readonly",
            width=28,
            postcommand=self._refresh_theme_choices,
        )
        self.theme_combobox.grid(row=0, column=1, sticky="ew")
        ttk.Button(theme_frame, text="Browse…", command=self.browse_for_theme).grid(
            row=0, column=2, sticky="ew", padx=(5, 4)
        )
        ttk.Button(
            theme_frame,
            text="Open Themes Folder…",
            command=self.open_themes_folder,
        ).grid(row=0, column=3, sticky="ew")
        self._refresh_theme_choices()
        self.generate_button = ttk.Button(
            footer,
            text="Generate PDF Report...",
            command=self.choose_report_output,
            state="disabled",
            style="Generate.TButton",
        )
        self.generate_button.grid(row=0, column=2, sticky="e")

    def _build_epu_workspace(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        atlas_frame = ttk.LabelFrame(parent, text="Atlas Directory", padding=12)
        atlas_frame.grid(row=0, column=0, sticky="ew")
        atlas_frame.columnconfigure(0, weight=1)
        ttk.Entry(atlas_frame, textvariable=self.atlas_path_var, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            atlas_frame,
            text="Browse for Atlas Directory...",
            command=self.browse_for_atlas,
        ).grid(row=0, column=1, padx=(8, 0))

        grids_frame = ttk.LabelFrame(parent, text="Grid Folders", padding=10)
        grids_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        grids_frame.columnconfigure(0, weight=1)
        grids_frame.rowconfigure(1, weight=1)
        self.grid_summary_label = ttk.Label(
            grids_frame, text="No grid folders loaded", style="Subtitle.TLabel"
        )
        self.grid_summary_label.grid(row=0, column=0, sticky="w", pady=(0, 7))
        container = ttk.Frame(grids_frame)
        container.grid(row=1, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self.grid_tree = ttk.Treeview(
            container,
            columns=("slot", "name", "atlas"),
            show="headings",
            selectmode="browse",
        )
        for key, label, width in (
            ("slot", "Slot", 70),
            ("name", "Grid folder name", 600),
            ("atlas", "Matched atlas", 250),
        ):
            self.grid_tree.heading(key, text=label)
            self.grid_tree.column(key, width=width, anchor="center" if key == "slot" else "w")
        self.grid_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.grid_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_tree.configure(yscrollcommand=scrollbar.set)

    def _build_serialem_workspace(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        source = ttk.LabelFrame(parent, text="SerialEM Session", padding=9)
        source.grid(row=0, column=0, sticky="ew")
        source.columnconfigure(0, weight=1)
        ttk.Entry(source, textvariable=self.serialem_path_var, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(source, text="Scan Session...", command=self.browse_for_serialem).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(source, text="Load Mapping...", command=self.load_serialem_mapping).grid(
            row=0, column=2, padx=(8, 0)
        )

        identity = ttk.LabelFrame(parent, text="Report Information", padding=8)
        identity.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        identity.columnconfigure(1, weight=1)
        identity.columnconfigure(3, weight=1)
        ttk.Label(identity, text="Project ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(identity, textvariable=self.serialem_project_var, width=18).grid(
            row=0, column=1, sticky="ew", padx=(5, 14)
        )
        ttk.Label(identity, text="Session name").grid(row=0, column=2, sticky="w")
        ttk.Entry(identity, textvariable=self.serialem_name_var).grid(
            row=0, column=3, sticky="ew", padx=(5, 0)
        )
        ttk.Label(identity, text="Report title").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(identity, textvariable=self.serialem_title_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(5, 0), pady=(6, 0)
        )

        pane = ttk.Panedwindow(parent, orient="horizontal")
        pane.grid(row=2, column=0, sticky="nsew", pady=(9, 0))
        table_frame = ttk.LabelFrame(pane, text="Image Assignments", padding=7)
        editor_frame = ttk.LabelFrame(pane, text="Preview and Assignment", padding=8)
        pane.add(table_frame, weight=2)
        pane.add(editor_frame, weight=3)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(2, weight=1)

        self.serialem_review_label = ttk.Label(
            table_frame,
            text="No images loaded",
            style="Subtitle.TLabel",
        )
        self.serialem_review_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))

        filters = ttk.Frame(table_frame)
        filters.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        filters.columnconfigure(6, weight=1)
        ttk.Checkbutton(
            filters,
            text="Needs review only",
            variable=self.serialem_review_only_var,
            command=self._refresh_serialem_tree,
        ).grid(row=0, column=0, padx=(0, 10))
        ttk.Label(filters, text="Slot:").grid(row=0, column=1, padx=(0, 4))
        slot_filter = ttk.Combobox(
            filters,
            textvariable=self.serialem_filter_slot_var,
            values=("All slots",) + tuple(str(slot) for slot in range(1, 13)),
            state="readonly",
            width=9,
        )
        slot_filter.grid(row=0, column=2, padx=(0, 10))
        ttk.Label(filters, text="Role:").grid(row=0, column=3, padx=(0, 4))
        role_filter = ttk.Combobox(
            filters,
            textvariable=self.serialem_filter_role_var,
            values=("All roles", "unassigned")
            + tuple(role.value for role in SerialEMImageRole),
            state="readonly",
            width=20,
        )
        role_filter.grid(row=0, column=4, padx=(0, 10))
        ttk.Label(filters, text="Filename:").grid(row=0, column=5, padx=(0, 4))
        ttk.Entry(filters, textvariable=self.serialem_filter_filename_var).grid(
            row=0, column=6, sticky="ew"
        )
        slot_filter.bind("<<ComboboxSelected>>", lambda _event: self._refresh_serialem_tree())
        role_filter.bind("<<ComboboxSelected>>", lambda _event: self._refresh_serialem_tree())
        self.serialem_filter_filename_var.trace_add(
            "write", lambda *_args: self._refresh_serialem_tree()
        )

        self.serialem_tree = ttk.Treeview(
            table_frame,
            columns=("status", "slot", "role", "square", "order", "file"),
            show="headings",
            selectmode="extended",
        )
        definitions = (
            ("status", "Status", 82),
            ("slot", "Slot", 48),
            ("role", "Role", 150),
            ("square", "Square", 65),
            ("order", "Order", 55),
            ("file", "File", 330),
        )
        for key, label, width in definitions:
            self.serialem_tree.heading(key, text=label)
            self.serialem_tree.column(
                key,
                width=width,
                minwidth=40,
                anchor="w" if key in {"role", "file"} else "center",
            )
        self.serialem_tree.tag_configure("review", background="#FFF4CC", foreground="#7C4A03")
        self.serialem_tree.tag_configure("unreadable", background="#FEE2E2", foreground="#991B1B")
        self.serialem_tree.tag_configure("excluded", background="#F1F5F9", foreground="#64748B")
        self.serialem_tree.grid(row=2, column=0, sticky="nsew")
        self.serialem_tree.bind("<<TreeviewSelect>>", self._serialem_selection_changed)
        self._bind_serialem_review_shortcuts()
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.serialem_tree.yview)
        tree_scroll.grid(row=2, column=1, sticky="ns")
        self.serialem_tree.configure(yscrollcommand=tree_scroll.set)
        buttons = ttk.Frame(table_frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        for column, (label, command) in enumerate(
            (
                ("Add Images...", self.add_serialem_images),
                ("Add Folder...", self.add_serialem_folder),
                ("Review Previous", self.select_previous_serialem_review),
                ("Review Next", self.select_next_serialem_review),
                ("Confirm Selected", self.confirm_serialem_selected),
                ("Exclude Selected", self.exclude_serialem_selected),
            )
        ):
            ttk.Button(buttons, text=label, command=command).grid(
                row=0, column=column, padx=(0, 6)
            )
        ttk.Label(
            buttons,
            text="Shortcuts: A/Enter accept  •  X/Delete exclude  •  P/← previous  •  N/→ next",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))

        slot_details = ttk.LabelFrame(table_frame, text="Selected Slot Details", padding=6)
        slot_details.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 0),
        )
        slot_details.columnconfigure(1, weight=1)
        ttk.Label(slot_details, text="Grid ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(slot_details, textvariable=self.serialem_slot_label_var).grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )
        ttk.Label(slot_details, text="Notes").grid(
            row=1, column=0, sticky="nw", pady=(5, 0)
        )
        self.serialem_slot_notes = tk.Text(slot_details, height=2, wrap="word")
        self.serialem_slot_notes.grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=(5, 0)
        )
        ttk.Button(
            slot_details,
            text="Save Slot Details",
            command=self.apply_serialem_slot_details,
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        editor_frame.columnconfigure(0, weight=1)
        self.preview_label = ttk.Label(
            editor_frame,
            text="Select an image to preview",
            anchor="center",
            relief="sunken",
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_label.bind("<Configure>", self._schedule_preview_resize)
        editor_frame.rowconfigure(0, weight=1)
        fields = ttk.Frame(editor_frame)
        fields.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text="Role").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            fields,
            textvariable=self.serialem_role_var,
            values=("unassigned",) + tuple(role.value for role in SerialEMImageRole),
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(fields, text="Slot").grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Combobox(
            fields,
            textvariable=self.serialem_slot_var,
            values=tuple(str(slot) for slot in range(1, 13)),
            state="readonly",
            width=8,
        ).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(5, 0))
        ttk.Label(fields, text="Square ID").grid(row=2, column=0, sticky="w", pady=(5, 0))
        ttk.Entry(fields, textvariable=self.serialem_square_var).grid(
            row=2, column=1, sticky="ew", padx=(6, 0), pady=(5, 0)
        )
        ttk.Label(fields, text="Order").grid(row=3, column=0, sticky="w", pady=(5, 0))
        ttk.Entry(fields, textvariable=self.serialem_order_var, width=10).grid(
            row=3, column=1, sticky="w", padx=(6, 0), pady=(5, 0)
        )
        ttk.Checkbutton(
            fields, text="Assignment confirmed", variable=self.serialem_confirmed_var
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Button(fields, text="Apply to Selected", command=self.apply_serialem_editor).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(7, 0)
        )

    def _mode_changed(self) -> None:
        if self.mode_var.get() == "SerialEM":
            self.epu_frame.grid_remove()
            self.serialem_frame.grid(row=0, column=0, sticky="nsew")
            self.fft_checkbutton.grid_remove()
            self.manifest_checkbutton.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            self.subtitle_label.configure(
                text="Scan display-ready SerialEM images, review every suggested assignment, then generate the report."
            )
            self.status_var.set(
                "Select a SerialEM session directory or load a saved mapping."
                if self.serialem_session is None
                else f"Loaded {len(self.serialem_session.assignments)} SerialEM image(s)."
            )
        else:
            self.serialem_frame.grid_remove()
            self.epu_frame.grid(row=0, column=0, sticky="nsew")
            self.manifest_checkbutton.grid_remove()
            self.fft_checkbutton.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            self.subtitle_label.configure(
                text="Select the EPU atlas session. Matching autoloader slot folders will be loaded automatically."
            )
            self.status_var.set(
                f"Loaded {len(self.grids)} EPU grid folder(s)."
                if self.grids
                else "Select an EPU atlas session directory to find associated grid folders."
            )
        self._update_generate_state()

    def _update_generate_state(self) -> None:
        ready = bool(self.serialem_session) if self.mode_var.get() == "SerialEM" else bool(self.grids)
        self.generate_button.configure(state="normal" if ready else "disabled")

    def browse_for_atlas(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="Select EPU atlas session directory", mustexist=True
        )
        if selected:
            self.load_atlas_directory(Path(selected))

    def load_atlas_directory(self, atlas_directory: Path) -> None:
        try:
            grids = discover_grid_folders(atlas_directory, self.naming_profile)
        except DiscoveryError as exc:
            messagebox.showerror("Invalid atlas directory", str(exc), parent=self.root)
            return
        self.atlas_directory = atlas_directory
        self.grids = grids
        self.atlas_path_var.set(str(atlas_directory))
        self._refresh_grid_tree()
        if grids:
            atlas_count = sum(grid.atlas_image is not None for grid in grids)
            self.status_var.set(
                f"Loaded {len(grids)} grid folder(s); matched {atlas_count} atlas image(s)."
            )
        else:
            self.status_var.set("No sibling folders ending in _Slot1 through _Slot12 were found.")
        self._update_generate_state()

    def _refresh_grid_tree(self) -> None:
        for item in self.grid_tree.get_children():
            self.grid_tree.delete(item)
        for grid in self.grids:
            self.grid_tree.insert(
                "", "end", iid=str(grid.slot), values=(grid.slot, grid.name, grid.atlas_status)
            )
        count = len(self.grids)
        self.grid_summary_label.configure(text=f"{count} grid {'folder' if count == 1 else 'folders'} loaded")

    def browse_for_serialem(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="Select SerialEM screening session", mustexist=True
        )
        if not selected:
            return
        try:
            session = scan_serialem_session(selected)
        except Exception as exc:
            messagebox.showerror("Could not scan SerialEM session", str(exc), parent=self.root)
            return
        self._load_serialem_session(session)

    def _load_serialem_session(self, session: SerialEMSession) -> None:
        self.serialem_session = session
        self.serialem_path_var.set(str(session.root))
        self.serialem_project_var.set(session.project_id)
        self.serialem_title_var.set(session.title)
        self.serialem_name_var.set(session.session_name)
        self._refresh_serialem_tree()
        confirmed = sum(
            item.confirmed
            for item in session.assignments
            if item.role is not SerialEMImageRole.EXCLUDED
        )
        review = sum(
            not item.confirmed
            for item in session.assignments
            if item.role is not SerialEMImageRole.EXCLUDED
        )
        self.status_var.set(
            f"Loaded {len(session.assignments)} image(s); {confirmed} auto-confirmed, {review} need review."
        )
        self._update_generate_state()

    def load_serialem_mapping(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Load SerialEM mapping",
            filetypes=[("SerialEM mappings", "*.serialem.json"), ("JSON files", "*.json")],
        )
        if not selected:
            return
        try:
            session = load_serialem_manifest(selected)
            if not session.root.is_dir():
                relocated = filedialog.askdirectory(
                    parent=self.root,
                    title="The saved session root is unavailable; locate the SerialEM session",
                    mustexist=True,
                )
                if relocated:
                    session = load_serialem_manifest(selected, relocated_root=relocated)
        except Exception as exc:
            messagebox.showerror("Could not load mapping", str(exc), parent=self.root)
            return
        self._load_serialem_session(session)

    def add_serialem_images(self) -> None:
        if self.serialem_session is None:
            messagebox.showerror("No session", "Scan a SerialEM session first.", parent=self.root)
            return
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="Add SerialEM images",
            filetypes=[("Supported images", "*.jpg *.jpeg *.png *.tif *.tiff"), ("All files", "*.*")],
        )
        if selected:
            added = add_serialem_paths(self.serialem_session, selected)
            self._refresh_serialem_tree()
            self.status_var.set(f"Added {len(added)} new image(s).")

    def add_serialem_folder(self) -> None:
        if self.serialem_session is None:
            messagebox.showerror("No session", "Scan a SerialEM session first.", parent=self.root)
            return
        selected = filedialog.askdirectory(parent=self.root, title="Add SerialEM image folder", mustexist=True)
        if selected:
            added = add_serialem_paths(self.serialem_session, (selected,))
            self._refresh_serialem_tree()
            self.status_var.set(f"Added {len(added)} new image(s).")

    def _assignment_for_iid(self, iid: str) -> SerialEMImageAssignment:
        assert self.serialem_session is not None
        return self.serialem_session.assignments[int(iid)]

    def _bind_serialem_review_shortcuts(self) -> None:
        """Bind review actions while the assignment table has keyboard focus."""

        for sequence in ("<KeyPress-a>", "<KeyPress-A>", "<Return>"):
            self.serialem_tree.bind(sequence, self._confirm_serialem_shortcut)
        for sequence in ("<KeyPress-x>", "<KeyPress-X>", "<Delete>"):
            self.serialem_tree.bind(sequence, self._exclude_serialem_shortcut)
        for sequence in ("<KeyPress-n>", "<KeyPress-N>", "<Right>"):
            self.serialem_tree.bind(sequence, self._next_serialem_shortcut)
        for sequence in ("<KeyPress-p>", "<KeyPress-P>", "<Left>"):
            self.serialem_tree.bind(sequence, self._previous_serialem_shortcut)

    def _confirm_serialem_shortcut(self, _event: object) -> str:
        self.confirm_serialem_selected()
        return "break"

    def _exclude_serialem_shortcut(self, _event: object) -> str:
        self.exclude_serialem_selected()
        return "break"

    def _next_serialem_shortcut(self, _event: object) -> str:
        self.select_next_serialem_review()
        return "break"

    def _previous_serialem_shortcut(self, _event: object) -> str:
        self.select_previous_serialem_review()
        return "break"

    def _serialem_assignment_is_visible(
        self,
        assignment: SerialEMImageAssignment,
    ) -> bool:
        needs_review = (
            assignment.role is not SerialEMImageRole.EXCLUDED
            and (not assignment.confirmed or not assignment.readable)
        )
        if self.serialem_review_only_var.get() and not needs_review:
            return False
        slot_filter = self.serialem_filter_slot_var.get()
        if slot_filter != "All slots" and assignment.slot != int(slot_filter):
            return False
        role_filter = self.serialem_filter_role_var.get()
        assignment_role = assignment.role.value if assignment.role else "unassigned"
        if role_filter != "All roles" and assignment_role != role_filter:
            return False
        filename_filter = self.serialem_filter_filename_var.get().strip().casefold()
        if filename_filter and filename_filter not in str(assignment.path).casefold():
            return False
        return True

    def _refresh_serialem_tree(self) -> None:
        for iid in self.serialem_tree.get_children():
            self.serialem_tree.delete(iid)
        if self.serialem_session is None:
            self.serialem_review_label.configure(text="No images loaded")
            return
        confirmed_count = 0
        review_count = 0
        excluded_count = 0
        visible_count = 0
        for index, assignment in enumerate(self.serialem_session.assignments):
            if assignment.role is SerialEMImageRole.EXCLUDED:
                status = "Excluded"
                tag = "excluded"
                excluded_count += 1
            elif not assignment.readable:
                status = "Unreadable"
                tag = "unreadable"
                review_count += 1
            else:
                status = "Confirmed" if assignment.confirmed else "Needs review"
                tag = "" if assignment.confirmed else "review"
                if assignment.confirmed:
                    confirmed_count += 1
                else:
                    review_count += 1
            if not self._serialem_assignment_is_visible(assignment):
                continue
            visible_count += 1
            try:
                filename = str(assignment.path.relative_to(self.serialem_session.root))
            except ValueError:
                filename = str(assignment.path)
            self.serialem_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    status,
                    assignment.slot or "",
                    assignment.role.value if assignment.role else "unassigned",
                    assignment.square_id or "",
                    assignment.order,
                    filename,
                ),
                tags=(tag,) if tag else (),
            )
        self.serialem_review_label.configure(
            text=(
                f"{confirmed_count} of {confirmed_count + review_count} included items reviewed"
                f"  •  {review_count} need review  •  {excluded_count} excluded"
                f"  •  {visible_count} shown"
            )
        )

    def _serialem_selection_changed(self, _event: object | None = None) -> None:
        selection = self.serialem_tree.selection()
        if not selection or self.serialem_session is None:
            return
        assignment = self._assignment_for_iid(selection[0])
        self.serialem_role_var.set(assignment.role.value if assignment.role else "unassigned")
        self.serialem_slot_var.set(str(assignment.slot) if assignment.slot else "")
        self.serialem_square_var.set(assignment.square_id or "")
        self.serialem_order_var.set(str(assignment.order))
        self.serialem_confirmed_var.set(assignment.confirmed)
        self._load_slot_details(assignment.slot)
        self._show_serialem_preview(assignment)

    def _show_serialem_preview(self, assignment: SerialEMImageAssignment) -> None:
        self._preview_assignment = assignment
        self._render_serialem_preview()

    def _schedule_preview_resize(self, _event: object | None = None) -> None:
        """Debounce preview regeneration while the window is being resized."""

        if self._preview_assignment is None:
            return
        if self._preview_resize_job is not None:
            try:
                self.root.after_cancel(self._preview_resize_job)
            except tk.TclError:
                pass
        self._preview_resize_job = self.root.after(120, self._render_serialem_preview)

    def _render_serialem_preview(self) -> None:
        """Render the selected image to the preview panel's current dimensions."""

        self._preview_resize_job = None
        assignment = self._preview_assignment
        if assignment is None:
            return
        available_width = self.preview_label.winfo_width()
        available_height = self.preview_label.winfo_height()
        if available_width <= 10 or available_height <= 10:
            target_size = (680, 480)
        else:
            target_size = (
                max(100, available_width - 20),
                max(100, available_height - 70),
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(assignment.path) as opened:
                    opened.seek(0)
                    original_size = opened.size
                    opened.draft("RGB", target_size)
                    preview = opened.convert("RGB")
            preview.thumbnail(target_size, Image.Resampling.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(preview)
            self.preview_label.configure(
                image=self._preview_photo,
                text=(
                    f"{assignment.path.name}\n{original_size[0]} × {original_size[1]}"
                    + (f"\nReview: {assignment.review_reason}" if assignment.review_reason else "")
                ),
                compound="top",
            )
        except Exception as exc:
            self._preview_photo = None
            self.preview_label.configure(image="", text=f"Preview unavailable\n{exc}")

    def _load_slot_details(self, slot: int | None) -> None:
        details = (
            self.serialem_session.slot_details.get(slot, SerialEMSlotDetails())
            if self.serialem_session and slot is not None
            else SerialEMSlotDetails()
        )
        self.serialem_slot_label_var.set(details.label)
        self.serialem_slot_notes.delete("1.0", "end")
        self.serialem_slot_notes.insert("1.0", details.notes)

    def apply_serialem_editor(self) -> None:
        if self.serialem_session is None:
            return
        selection = self.serialem_tree.selection()
        if not selection:
            return
        role_text = self.serialem_role_var.get()
        role = None if role_text == "unassigned" else SerialEMImageRole(role_text)
        slot = int(self.serialem_slot_var.get()) if self.serialem_slot_var.get() else None
        try:
            order = int(self.serialem_order_var.get())
        except ValueError:
            messagebox.showerror("Invalid order", "Order must be a whole number.", parent=self.root)
            return
        for iid in selection:
            assignment = self._assignment_for_iid(iid)
            assignment.role = role
            assignment.slot = slot
            assignment.square_id = self.serialem_square_var.get().strip() or None
            assignment.order = order
            assignment.confirmed = self.serialem_confirmed_var.get()
            assignment.review_reason = (
                None if assignment.confirmed else "Assignment has not been confirmed"
            )
        if slot is not None:
            self.serialem_session.slot_details.setdefault(slot, SerialEMSlotDetails())
        self._refresh_serialem_tree()
        self.status_var.set(f"Updated {len(selection)} assignment(s).")
        self.select_next_serialem_review()

    def confirm_serialem_selected(self) -> None:
        if self.serialem_session is None:
            return
        selection = self.serialem_tree.selection()
        for iid in selection:
            assignment = self._assignment_for_iid(iid)
            if assignment.role is not SerialEMImageRole.EXCLUDED:
                assignment.confirmed = True
                assignment.review_reason = None
        self._refresh_serialem_tree()
        self.status_var.set(f"Confirmed {len(selection)} assignment(s).")
        self.select_next_serialem_review()

    def exclude_serialem_selected(self) -> None:
        if self.serialem_session is None:
            return
        selection = self.serialem_tree.selection()
        for iid in selection:
            assignment = self._assignment_for_iid(iid)
            assignment.role = SerialEMImageRole.EXCLUDED
            assignment.confirmed = True
            assignment.review_reason = None
        self._refresh_serialem_tree()
        self.status_var.set(f"Excluded {len(selection)} image(s).")
        self.select_next_serialem_review()

    def select_next_serialem_review(self) -> None:
        """Focus the next visible assignment requiring user attention."""

        self._select_serialem_review(direction=1)

    def select_previous_serialem_review(self) -> None:
        """Focus the previous visible assignment requiring user attention."""

        self._select_serialem_review(direction=-1)

    def _select_serialem_review(self, *, direction: int) -> None:
        """Move through the filtered review queue in either direction."""

        if self.serialem_session is None:
            return
        candidates = [
            iid
            for iid in self.serialem_tree.get_children()
            if (
                (assignment := self._assignment_for_iid(iid)).role
                is not SerialEMImageRole.EXCLUDED
                and (not assignment.confirmed or not assignment.readable)
            )
        ]
        if not candidates:
            self.serialem_tree.selection_remove(*self.serialem_tree.selection())
            self.status_var.set("No images in the current filters need review.")
            return
        current = self.serialem_tree.focus()
        if current in candidates:
            index = (candidates.index(current) + direction) % len(candidates)
            iid = candidates[index]
        elif current:
            current_index = int(current)
            if direction > 0:
                iid = next(
                    (candidate for candidate in candidates if int(candidate) > current_index),
                    candidates[0],
                )
            else:
                iid = next(
                    (
                        candidate
                        for candidate in reversed(candidates)
                        if int(candidate) < current_index
                    ),
                    candidates[-1],
                )
        else:
            iid = candidates[0] if direction > 0 else candidates[-1]
        self.serialem_tree.selection_set(iid)
        self.serialem_tree.focus(iid)
        self.serialem_tree.see(iid)
        self._serialem_selection_changed()

    def apply_serialem_slot_details(self) -> None:
        if self.serialem_session is None or not self.serialem_slot_var.get():
            return
        slot = int(self.serialem_slot_var.get())
        self.serialem_session.slot_details[slot] = SerialEMSlotDetails(
            label=self.serialem_slot_label_var.get().strip(),
            notes=self.serialem_slot_notes.get("1.0", "end-1c").strip(),
        )
        self.status_var.set(f"Saved details for slot {slot}.")

    def _sync_serialem_fields(self) -> SerialEMSession:
        assert self.serialem_session is not None
        self.serialem_session.project_id = self.serialem_project_var.get().strip()
        self.serialem_session.title = self.serialem_title_var.get().strip()
        self.serialem_session.session_name = self.serialem_name_var.get().strip()
        return self.serialem_session

    def _quality_key(self) -> str:
        return next(
            key
            for key, profile in IMAGE_QUALITY_PROFILES.items()
            if profile.label == self.image_quality_var.get()
        )

    def _refresh_theme_choices(self) -> None:
        themes: dict[str, ReportTheme] = {"Default": DEFAULT_REPORT_THEME}
        try:
            discovery = discover_report_themes(self.theme_directory)
        except OSError as exc:
            self._theme_directory_error = str(exc)
            discovery = None
        if discovery is not None:
            for entry in discovery.themes:
                themes[entry.label] = entry.theme
            if discovery.errors:
                filenames = ", ".join(path.name for path, _error in discovery.errors)
                self.status_var.set(f"Ignored invalid theme file(s): {filenames}")
        if self._browsed_theme is not None:
            label, browsed = self._browsed_theme
            themes[label] = browsed
        self._themes_by_label = themes
        self.theme_combobox.configure(values=tuple(themes))
        if self.theme_var.get() not in themes:
            self.theme_var.set("Default")
        if self._theme_directory_error:
            self.status_var.set(
                f"Could not initialize the themes folder: {self._theme_directory_error}"
            )

    def _selected_report_theme(self) -> ReportTheme:
        return self._themes_by_label.get(self.theme_var.get(), DEFAULT_REPORT_THEME)

    def browse_for_theme(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select report theme",
            initialdir=str(self.theme_directory),
            filetypes=[("JSON theme files", "*.json"), ("All files", "*.*")],
        )
        if not selected:
            return
        path = Path(selected)
        try:
            theme = load_report_theme(path)
        except ThemeError as exc:
            messagebox.showerror("Invalid report theme", str(exc), parent=self.root)
            return
        label = f"{theme.name} ({path.name})"
        self._browsed_theme = label, theme
        self._refresh_theme_choices()
        self.theme_var.set(label)
        self.status_var.set(f"Loaded report theme: {theme.name}")

    def open_themes_folder(self) -> None:
        try:
            directory = ensure_user_theme_directory(self.theme_directory)
            self._theme_directory_error = None
            self._refresh_theme_choices()
            if sys.platform == "win32":
                getattr(os, "startfile")(str(directory))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(directory)])
            else:
                subprocess.Popen(["xdg-open", str(directory)])
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror(
                "Could not open themes folder",
                f"{self.theme_directory}\n\n{exc}",
                parent=self.root,
            )

    def choose_report_output(self) -> None:
        if self.mode_var.get() == "SerialEM":
            self._choose_serialem_report_output()
        else:
            self._choose_epu_report_output()

    def _choose_epu_report_output(self) -> None:
        if not self.atlas_directory or not self.grids:
            messagebox.showerror(
                "No grids loaded",
                "Select an atlas directory with associated slot folders first.",
                parent=self.root,
            )
            return
        project_number = extract_project_number(self.atlas_directory.name, self.naming_profile)
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save screening report",
            initialdir=str(self.atlas_directory.parent),
            initialfile=f"{project_number}_Screening_Report.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF documents", "*.pdf")],
        )
        if selected:
            self._generate_epu_report(Path(selected))

    def _choose_serialem_report_output(self) -> None:
        if self.serialem_session is None:
            return
        session = self._sync_serialem_fields()
        validation = validate_serialem_session(session)
        if not validation.valid:
            displayed = "\n".join(f"• {error}" for error in validation.errors[:12])
            if len(validation.errors) > 12:
                displayed += f"\n• …and {len(validation.errors) - 12} more"
            messagebox.showerror(
                "SerialEM mapping needs review",
                "Resolve these items before generating:\n\n" + displayed,
                parent=self.root,
            )
            return
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save SerialEM screening report",
            initialdir=str(session.root.parent),
            initialfile=f"{session.project_id}_SerialEM_Screening_Report.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF documents", "*.pdf")],
        )
        if selected:
            self._generate_serialem_report(Path(selected), session)

    def _start_worker(
        self,
        worker: Callable[[queue.Queue[tuple[str, object]]], Path],
    ) -> None:
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.generate_button.configure(state="disabled")
        self.status_var.set("Generating PDF report...")
        self.root.configure(cursor="watch")

        def run() -> None:
            try:
                result = worker(result_queue)
            except Exception as exc:
                result_queue.put(("error", exc))
            else:
                result_queue.put(("success", result))

        threading.Thread(target=run, daemon=True).start()
        self.root.after(100, self._poll_report_result, result_queue)

    def _generate_epu_report(self, output_path: Path) -> None:
        assert self.atlas_directory is not None
        atlas_directory = self.atlas_directory
        grids = list(self.grids)
        quality = self._quality_key()
        include_fft = self.include_fft_var.get()
        theme = self._selected_report_theme()

        def worker(result_queue: queue.Queue[tuple[str, object]]) -> Path:
            return generate_screening_report(
                output_path,
                atlas_directory,
                grids,
                image_quality=quality,
                include_fft=include_fft,
                naming_profile=self.naming_profile,
                theme=theme,
                progress_callback=lambda message: result_queue.put(("progress", message)),
            )

        self._start_worker(worker)

    def _generate_serialem_report(self, output_path: Path, session: SerialEMSession) -> None:
        quality = self._quality_key()
        save_manifest = self.save_manifest_var.get()
        theme = self._selected_report_theme()

        def worker(result_queue: queue.Queue[tuple[str, object]]) -> Path:
            result = generate_serialem_report(
                output_path,
                session,
                image_quality=quality,
                theme=theme,
                progress_callback=lambda message: result_queue.put(("progress", message)),
            )
            if save_manifest:
                save_serialem_manifest(session, output_path.with_suffix(".serialem.json"))
            return result

        self._start_worker(worker)

    def _poll_report_result(self, result_queue: queue.Queue[tuple[str, object]]) -> None:
        try:
            status, value = result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_report_result, result_queue)
            return
        if status == "progress":
            self.status_var.set(cast(str, value))
            self.root.after(100, self._poll_report_result, result_queue)
        elif status == "success":
            self._report_finished(cast(Path, value))
        else:
            self._report_failed(cast(Exception, value))

    def _report_finished(self, output_path: Path) -> None:
        self._update_generate_state()
        self.root.configure(cursor="")
        self.status_var.set(f"Report saved: {output_path}")
        messagebox.showinfo(
            "Report created",
            f"The screening report was saved successfully:\n\n{output_path}",
            parent=self.root,
        )

    def _report_failed(self, error: Exception) -> None:
        self._update_generate_state()
        self.root.configure(cursor="")
        self.status_var.set("Report generation failed.")
        messagebox.showerror("Could not generate report", str(error), parent=self.root)


def main() -> None:
    root = tk.Tk()
    ScreeningReportApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
