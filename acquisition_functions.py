# acquisition_functions.py
import usb.core
import usb.util
import seabreeze

seabreeze.use('pyseabreeze')

# Import seabreeze.spectrometers after calling seabreeze.use
import seabreeze.spectrometers as sb

# Initialize the spectrometer
def initialize_spectrometer(app):
    devices = sb.list_devices()
    if devices:
        spectrometer = sb.Spectrometer(devices[0])
        return spectrometer
    else:
        raise Exception("No spectrometer found")


def initialize_trigger():
    # Initialize the USB trigger
    # Replace the idVendor and idProduct with your device's Vendor ID and Product ID
    dev = usb.core.find(idVendor=0x1234, idProduct=0x5678)
    if dev is None:
        raise ValueError('Device not found')
    return dev

def handle_external_trigger(dev):
    # Handle an external trigger event
    # This will be specific to your hardware setup
    pass

def update_live_graph():
    # Update the graph with live data
    pass
