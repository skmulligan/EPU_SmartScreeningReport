"""Tkinter desktop interface for ScreeningReport."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import cast

from .discovery import DiscoveryError, discover_grid_folders, extract_project_number
from .models import GridFolder
from .naming import DEFAULT_NAMING_PROFILE, NamingProfile
from .pdf_report import (
    DEFAULT_IMAGE_QUALITY,
    IMAGE_QUALITY_PROFILES,
    generate_screening_report,
)


class ScreeningReportApp:
    """Main application window."""

    def __init__(
        self,
        root: tk.Tk,
        naming_profile: NamingProfile = DEFAULT_NAMING_PROFILE,
    ) -> None:
        self.root = root
        self.naming_profile = naming_profile
        self.root.title("Screening Report")
        self.root.minsize(880, 570)
        self.root.geometry("980x650")

        self.atlas_directory: Path | None = None
        self.grids: list[GridFolder] = []
        self.atlas_path_var = tk.StringVar()
        self.image_quality_var = tk.StringVar(
            value=IMAGE_QUALITY_PROFILES[DEFAULT_IMAGE_QUALITY].label
        )
        self.include_fft_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(
            value="Select an atlas session directory to find associated grid folders."
        )

        self._configure_style()
        self._build_interface()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", foreground="#475569")
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Generate.TButton", font=("Segoe UI", 10, "bold"))

    def _build_interface(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(outer, text="Screening Report", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text=(
                "Select the EPU atlas session. Matching autoloader slot folders "
                "will be loaded automatically."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 16))

        atlas_frame = ttk.LabelFrame(outer, text="Atlas Directory", padding=12)
        atlas_frame.grid(row=2, column=0, sticky="ew")
        atlas_frame.columnconfigure(0, weight=1)
        atlas_entry = ttk.Entry(
            atlas_frame,
            textvariable=self.atlas_path_var,
            state="readonly",
        )
        atlas_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            atlas_frame,
            text="Browse for Atlas Directory...",
            command=self.browse_for_atlas,
        ).grid(row=0, column=1, padx=(8, 0))

        grids_frame = ttk.LabelFrame(outer, text="Grid Folders", padding=10)
        grids_frame.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        grids_frame.columnconfigure(0, weight=1)
        grids_frame.rowconfigure(1, weight=1)
        self.grid_summary_label = ttk.Label(
            grids_frame,
            text="No grid folders loaded",
            style="Subtitle.TLabel",
        )
        self.grid_summary_label.grid(row=0, column=0, sticky="w", pady=(0, 7))

        tree_container = ttk.Frame(grids_frame)
        tree_container.grid(row=1, column=0, sticky="nsew")
        tree_container.columnconfigure(0, weight=1)
        tree_container.rowconfigure(0, weight=1)

        self.grid_tree = ttk.Treeview(
            tree_container,
            columns=("slot", "name", "atlas"),
            show="headings",
            selectmode="browse",
        )
        self.grid_tree.heading("slot", text="Slot")
        self.grid_tree.heading("name", text="Grid folder name")
        self.grid_tree.heading("atlas", text="Matched atlas")
        self.grid_tree.column("slot", width=70, minwidth=60, anchor="center", stretch=False)
        self.grid_tree.column("name", width=560, minwidth=320, anchor="w")
        self.grid_tree.column("atlas", width=220, minwidth=170, anchor="w")
        self.grid_tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            tree_container,
            orient="vertical",
            command=self.grid_tree.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_tree.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            textvariable=self.status_var,
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        quality_frame = ttk.Frame(footer)
        quality_frame.grid(row=0, column=1, padx=(12, 12))
        ttk.Label(quality_frame, text="PDF image quality:").grid(
            row=0,
            column=0,
            padx=(0, 6),
        )
        ttk.Combobox(
            quality_frame,
            textvariable=self.image_quality_var,
            values=tuple(
                profile.label for profile in IMAGE_QUALITY_PROFILES.values()
            ),
            state="readonly",
            width=25,
        ).grid(row=0, column=1)
        ttk.Checkbutton(
            quality_frame,
            text="Include FFT power spectra",
            variable=self.include_fft_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.generate_button = ttk.Button(
            footer,
            text="Generate PDF Report...",
            command=self.choose_report_output,
            state="disabled",
            style="Generate.TButton",
        )
        self.generate_button.grid(row=0, column=2, sticky="e")

    def browse_for_atlas(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Select EPU atlas session directory",
            mustexist=True,
        )
        if selected:
            self.load_atlas_directory(Path(selected))

    def load_atlas_directory(self, atlas_directory: Path) -> None:
        """Validate an atlas selection and refresh the grid list."""

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
            self.generate_button.configure(state="normal")
        else:
            self.status_var.set(
                "No sibling folders ending in _Slot1 through _Slot12 were found."
            )
            self.generate_button.configure(state="disabled")

    def _refresh_grid_tree(self) -> None:
        for item in self.grid_tree.get_children():
            self.grid_tree.delete(item)
        for grid in self.grids:
            self.grid_tree.insert(
                "",
                "end",
                iid=str(grid.slot),
                values=(grid.slot, grid.name, grid.atlas_status),
            )
        count = len(self.grids)
        noun = "folder" if count == 1 else "folders"
        self.grid_summary_label.configure(text=f"{count} grid {noun} loaded")

    def choose_report_output(self) -> None:
        if not self.atlas_directory or not self.grids:
            messagebox.showerror(
                "No grids loaded",
                "Select an atlas directory with associated slot folders first.",
                parent=self.root,
            )
            return

        project_number = extract_project_number(
            self.atlas_directory.name,
            self.naming_profile,
        )
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save screening report",
            initialdir=str(self.atlas_directory.parent),
            initialfile=f"{project_number}_Screening_Report.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF documents", "*.pdf")],
        )
        if selected:
            self._generate_report(Path(selected))

    def _generate_report(self, output_path: Path) -> None:
        assert self.atlas_directory is not None
        atlas_directory = self.atlas_directory
        grids = list(self.grids)
        selected_quality = next(
            key
            for key, profile in IMAGE_QUALITY_PROFILES.items()
            if profile.label == self.image_quality_var.get()
        )
        include_fft = self.include_fft_var.get()
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.generate_button.configure(state="disabled")
        self.status_var.set("Generating PDF report...")
        self.root.configure(cursor="watch")

        def worker() -> None:
            try:
                result = generate_screening_report(
                    output_path,
                    atlas_directory,
                    grids,
                    image_quality=selected_quality,
                    include_fft=include_fft,
                    naming_profile=self.naming_profile,
                    progress_callback=lambda message: result_queue.put(
                        ("progress", message)
                    ),
                )
            except Exception as exc:
                result_queue.put(("error", exc))
            else:
                result_queue.put(("success", result))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_report_result, result_queue)

    def _poll_report_result(
        self,
        result_queue: queue.Queue[tuple[str, object]],
    ) -> None:
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
        self.generate_button.configure(state="normal")
        self.root.configure(cursor="")
        self.status_var.set(f"Report saved: {output_path}")
        messagebox.showinfo(
            "Report created",
            f"The screening report was saved successfully:\n\n{output_path}",
            parent=self.root,
        )

    def _report_failed(self, error: Exception) -> None:
        self.generate_button.configure(state="normal" if self.grids else "disabled")
        self.root.configure(cursor="")
        self.status_var.set("Report generation failed.")
        messagebox.showerror(
            "Could not generate report",
            str(error),
            parent=self.root,
        )


def main() -> None:
    root = tk.Tk()
    ScreeningReportApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
