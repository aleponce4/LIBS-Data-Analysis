# Help documents

These markdown files are reference copies of the in-app help text. **Nothing in
the application reads them at runtime.**

Each help dialog builds its content from a markdown string defined inside the
module that owns the dialog, and renders it with `markdown` + `tkhtmlview`:

| Document | Dialog source |
| --- | --- |
| `adjust_help.md` | `src/prolibspector/analysis/adjust_plot.py` |
| `spectrum_help.md` | `src/prolibspector/analysis/adjust_spectrum.py` |
| `threshold_help.md` | `src/prolibspector/analysis/label_peaks.py` |
| `periodic_table_help.md` | `src/prolibspector/analysis/search_element.py` |

Because the strings in those modules are what users actually see, editing a file
in this directory changes nothing in the application. Edit the module instead,
and update the copy here if you want the two to stay in step.
