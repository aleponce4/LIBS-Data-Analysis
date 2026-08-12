# ProLIBSpector (Public Edition)

[![CI](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

## Overview

ProLIBSpector is a scientific software platform for Laser-Induced Breakdown Spectroscopy (LIBS) data processing, spectral visualization, elemental line identification, and 2D spatial mapping.

> **Product Scope & Relationship**  
> The complete private product (`ProLIBSpector`) supports Ocean Optics, Thorlabs CCS, and YiXist spectrometers, GRBL-controlled stages, and pulsed-laser triggering. This public edition provides the generic interfaces, simulated hardware engines, spectral analysis algorithms, interactive visualization, and selected nonconfidential acquisition workflows. Commercial classification models and physical driver binaries are maintained separately.
>
> **Not in this edition**, and reported as such in the UI rather than failing silently: vendor spectrometer drivers (Ocean Optics via seabreeze, Thorlabs CCS via TLCCS/NI-VISA, YiXist via its vendor SDK), the GRBL laser-stage driver, automated laser-stage runs, automated 2D mapping *acquisition*, and the trained spectra-readiness calibration. Analysing existing 2D mapping runs is fully supported here; only acquiring them requires the private edition. Modules standing in for these features carry a `PUBLIC-EDITION STUB` header stating what the private version does.

---

## Supported Public Features

- **Hardware Simulation Engine**: `SimulatedSpectrometer` engine mimicking physical nanosecond plasma emission decay, dark counts, exposure integration timing, shot noise, and trigger delay responses.
- **High-Throughput Plate Workflow**: Plate autosave for 6/12/24/48/96/384-well formats with configurable shots-per-well and row- or column-major ordering, a live well-status plate map, per-shot discard, specific-well repair passes, atomic run-state persistence, and resume of an interrupted plate (from saved state or by scanning the folder).
- **Spectra Readiness QC**: Deterministic per-shot gating on detector saturation, signal-to-noise, and flat-trace detection, with a per-shot CSV and one-click requeueing of non-passing wells.
- **Spectral Preprocessing Engine**: Savitzky-Golay filtering, Asymmetric Least Squares (ALS) baseline correction, Area normalization, Maximum normalization, and Standard Normal Variate (SNV) transformation.
- **NIST Line Database Search**: Internal reference database derived from NIST Atomic Spectra Database (ASD) data covering neutral and ionic species ($I, II, III$).
- **Interactive Visualization**: High-DPI Tkinter/Matplotlib interactive spectrum canvas, region-of-interest (ROI) selection, peak annotation, and dark spectrum subtraction.
- **Reliable Asynchronous Persistence**: Bounded background save queue with ordered single-writer persistence, backpressure, failure propagation, and drain-before-finalization behavior.
- **Spatially Resolved 2D Mapping**: Grid spectrum visualization, multi-element evidence fusion, and peak intensity heatmap rendering.

---

## Technical Overview & Visual Evidence

### 1. Main Application Interface (Analysis Mode)

![Main Application Interface](docs/img/main_gui_analysis.png)
*_Main application interface in Analysis Mode displaying an imported reference Titanium spectrum (200–880 nm) with interactive curve controls, spectrum parameter sidebar, and plot navigation tools._*

---

### 2. Automated Acquisition Interface (2x2 Multi-Plate Holder Mode)

![Automated Acquisition Interface](docs/img/gui_automated_acquisition_96well_real.png)
*_Automated acquisition interface running a 2x2 multi-plate holder workflow (Plates P01–P04, Corning 96-well format). Displays live spectrum acquisition canvas (Shot #27), active plate progress map (Plate 1/4, 9/96 wells completed, next target A10), and step-by-step wizard sidebar._*

---

### 3. Peak Identification & NIST Database Search

![Peak Identification & NIST Overlay](docs/img/peak_identification_nist.png)
*_Automated peak detection and NIST elemental line matching for Ti I and Ti II emission lines (e.g. 335 nm, 454 nm, 501 nm) with configurable intensity threshold sliders and round-off error tolerances._*

---

### 4. Spatially Resolved 2D LIBS Mapping Demonstration (Decorated Ceramic Tile)

![Decorated Ceramic Tile 2D Mapping Composite](docs/img/ceramic_tile_2d_mapping_composite.png)
*_Spatially resolved 2D LIBS elemental mapping composite of a decorated ceramic tile sample. Panels display the original sample photograph alongside corresponding spatial regions for blue pigments (Copper Cu, Cobalt Co), overglaze dark outline ink (Manganese Mn, Lead Pb), and non-present control (Cesium Cs). Colorbar indicates signal-to-noise ratio ($0\text{--}10\sigma$)._*

> For additional figures (Periodic Table selector, annotated spectra, SNR timing matrices), file format specifications, and caching internals, see the [Technical Gallery & Architecture Guide](docs/gallery.md).

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

An editable install is used above because the bundled data directories
(`Icons/`, `Help/`, `element_database.csv`, `persistent_lines.csv`,
`calibration_data_library.csv`) live at the repository root, alongside `src/`.

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
