#!/usr/bin/env python3
"""Tkinter GUI for the DeepState overview screenshot tool."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

OUTPUT_DIR = Path.home() / "Pictures" / "DeepState Overviews"
PLAYWRIGHT_CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"

# Pin Playwright's browser lookup to a writable, stable user-home location.
# Without this, the bundled driver sets PLAYWRIGHT_BROWSERS_PATH to "0" when
# `sys.frozen` is True, which means "look inside the .app bundle" — that dir
# is wiped on every rebuild and may be read-only under Gatekeeper
# translocation. Must run before any `from playwright...` import.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_CACHE)

# Make `deepstate_screenshot` importable both from source and from a
# PyInstaller .app bundle (where data files land in sys._MEIPASS).
if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys._MEIPASS)))  # type: ignore[attr-defined]
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

import deepstate_screenshot as ds  # noqa: E402


CHROMIUM_PROMPT = (
    "First-time setup: downloading the browser engine (~150MB). "
    "This takes a minute or two and only happens once."
)

DEFAULT_LOCATIONS = "Sumy\nVovchansk\nKupiansk\nKramatorsk\nPokrovsk"


def _chromium_installed() -> bool:
    # Ask Playwright where the binary should be for *this* version, then check
    # that exact path. A bare "any chromium-* dir exists" check passes when an
    # older revision is cached but the bundled Playwright wants a newer one.
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_PROGRESS_RE = re.compile(r"(\d{1,3})\s*%")


def _install_chromium(
    on_text: Callable[[str], None] | None = None,
    on_percent: Callable[[int], None] | None = None,
) -> None:
    """Run `playwright install` and stream progress to the callbacks.

    We invoke the bundled node driver directly (not `playwright.__main__.main`)
    because the latter calls `sys.exit()` — which raises `SystemExit` and
    silently kills the worker thread after a successful install. Reading
    char-by-char lets us catch `\r`-overwritten progress bars, not just
    newline-terminated lines.
    """
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver_executable, driver_cli = compute_driver_executable()
    proc = subprocess.Popen(
        [
            str(driver_executable),
            str(driver_cli),
            "install",
            "chromium",
            "chromium-headless-shell",
        ],
        env=get_driver_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
    )
    assert proc.stdout is not None

    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in ("\r", "\n"):
            line = buf.strip()
            buf = ""
            if not line:
                continue
            if on_text is not None:
                on_text(line)
            if on_percent is not None:
                m = _PROGRESS_RE.search(line)
                if m:
                    on_percent(max(0, min(100, int(m.group(1)))))
        else:
            buf += ch
    if buf.strip() and on_text is not None:
        on_text(buf.strip())

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"playwright install failed (exit code {rc})")


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

        self._progress = ttk.Progressbar(
            frame, mode="determinate", maximum=100, length=400
        )
        # Hidden until an install starts. Pack-managed so we can show/hide.

        self._status = ttk.Label(frame, text="Ready.", foreground="#555")
        self._status.pack(anchor="w")

    def _set_status(self, text: str) -> None:
        self._status.configure(text=text)

    def _show_progress(self) -> None:
        self._progress.configure(value=0, mode="determinate")
        self._progress.pack(fill="x", pady=(0, 8), before=self._status)

    def _set_progress(self, percent: int) -> None:
        # If we hit 100% but install is still running, switch to a marquee so
        # the user can tell the post-download extract phase is still working.
        if percent >= 100 and str(self._progress.cget("mode")) == "determinate":
            self._progress.configure(mode="indeterminate")
            self._progress.start(15)
        elif str(self._progress.cget("mode")) == "determinate":
            self._progress.configure(value=percent)

    def _hide_progress(self) -> None:
        self._progress.stop()
        self._progress.pack_forget()

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
                self._root.after(0, self._show_progress)
                try:
                    _install_chromium(
                        on_text=lambda line: self._root.after(0, self._set_status, line),
                        on_percent=lambda pct: self._root.after(0, self._set_progress, pct),
                    )
                finally:
                    self._root.after(0, self._hide_progress)

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
