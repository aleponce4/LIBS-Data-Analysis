# Technical Gallery & Extended Architecture

This document provides supplementary visual case studies, file format specifications, caching internals, and hardware integration details for the LIBS Spectroscopy Workbench.

The figures below are illustrative. They were produced from measurement runs on the private edition's hardware; the source spectra and the plotting scripts are not part of this repository, so the numbers quoted in the captions cannot be regenerated from what is published here. They are shown to document what the interface and analysis output look like, not as benchmark results.

---

## Supplementary Visual Demonstrations

### 1. Interactive Periodic Table Element Selector

![Interactive Periodic Table Selector](img/gui_periodic_table.png)

*Interactive Periodic Table dialog interface enabling element selection, database selection, and ionization state filtering (I, II, III) for NIST line overlay matching.*

---

### 2. Single-Point Annotated LIBS Emission Spectrum

![Annotated LIBS Emission Spectrum](img/ceramic_tile_annotated_spectrum.png)

*Annotated net-intensity spectrum from a ceramic tile sample point across 200-1000 nm, identifying key neutral and ionic emission lines for Cu I (325/327 nm), Co I (358/361 nm), Pb I (368/406 nm), Ca II (393/397 nm), Na I (589 nm), and K I (766/769 nm).*

---

Physical instrument drivers are maintained separately and are not part of this edition.

---

## Supported File Input & Export Formats

- **Input Formats**:
  - Legacy & standard single-shot / average spectral CSV files.
  - Multi-column spatial mapping index manifests (`_mapping_grid_index.csv`).
  - Binary memory-mapped intensity arrays (`_mapping_spectrum_store/intensities.npy`).
  - JSON run configuration and calibration snapshots (`_mapping_grid_manifest.json`).

- **Export & Storage Formats**:
  - **CSV Spectral Files**: Standard 2-column (wavelength, intensity) and multi-point CSV exports.
  - **High-Density Binary Stores**: Chunked binary arrays (`.npy`) for rapid disk write/read during high-speed raster scanning.
  - **Reproducibility Manifests**: JSON metadata logs containing spectrometer settings and reported identity, operator-entered run metadata such as laser energy, and timestamped spatial coordinates. The signed-manifest format, which adds cryptographic data checksums, is part of the private edition; the public manifests are plain JSON and are not signed.
  - **Graphics & Figures**: Export to high-resolution PNG, PDF, TIFF, and SVG formats via the embedded Matplotlib canvas.

---

## High-Performance Analysis, Matrix Caching & Memory Architecture

Processing large-scale 2D LIBS spatial maps (thousands of grid points, multiple shot replicates per point, multi-element spectral line scoring) requires optimized data structures and memory management:

- **Binary Memory Mapping (`.npy`)**: Large mapping datasets use memory-mapped binary array stores (`intensities.npy`) to avoid the I/O overhead of parsing gigabytes of CSV text files.
- **Content-Addressed Deterministic Caching**: Preprocessing results, candidate line scans, and fused multi-element intensity grids are cached under cryptographic signatures (`_mapping_analysis_cache/`). The cache signature hashes the algorithm version, preprocessing settings, sideband geometry, and line selection rules to ensure instantaneous re-loading without redundant re-computation.
- **Vectorized Matrix Computations**: Peak net-area integration, local continuum sideband estimation, empirical null distributions, and robust Z-score computations are fully vectorized using NumPy.
- **Asynchronous Persistence Architecture**: Bounded background save queue with ordered single-writer persistence, backpressure, failure propagation, and drain-before-finalization behavior to prevent UI freezes during continuous high-frequency data acquisition.
