# ProLIBSpector (Public Edition)

[![CI](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/aleponce4/libs-spectroscopy-workbench/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org)

## Overview

ProLIBSpector is a scientific software platform for Laser-Induced Breakdown Spectroscopy (LIBS) data processing, spectral visualization, elemental line identification, and 2D spatial mapping.

> **Product Scope & Relationship**  
> The complete private product (`ProLIBSpector`) supports Ocean Optics, Thorlabs CCS, and YiXist spectrometers, GRBL-controlled stages, and pulsed-laser triggering. This public edition provides the generic interfaces, simulated hardware engines, spectral analysis algorithms, interactive visualization, and selected nonconfidential acquisition workflows. Commercial classification models and physical driver binaries are maintained separately.

---

## Supported Public Features

- **Hardware Simulation Engine**: `SimulatedSpectrometer` engine mimicking physical nanosecond plasma emission decay, dark counts, exposure integration timing, shot noise, and trigger delay responses.
- **Automated Multi-Plate Acquisition Interface**: Automated workflow interface supporting 2x2 multi-plate holders (P1–P4, Corning 96-well format), live spectrum canvas, and well status maps.
- **Spectral Preprocessing Engine**: Savitzky-Golay filtering, Asymmetric Least Squares (ALS) baseline correction, Area normalization, Maximum normalization, and Standard Normal Variate (SNV) transformation.
- **NIST Line Database Search**: Internal reference database derived from NIST Atomic Spectra Database (ASD) data covering neutral and ionic species ($I, II, III$).
- **Interactive Visualization**: High-DPI PyQtGraph interactive spectrum canvas, region-of-interest (ROI) selection, peak annotation, and dark spectrum subtraction.
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
git clone https://github.com/aleponce4/libs-spectroscopy-workbench.git
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

Automated unit and integration tests verify device initialization, wavelength generation, baseline correction algorithms, background save writer reliability, and 2D spatial mapping output contracts.

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
