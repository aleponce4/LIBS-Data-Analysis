# LIBS Spectroscopy Workbench

[![CI](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

## Overview

Instrument control and analysis software for a Laser-Induced Breakdown
Spectroscopy bench: stage motion, laser triggering, gated detection, unattended
multi-well and 2D-mapping runs, and the QC and characterization work around
them. It drives real hardware, and it runs end to end against simulated devices
with nothing plugged in.

This is the open core of **ProLIBSpector**, the software for a LIBS instrument
built by a small company. What is not here is the parts that cannot be
redistributed: one vendor's spectrometer DLL and the trained readiness
calibration. Everything those sit behind -- the device abstraction, the
out-of-process driver isolation, the run engine -- is here and works.

### What it does

**Motion and firing.** A GRBL controller driven over serial: status polling and
`$`-setting parsing, homing with limit verification, alarm lockout and `$X`
recovery, jogging, and program streaming with a flow controller sized to the
128-byte serial buffer. Laser firing is interlocked behind a safety checklist.

**Gated acquisition.** Spectrometers are reached through a two-tier abstraction,
with capability negotiation rather than assumption: a device that has no
programmable trigger delay says so, and the run engine plans around it. Three
backends ship -- Ocean Optics over python-seabreeze, Thorlabs CCS through
`TLCCS_64.dll` over NI-VISA, and a brokered client that runs a fragile vendor DLL
in a separate process so a hung driver is a killable subprocess rather than a
frozen application.

**Unattended runs.** Plate workflows for 6 through 384-well formats with
configurable shots per well, live well-status maps, per-shot discard, targeted
repair passes, atomic run-state persistence, and resume of an interrupted run
from either its state file or a folder scan. 2D raster mapping streams spectra
to a memory-mapped store as it goes.

**Timing characterization.** Gated LIBS lives or dies on when the detector opens:
the plasma continuum decays in about 1.6 us while the emission lines persist for
tens, so a gate that opens too early buries the lines and one that opens too late
loses them. `tools/delay_sweep_example.py` sweeps trigger delay against
integration window, scores each cell on resolved line count and signal-to-noise,
and recommends an operating point. It runs against the simulated detector, so the
figure below is reproducible from a clean checkout.

![Trigger delay and integration sweep](docs/img/delay_sweep_example/delay_sweep_heatmap.png)

*Signal-to-noise across an 8 x 5 grid of trigger delays and integration windows,
simulated. The interior optimum is the point of the exercise: both axes trade
off, and the best setting is not at either extreme.* Regenerate with
`python tools/delay_sweep_example.py docs/img/delay_sweep_example`; method notes
in [docs/trigger_delay_sweep.md](docs/trigger_delay_sweep.md).

**Running without hardware.** Two levels of simulation. `SimulatedGrblLaserController`
stands in for the controller in-process, which is fast and fine for exercising the
run engine. `GrblSerialSimulator` fakes the *serial port* instead, so the real
driver runs unmodified against something that answers the way GRBL 1.1 does --
which is what makes the command framing, the ack ledger, and the status parser
testable at all. Faults are injectable, because the driver code worth testing is
the error handling.

## Analysis features

- **Hardware Simulation Engine**: `SimulatedSpectrometer` engine mimicking physical nanosecond plasma emission decay, dark counts, exposure integration timing, shot noise, and trigger delay responses.
- **High-Throughput Plate Workflow**: Plate autosave for 6/12/24/48/96/384-well formats with configurable shots-per-well and row- or column-major ordering, a live well-status plate map, per-shot discard, specific-well repair passes, atomic run-state persistence, and resume of an interrupted plate (from saved state or by scanning the folder).
- **Spectra Readiness QC**: Deterministic per-shot gating on detector saturation, signal-to-noise, and flat-trace detection, with a per-shot CSV and one-click requeueing of non-passing wells.
- **Spectral Preprocessing Engine**: Savitzky-Golay and Gaussian smoothing, Asymmetric Least Squares (ALS) baseline correction, and three normalizations - min-max, total-intensity (area), and Standard Normal Variate (SNV).
- **NIST Line Database Search**: Internal reference database derived from NIST Atomic Spectra Database (ASD) data covering neutral and ionic species (I, II, III).
- **Interactive Visualization**: High-DPI Tkinter/ttk and Matplotlib spectrum canvas with axis and wavelength-range controls, automated peak detection, and NIST line annotation with overlap reduction.
- **Reliable Asynchronous Persistence**: Bounded background save queue with ordered single-writer persistence, backpressure, failure propagation, and drain-before-finalization behavior.
- **Spatially Resolved 2D Mapping**: Grid spectrum visualization, multi-element evidence fusion, and peak intensity heatmap rendering.

---

## Technical Overview & Screenshots

### 1. Main Application Interface (Analysis Mode)

![Main Application Interface](docs/img/main_gui_analysis.png)

*Main application interface in Analysis Mode displaying an imported reference Titanium spectrum (200-880 nm) with interactive curve controls, spectrum parameter sidebar, and plot navigation tools.*

---

### 2. Peak Identification & NIST Database Search

![Peak Identification & NIST Overlay](docs/img/peak_identification_nist.png)

*Automated peak detection and NIST elemental line matching for Ti I and Ti II emission lines (e.g. 335 nm, 454 nm, 501 nm), with sliders for label font size, intensity threshold, prominence filtering, and database matching tolerance.*

---

### 3. Spatially Resolved 2D LIBS Mapping Demonstration (Decorated Ceramic Tile)

![Decorated Ceramic Tile 2D Mapping Composite](docs/img/ceramic_tile_2d_mapping_composite.png)

*Spatially resolved 2D LIBS elemental mapping composite of a decorated ceramic tile sample. Panels display the original sample photograph alongside corresponding spatial regions for blue pigments (Copper Cu, Cobalt Co), overglaze dark outline ink (Manganese Mn, Lead Pb), and non-present control (Cesium Cs).*

*Illustrative figure. It was produced from a measurement run on the private edition's hardware; the source spectra and the script that rendered the composite are not part of this repository, so the colorbar values are not reproducible from what is published here.*

> For additional figures (Periodic Table selector, annotated spectra), file format specifications, and caching internals, see the [Technical Gallery & Architecture Guide](docs/gallery.md).

---

## Architecture Overview

```mermaid
flowchart TD
    UI[GUI Layer / Tkinter, ttk & Matplotlib] --> Launcher[Mode Launcher]
    Launcher --> Analysis[Analysis App]
    Launcher --> Acquisition[Acquisition App]
    
    Acquisition --> Worker[Acquisition Worker Thread]
    Worker --> DevBase[Spectrometer Base Interface]
    DevBase --> SimDev[SimulatedSpectrometer Backend]
    
    Worker --> Writer[Asynchronous Save Exporter]
    Writer --> Storage[(Disk Exporter: CSV & JSON Manifest)]
    
    Analysis --> Preproc[Preprocessing Module: ALS Baseline / SG Filter]
    Analysis --> NIST[NIST Line Database Search]
```

---

## Quick Start (Simulated Mode)

### Prerequisites

Python 3.10+ is required.

### Installation

```bash
git clone https://github.com/aleponce4/libs-spectroscopy-workbench.git
cd libs-spectroscopy-workbench
pip install -e .
```

Two optional features have optional dependencies: wavelet denoising needs
`PyWavelets`, and Parquet map exports need `pyarrow`. Install both with:

```bash
pip install -e ".[full]"
```

The editable install (`-e`) is required, not a convenience. The bundled assets
(`Icons/`, `Help/`, `element_database.csv`, `persistent_lines.csv`,
`calibration_data_library.csv`) live at the repository root alongside `src/`
rather than inside the package, and the application resolves them relative to
that root. A plain `pip install .` will import but will not find its icons or
spectral databases, so run the application from a checkout.

### Running the Application

Launch the desktop interface using the simulated backend:

```bash
python main.py
```

Select **Simulated Acquisition** to test live spectral collection without physical hardware, or **Analysis Mode** to process offline spectra and compare NIST elemental lines.

---

## Testing & Validation Scope

Automated unit and integration tests verify device initialization, wavelength generation, baseline correction algorithms, background save writer reliability, plate autosave and resume behavior, spectra readiness gating, and 2D spatial mapping output contracts.

`tests/test_imports.py` imports every module under `src/prolibspector` and fails on any `ImportError`. CI runs it as its own step before the rest of the suite, so a missing module or dependency can never again pass CI while the application is unable to start.

To execute the test suite locally:

```bash
pytest
```

---

## Elemental Line Database & NIST Citation

Elemental line search and peak matching rely on an internal reference table derived from NIST ASD data.

> Kramida, A., Ralchenko, Y., Reader, J., and NIST ASD Team. *NIST Atomic Spectra Database* (Version 5.12). National Institute of Standards and Technology, Gaithersburg, MD. DOI: [10.18434/T4W30F](https://doi.org/10.18434/T4W30F).

---

## License

This public edition is released under the [GNU General Public License v3.0](LICENSE).

