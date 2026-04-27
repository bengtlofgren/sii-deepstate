#!/usr/bin/env python3
"""Tkinter GUI for the DeepState overview screenshot tool."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Make `deepstate_screenshot` importable both from source and from a
# PyInstaller .app bundle (where data files land in sys._MEIPASS).
if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys._MEIPASS)))  # type: ignore[attr-defined]
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

import deepstate_screenshot as ds  # noqa: E402


OUTPUT_DIR = Path.home() / "Pictures" / "DeepState Overviews"
PLAYWRIGHT_CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"

CHROMIUM_PROMPT = (
    "First-time setup: downloading the browser engine (~80MB). "
    "This takes about a minute and only happens once."
)

DEFAULT_LOCATIONS = "Sumy\nVovchansk\nKupiansk\nKramatorsk\nPokrovsk"


def _chromium_installed() -> bool:
    """Heuristic check for a previously-downloaded Playwright Chromium build."""
    if not PLAYWRIGHT_CACHE.exists():
        return False
    return any(child.name.startswith("chromium") for child in PLAYWRIGHT_CACHE.iterdir())


def _install_chromium() -> None:
    """Run `playwright install chromium` from inside the current Python env."""
    from playwright.__main__ import main as pw_main

    saved = sys.argv
    try:
        sys.argv = ["playwright", "install", "chromium"]
        pw_main()
    finally:
        sys.argv = saved


def _open_in_preview(path: Path) -> None:
    subprocess.run(["open", str(path)], check=False)


class _StatusWriter(io.TextIOBase):
    """File-like object that pipes line-buffered writes to the Tk main thread."""

    def __init__(self, root: tk.Tk, callback) -> None:
        super().__init__()
        self._root = root
        self._callback = callback
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self._root.after(0, self._callback, line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._root.after(0, self._callback, self._buf.strip())
        self._buf = ""


class App:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        root.title("DeepState Overview")
        root.minsize(520, 460)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Locations (one per line, at least two):",
        ).pack(anchor="w")

        self._text = tk.Text(frame, height=12, wrap="none", font=("Helvetica", 13))
        self._text.pack(fill="both", expand=True, pady=(4, 12))
        self._text.insert("1.0", DEFAULT_LOCATIONS)

        self._satellite = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Satellite basemap",
            variable=self._satellite,
        ).pack(anchor="w")

        self._button = ttk.Button(
            frame,
            text="Generate Overview",
            command=self._on_generate,
        )
        self._button.pack(pady=(12, 8))

        self._status = ttk.Label(frame, text="Ready.", foreground="#555")
        self._status.pack(anchor="w")

    def _set_status(self, text: str) -> None:
        self._status.configure(text=text)

    def _on_generate(self) -> None:
        raw_lines = self._text.get("1.0", "end").splitlines()
        locations = [line.strip() for line in raw_lines if line.strip()]
        if len(locations) < 2:
            messagebox.showwarning(
                "Need more locations",
                "Enter at least two locations, one per line.",
            )
            return

        if not _chromium_installed():
            if not messagebox.askokcancel("First-time setup", CHROMIUM_PROMPT):
                return

        self._button.configure(state="disabled")
        self._set_status("Working…")
        thread = threading.Thread(
            target=self._run_job,
            args=(locations, self._satellite.get()),
            daemon=True,
        )
        thread.start()

    def _run_job(self, locations: list[str], satellite: bool) -> None:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            if not _chromium_installed():
                self._root.after(0, self._set_status, "Downloading browser engine…")
                _install_chromium()

            writer = _StatusWriter(self._root, self._set_status)
            saved_stdout = sys.stdout
            sys.stdout = writer
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(
                            viewport={"width": 1920, "height": 1080}
                        )
                        page = context.new_page()
                        page.add_init_script(ds.LEAFLET_CAPTURE_SCRIPT)
                        output = ds.process_overview(
                            page,
                            location_names=locations,
                            output_dir=str(OUTPUT_DIR),
                            satellite=satellite,
                        )
                    finally:
                        browser.close()
            finally:
                sys.stdout = saved_stdout

            self._root.after(0, self._on_done, Path(output), None)
        except Exception as exc:  # noqa: BLE001
            self._root.after(0, self._on_done, None, exc)

    def _on_done(self, path: Path | None, error: Exception | None) -> None:
        self._button.configure(state="normal")
        if error is not None:
            self._set_status("Error.")
            messagebox.showerror("Something went wrong", str(error))
            return
        if path is None:
            self._set_status("Cancelled.")
            return
        self._set_status(f"Saved to {path}")
        _open_in_preview(path)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
