# ProLIBSpector Technical Gallery & Extended Architecture

This document provides supplementary visual case studies, file format specifications, caching internals, and hardware integration details for the ProLIBSpector platform.

---

## Supplementary Visual Demonstrations

### 1. Interactive Periodic Table Element Selector

![Interactive Periodic Table Selector](img/gui_periodic_table.png)
*_Interactive Periodic Table dialog interface enabling element selection, database selection, and ionization state filtering (I, II, III) for NIST line overlay matching._*

---

### 2. Single-Point Annotated LIBS Emission Spectrum

![Annotated LIBS Emission Spectrum](img/ceramic_tile_annotated_spectrum.png)
*_Annotated net-intensity spectrum from a ceramic tile sample point across 200–1000 nm, identifying key neutral and ionic emission lines for Cu I (325/327 nm), Co I (358/361 nm), Pb I (368/406 nm), Ca II (393/397 nm), Na I (589 nm), and K I (766/769 nm)._*

---

### 3. Timing & Signal-to-Noise Optimization Matrix

![SNR Optimization Heatmap](img/delay_sweep_snr_heatmap.png)
*_Signal-to-Noise Ratio (SNR) parameter matrix generated across trigger delays (0–32 µs) and integration windows (0.009–1 ms), identifying an optimal acquisition window at 8 µs delay and 0.1 ms integration (SNR 192)._*

---

## Private Product Hardware Integrations

The complete private product (`ProLIBSpector`) supports the following commercial hardware interfaces:

- **Spectrometers**:
  - **Ocean Optics**: USB2000+, HR4000, Flame, Maya2000 Pro, Spark (via SeaBreeze C-library and PyUSB backends).
  - **Thorlabs CCS Series**: CCS100, CCS125, CCS150, CCS175, CCS200 compact spectrometers (via `TLCCS` C-DLL and VISA driver wrappers).
  - **YiXist YSM Series**: YSM-8111-06-01 high-resolution spectrometers (via C++ DLL wrapper interfaces).
- **Motorized Stage Motion Control ($X\text{--}Y\text{--}Z$)**:
  - **GRBL 1.1 Controllers**: Multi-axis stepper motor stage positioning via serial GRBL protocol ($X\text{--}Y$ spatial rastering, $Z$-axis focal positioning, microplate well alignment, teach-point calibration).
- **Pulsed Ablation Lasers**:
  - **Q-Switched Pulsed Lasers**: Monport K40 relay, LaserArt 10 Hz Nd:YAG laser control, external pulse generator synchronization, and foot-switch firing semantics.

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

## High-Performance Analysis, Matrix Caching & Memory Architecture

Processing large-scale 2D LIBS spatial maps (thousands of grid points, multiple shot replicates per point, multi-element spectral line scoring) requires optimized data structures and memory management:

- **Binary Memory Mapping (`.npy`)**: Large mapping datasets use memory-mapped binary array stores (`intensities.npy`) to avoid the I/O overhead of parsing gigabytes of CSV text files.
- **Content-Addressed Deterministic Caching**: Preprocessing results, candidate line scans, and fused multi-element intensity grids are cached under cryptographic signatures (`_mapping_analysis_cache/`). The cache signature hashes the algorithm version, preprocessing settings, sideband geometry, and line selection rules to ensure instantaneous re-loading without redundant re-computation.
- **Vectorized Matrix Computations**: Peak net-area integration, local continuum sideband estimation, empirical null distributions, and robust Z-score computations are fully vectorized using NumPy.
- **Asynchronous Persistence Architecture**: Bounded background save queue with ordered single-writer persistence, backpressure, failure propagation, and drain-before-finalization behavior to prevent UI freezes during continuous high-frequency data acquisition.
