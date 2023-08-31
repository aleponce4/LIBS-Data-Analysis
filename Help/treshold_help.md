# Help Section: Set Threshold and Round-off Error Tolerance

In this section, you can set the parameters that greatly impact the software's interpretation of the LIBS data. The tool gives you fine-grained control over how the program identifies and labels peaks in your spectral data. It consists of three sliders and three buttons for specific functionalities.

1.  Change Font Size: This slider controls the size of the text used in labels, helping you to tailor the visibility of information according to your preference.
2.  Intensity Threshold: This slider determines the intensity above which the program identifies and works on the spectral lines. The intensity threshold is relative to the intensity of the highest peak in the spectrum, with 100% equating to this peak intensity.

Moving the threshold higher will result in the software only processing spectral lines that are of high intensity, thus possibly reducing noise and focusing on the most significant elements in the sample. This could be particularly useful when the spectrometer's resolution is high, as it would help eliminate minor peaks that might be an artifact of the high-resolution data.

Conversely, lowering the threshold allows the software to consider smaller peaks, which could be beneficial when working with lower resolution data where important spectral lines might not be as intense. However, this might also increase the risk of misidentifying noise as meaningful data.

1.  Round-off Error Tolerance: This slider defines how precise the software is when comparing the peaks for matches in the database. It allows for a range from rounding database and peaks to no decimals, to making the comparison with a tolerance of 0.001.

A higher round-off error tolerance (i.e., less precision) can be useful when dealing with lower resolution spectrometers where the exact location of peaks might not be perfectly accurate. A lower tolerance (more precision) is beneficial when using high-resolution spectrometers that provide very accurate peak locations, allowing for more precise matching against the database.

### Additionally, there are three buttons for further control:

-   **Hide Unlabeled Peaks**: This button enables you to simplify your view by hiding all peaks that have not been labeled. This could help in focusing on the labeled peaks and reducing visual clutter.
-   **Delete All Labels**: This button allows you to remove all labels from the spectral lines in one go. Use this when you want to start the labeling process afresh.
-   **Reduce Label Overlap**: If your spectrum is dense with peaks and labels, clicking this button will adjust the labels to minimize their overlap, making them easier to read and understand.

Remember, adjusting these settings can significantly impact the results obtained from the LIBS data analysis. It's important to understand your spectrometer's specifications and the nature of your samples to fine-tune these parameters effectively.
