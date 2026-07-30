# ProLIBSpector (Public Edition)

[![CI](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

## About

Python-based software for scientific instrument control, automated motorized sample positioning, pulsed laser triggering, and spectral data analysis for Laser-Induced Breakdown Spectroscopy (LIBS). Controls Ocean Optics, Thorlabs CCS, and YiXist spectrometers, GRBL 1.1 motorized $X\text{--}Y\text{--}Z$ stages, and Q-switched pulse lasers via an interactive desktop GUI built with Tkinter and PyQtGraph. Features automated 2x2 microplate mapping, spatial grid rastering, spectral preprocessing, high-DPI scientific plotting, and NIST Atomic Spectra Database elemental line identification.

---

## Public-Edition Scope

> This repository contains the public edition of ProLIBSpector, a scientific instrument-control and LIBS analysis application. It includes selected generic components for simulated acquisition, spectrum processing, visualization, metadata recording, and nonconfidential analysis workflows. Commercial, customer-specific, proprietary classification, production automation, and private hardware DLL binaries are maintained separately.

---

## Relationship to Private Product

`ProLIBSpector` remains the complete private product and authoritative development codebase. This public repository demonstrates generic scientific-software architecture, software hardware simulation, signal processing algorithms, visualization, testing, and software reliability without exposing commercially sensitive startup algorithms or vendor SDK binaries.

---

## Supported Hardware & Interfaces

ProLIBSpector includes driver abstraction layers for commercial spectroscopic and motion hardware alongside a zero-dependency simulated backend for hardware-free execution:

- **Spectrometers**:
  - **Ocean Optics**: USB2000+, HR4000, Flame, Maya2000 Pro, Spark (via SeaBreeze C-library and PyUSB backends).
  - **Thorlabs CCS Series**: CCS100, CCS125, CCS150, CCS175, CCS200 compact spectrometers (via `TLCCS` C-DLL and VISA driver wrappers).
  - **YiXist YSM Series**: YSM-8111-06-01 high-resolution spectrometers (via C++ DLL wrapper interfaces).
- **Motorized Stage Motion Control ($X\text{--}Y\text{--}Z$)**:
  - **GRBL 1.1 Controllers**: Multi-axis stepper motor stage positioning via serial GRBL protocol ($X\text{--}Y$ spatial rastering, $Z$-axis focal positioning, microplate well alignment, teach-point calibration).
- **Pulsed Ablation Lasers**:
  - **Q-Switched Pulsed Lasers**: Monport K40 relay, LaserArt 10 Hz Nd:YAG laser control, external pulse generator synchronization, and foot-switch firing semantics.
- **Simulated Hardware Engine**:
  - Zero-dependency simulation engines (`SimulatedSpectrometer`, simulated 2-axis stage) mimicking physical nanosecond plasma emission decay, dark counts, exposure integration timing, stage movement, shot noise, and trigger delay responses.

---

## Core Features

- **Hardware-Free Simulation**: `SimulatedSpectrometer` and simulated stage engines for full workflow testing without physical hardware.
- **Automated Multi-Plate & Grid Acquisition**: Motorized stage raster scanning, 2x2 multi-plate holder support (P1–P4, Corning 96-well format), live spectrum canvas, and well status maps.
- **Spectral Preprocessing Engine**: Savitzky-Golay filtering, Asymmetric Least Squares (ALS) baseline correction, Area normalization, Maximum normalization, and Standard Normal Variate (SNV) transformation.
- **Elemental Line Identification**: Internal reference database derived from NIST Atomic Spectra Database (ASD) data covering neutral and ionic species ($I, II, III$).
- **Interactive Visualization**: High-DPI PyQtGraph interactive spectrum canvas, region-of-interest (ROI) selection, peak annotation, and dark spectrum subtraction.
- **Asynchronous File Export**: Lock-free background exporter thread saving acquired spectra and JSON reproducibility manifests with SHA256 data checksums.
- **Ground-Truth 2D Mapping Analysis**: Spatial grid spectrum visualization, ground-truth benchmark separation, and peak intensity heatmap rendering.

---

## Elemental Line Database & NIST Citation

Elemental line search and peak matching rely on an internal reference database derived from NIST Atomic Spectra Database (ASD) data covering neutral and ionic species ($I, II, III$).

### NIST ASD Citation

> Kramida, A., Ralchenko, Y., Reader, J., and NIST ASD Team. *NIST Atomic Spectra Database* (Version 5.12). National Institute of Standards and Technology, Gaithersburg, MD. DOI: [10.18434/T4W30F](https://doi.org/10.18434/T4W30F).

---

## Supported File Input & Export Formats

- **Input Formats**:
  - Legacy & standard single-shot / average spectral CSV files.
  - Multi-column spatial mapping index manifests (`_mapping_grid_index.csv`).
  - Binary memory-mapped intensity arrays (`_mapping_spectrum_store/intensities.npy`).
  - JSON run configuration and calibration snapshots (`_mapping_grid_manifest.json`).

- **Export & Storage Formats**:
  - **CSV Spectral Files**: Standard 2-column $(\lambda, \text{Intensity})$ and multi-point CSV exports.
  - **High-Density Binary Stores**: Chunked binary arrays (`.npy`) for rapid disk write/read during high-speed raster scanning.
  - **Reproducibility Manifests**: Automated JSON metadata logs containing spectrometer settings, laser energy profiles, timestamped spatial coordinates, and SHA256 data checksums.
  - **Graphics & Figures**: Export to high-resolution PNG, PDF, and SVG formats via Matplotlib and PyQtGraph canvases.

---

## High-Performance Analysis, Matrix Caching & Parallel Architecture

Processing large-scale 2D LIBS spatial maps (thousands of grid points, multiple shot replicates per point, multi-element spectral line scoring) requires optimized data structures and memory management:

- **Binary Memory Mapping (`.npy`)**: Large mapping datasets use memory-mapped binary array stores (`intensities.npy`) to avoid the I/O overhead of parsing gigabytes of CSV text files.
- **Content-Addressed Deterministic Caching**: Preprocessing results, candidate line scans, and fused multi-element intensity grids are cached under cryptographic signatures (`_mapping_analysis_cache/`). The cache signature hashes the algorithm version, preprocessing settings, sideband geometry, and line selection rules to ensure instantaneous re-loading without redundant re-computation.
- **Vectorized Matrix Computations**: Peak net-area integration, local continuum sideband estimation, empirical null distributions, and robust Z-score computations are fully vectorized using NumPy.
- **Asynchronous Non-Blocking I/O**: Multi-threaded acquisition queues and a dedicated lock-free background disk writer thread (`mapping_save_writer.py`) prevent UI freezes during continuous high-frequency data acquisition.

---

## Visual Demonstration & Evidence

### 1. Main Application Interface (Analysis Mode)

![Main Application Interface](docs/img/main_gui_analysis.png)
*_Main application interface in Analysis Mode displaying an imported reference Titanium spectrum (200–880 nm) with interactive curve controls, spectrum parameter sidebar, and plot navigation tools._*

---

### 2. Automated Acquisition Interface (2x2 Multi-Plate Holder Mode)

![Automated Acquisition Interface](docs/img/gui_automated_acquisition_96well_real.png)
*_Automated acquisition interface running a 2x2 multi-plate holder workflow (Plates P01–P04, Corning 96-well format). Displays live spectrum acquisition canvas (Shot #27), active plate progress map (Plate 1/4, 9/96 wells completed, next target A10), and step-by-step wizard sidebar._*

---

### 3. Interactive Periodic Table Element Selector

![Interactive Periodic Table Selector](docs/img/gui_periodic_table.png)
*_Interactive Periodic Table dialog interface enabling element selection, database selection, and ionization state filtering (I, II, III) for NIST line overlay matching._*

---

### 4. Peak Identification & NIST Database Search

![Peak Identification & NIST Overlay](docs/img/peak_identification_nist.png)
*_Automated peak detection and NIST elemental line matching for Ti I and Ti II emission lines (e.g. 335 nm, 454 nm, 501 nm) with configurable intensity threshold sliders and round-off error tolerances._*

---

### 5. Ground-Truth 2D LIBS Spatial Mapping (Decorated Ceramic Tile)

![Decorated Ceramic Tile 2D Mapping Composite](docs/img/ceramic_tile_2d_mapping_composite.png)
*_Ground-truth 2D LIBS elemental mapping composite of a decorated ceramic tile sample. Panels display the original sample photograph, co-localized blue pigment elements (Copper Cu vs. Cobalt Co), overglaze dark outline ink (Manganese Mn vs. Lead Pb), and the non-present negative control (Cesium Cs). Colorbar indicates signal-to-noise ratio ($0\text{--}10\sigma$)._*

---

### 6. Single-Point Annotated LIBS Emission Spectrum

![Annotated LIBS Emission Spectrum](docs/img/ceramic_tile_annotated_spectrum.png)
*_Annotated net-intensity spectrum from a ceramic tile sample point across 200–1000 nm, identifying key neutral and ionic emission lines for Cu I (325/327 nm), Co I (358/361 nm), Pb I (368/406 nm), Ca II (393/397 nm), Na I (589 nm), and K I (766/769 nm)._*

---

### 7. Timing & Signal-to-Noise Optimization Matrix

![SNR Optimization Heatmap](docs/img/delay_sweep_snr_heatmap.png)
*_Signal-to-Noise Ratio (SNR) parameter matrix generated across trigger delays (0–32 µs) and integration windows (0.009–1 ms), identifying an optimal acquisition window at 8 µs delay and 0.1 ms integration (SNR 192)._*

---

## Architecture Overview

```mermaid
flowchart TD
    UI[GUI Layer / PyQt5 & PyQtGraph] --> Launcher[Mode Launcher]
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
git clone https://github.com/aponcefl/libs-spectroscopy-workbench.git
cd libs-spectroscopy-workbench
pip install -e .
```

### Running the Application

Launch the desktop interface using the simulated backend:

```bash
python main.py
```

Select **Simulated Acquisition** to test live spectral collection without physical hardware, or **Analysis Mode** to process offline spectra and compare NIST elemental lines.

---

## Testing & Validation Scope

Automated unit and integration tests verify device initialization, wavelength generation, baseline correction algorithms, multi-threaded export queues, and reproducibility manifest formatting.

To execute the test suite locally:

```bash
pytest
```

The test profile uses synthetic fixtures and simulated hardware drivers. Execution on physical spectrometers and commercial hardware drivers is validated separately on hardware test benches.

---

## Hardware Simulation

The primary execution engine in this public edition is `SimulatedSpectrometer`. It generates synthetic plasma emission spectra based on physical decay models:

$$\text{Intensity}(\lambda, t) = I_{\text{lines}}(\lambda) e^{-t / \tau_{\text{lines}}} + I_{\text{continuum}}(\lambda) e^{-t / \tau_{\text{continuum}}} + \text{Noise}$$

This allows full workflow execution, GUI testing, and data stream validation without requiring a physical spectrometer or pulse laser.

---

## Known Limitations

- **Physical Drivers Omitted**: Physical USB drivers (Ocean Optics SeaBreeze), YiXist C++ DLL wrappers, and GRBL 1.1 laser motion control are omitted from the public edition.
- **Classification Models Omitted**: Proprietary seed classification and delay sweep analysis modules are maintained in the private product repository.

---

## License

This public edition is released under the [GNU General Public License v3.0](LICENSE).
