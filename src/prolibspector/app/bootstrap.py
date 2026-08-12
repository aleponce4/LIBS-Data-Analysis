"""Application bootstrap and initialization for public edition."""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

from prolibspector.core.paths import ensure_parent, prepare_frozen_runtime, resource_path, working_path
from prolibspector.core.runtime import configure_dpi_awareness, set_window_icon
from prolibspector.core.ui_scale import apply_text_scale


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def log_error_to_file(error_message: str, file_name: str = "error_log.txt") -> Path:
    error_path = ensure_parent(working_path(file_name))
    with error_path.open("a", encoding="utf-8") as error_file:
        error_file.write(f"{error_message}\n")
    return error_path


def global_exception_handler(exc_type, value, tb) -> None:
    """Log the traceback to file AND print it to stderr.

    The previous version printed only "An error occurred. Please check
    error_log.txt." and swallowed the traceback. That is how this application
    shipped with eleven missing imports and an unconditional NameError: the
    failures were invisible. Always show the real traceback.
    """
    error_message = "".join(traceback.format_exception(exc_type, value, tb))

    try:
        error_path = log_error_to_file(error_message)
    except OSError as log_exc:  # never let logging hide the original failure
        error_path = None
        print(f"(could not write error log: {log_exc})", file=sys.stderr)

    sys.stderr.write(error_message)
    if error_path is not None:
        print(f"The traceback above was also written to {error_path}", file=sys.stderr)
    sys.stderr.flush()


def smoke_check() -> dict[str, str]:
    return {
        "resource_root": resource_path(),
        "error_log": working_path("error_log.txt"),
    }


def main() -> None:
    multiprocessing.freeze_support()
    configure_logging()
    sys.excepthook = global_exception_handler
    prepare_frozen_runtime()
    configure_dpi_awareness()

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    set_window_icon(root)

    try:
        from prolibspector.core.settings import get_ui_settings
        import sv_ttk
        sv_ttk.set_theme(get_ui_settings()["theme"].lower())
    except Exception:
        pass

    apply_text_scale(root)

    from prolibspector.app.launcher import ModeLauncher, MODE_CHOOSER
    launcher = ModeLauncher(root)
    selected_mode = launcher.run()

    if selected_mode is None:
        root.destroy()
        sys.exit(0)

    handoff = None
    while selected_mode is not None:
        if selected_mode == MODE_CHOOSER:
            root.withdraw()
            launcher = ModeLauncher(root)
            selected_mode = launcher.run()
            continue

        if selected_mode == "Analysis":
            from prolibspector.analysis.app import App
            import pandas as pd
            app = App(root)
            if handoff is not None:
                app.set_loaded_spectrum(
                    pd.Series(handoff["wavelengths"]),
                    pd.Series(handoff["intensities"]),
                    title="Acquired Spectrum",
                    status_text=f"Loaded acquired spectrum ({len(handoff['wavelengths'])} points).",
                )
                handoff = None
            app.run()
            selected_mode = app.get_requested_mode()
            continue

        if selected_mode == "Acquisition":
            from prolibspector.acquisition.app import AcquisitionApp
            acq_app = AcquisitionApp(root)
            acq_app.run()
            handoff = acq_app.get_handoff_data()
            if handoff is not None:
                selected_mode = "Analysis"
                continue
            selected_mode = acq_app.get_requested_mode()
            continue

        selected_mode = None


if __name__ == "__main__":
    main()
