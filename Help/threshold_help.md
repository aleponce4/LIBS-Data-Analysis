# Help Section: Label Peaks

In this section, you can set the parameters that greatly impact the software's interpretation of the LIBS data. The tool gives you fine-grained control over how the program identifies and labels peaks in your spectral data. It consists of four sliders and three buttons for specific functionalities.

**1.  Change Font Size**: This slider controls the size of the text used in labels, helping you to tailor the visibility of information according to your preference.

**2.  Intensity Threshold**: This slider determines the intensity above which the program identifies and works on the spectral lines. The intensity threshold is relative to the intensity of the highest peak in the spectrum, with 100% equating to this peak intensity.

Moving the threshold higher will result in the software only processing spectral lines that are of high intensity, thus possibly reducing noise and focusing on the most significant elements in the sample. This could be particularly useful when the spectrometer's resolution is high, as it would help eliminate minor peaks that might be an artifact of the high-resolution data.

Conversely, lowering the threshold allows the software to consider smaller peaks, which could be beneficial when working with lower resolution data where important spectral lines might not be as intense. However, this might also increase the risk of misidentifying noise as meaningful data.

**3.  Prominence Filter**: This slider adds an additional filter that removes peaks based on their prominence relative to their local neighborhood. A peak must stand out by the specified percentage above its local baseline to be retained. For example, 15% means a peak must be at least 15% higher than the average intensity of nearby points.

This filter is particularly effective at removing small noise peaks that might pass the absolute intensity threshold but are not significant compared to their immediate surroundings. Set to 0% to disable this filter and use only the intensity threshold. Higher values (20-30%) provide more aggressive noise filtering, while lower values (5-15%) provide gentle noise reduction while preserving weak but significant peaks. For extremely noisy data, values up to 100-200% can be used to ensure only the most prominent peaks are retained.

**4.  Tolerance (nm)**: This slider defines how close (in nanometres) a detected peak must be to a database line for the peak to be tagged with that element and ionization level.

A wider tolerance can be useful when dealing with lower resolution spectrometers where the exact location of peaks might not be perfectly accurate. A tighter tolerance is beneficial when using high-resolution spectrometers that provide very accurate peak locations, allowing for more precise matching against the database.

### Additionally, there are three buttons for further control:

-   **Hide Unlabeled Peaks**: This button enables you to simplify your view by hiding all peaks that have not been labeled. This could help in focusing on the labeled peaks and reducing visual clutter.
-   **Delete Labels**: This button allows you to remove all labels from the spectral lines in one go. Use this when you want to start the labeling process afresh.
-   **Reduce Label Overlap**: If your spectrum is dense with peaks and labels, clicking this button will adjust the labels to minimize their overlap, making them easier to read and understand.

*Remember, adjusting these settings can significantly impact the results obtained from the LIBS data analysis. It's important to understand your spectrometer's specifications and the nature of your samples to fine-tune these parameters effectively.*

### Tips for Using the Prominence Filter:

- **Start with 15%**: This is a good default that removes most noise while preserving important peaks
- **Increase for noisy data**: Use 20-30% for very noisy spectra
- **Decrease for clean data**: Use 5-10% for high-quality spectra where you want to preserve weak peaks
- **Combine with intensity threshold**: Use both filters together for optimal results - the intensity threshold removes very small peaks, while the prominence filter removes local noise
- **Set to 0% to disable**: If you only want to use the intensity threshold, set the prominence filter to 0%
- **Extreme filtering (50-200%)**: For extremely noisy data or when you only want the most dominant peaks, use higher values. 100%+ means peaks must be at least double their local baseline
- **Fine-tuning approach**: Start low (15%), then gradually increase until unwanted noise disappears while keeping important spectral lines
