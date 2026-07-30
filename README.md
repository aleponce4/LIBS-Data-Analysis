# ProLIBSpector (Public Edition)

[![CI](https://github.com/aponcefl/libs-spectroscopy-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/aponcefl/libs-spectroscopy-workbench/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

Scientific instrument-control and Laser-Induced Breakdown Spectroscopy (LIBS) analysis software featuring simulated acquisition, spectrum preprocessing, elemental line identification, and 2D mapping visualization.

---

## Public-Edition Scope

> This repository contains the public edition of ProLIBSpector, a scientific instrument-control and LIBS analysis application. It includes selected generic components for simulated acquisition, spectrum processing, visualization, metadata recording, and nonconfidential analysis workflows. Commercial, customer-specific, proprietary classification, production automation, and private hardware functionality are maintained separately.

---

## Relationship to Private Product

`ProLIBSpector` remains the complete private product and authoritative development codebase. This public repository demonstrates generic scientific-software architecture, software hardware simulation, signal processing algorithms, visualization, testing, and software reliability without exposing commercially sensitive startup algorithms or vendor SDK binaries.

---

## Supported Features

- **Hardware-Free Simulation**: `SimulatedSpectrometer` engine mimicking dark counts, integration timing responses, synthetic emission lines, shot noise, and trigger delays.
- **Spectral Preprocessing**: Savitzky-Golay filtering, Asymmetric Least Squares (ALS) baseline correction, Area normalization, Maximum normalization, and Standard Normal Variate (SNV) transformation.
- **Elemental Line Identification**: NIST element line database integration (`element_database.csv`), persistent emission line overlays, and peak matching tolerances.
- **Interactive Visualization**: High-DPI PyQtGraph interactive spectrum canvas, region-of-interest (ROI) selection, peak annotation, and dark spectrum subtraction.
- **Asynchronous File Export**: Lock-free background exporter thread saving acquired spectra and JSON reproducibility manifests with SHA256 data checksums.
- **2D Mapping Analysis**: Spatial grid spectrum visualization and peak intensity heatmap rendering.

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
