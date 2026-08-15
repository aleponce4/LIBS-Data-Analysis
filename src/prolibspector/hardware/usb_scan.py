"""USB bus enumeration for supported spectrometer brands.

Both vendor backends need to answer the same diagnostic question: is the device
visible to the operating system at all? That question is separate from whether
the vendor SDK can open it, and answering it separately is what turns "no
spectrometer found" into an actionable message — a device on the bus that
seabreeze cannot see is a driver-binding problem, whereas a device absent from
the bus is a cable or power problem.

Enumeration uses PowerShell on Windows and sysfs (falling back to pyusb) on
Linux, so neither path adds a runtime dependency.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: USB vendor IDs for the brands with a backend in this repository.
OCEAN_OPTICS_VID = "2457"   # 0x2457
THORLABS_VID = "1313"       # 0x1313

_TARGET_VIDS = {OCEAN_OPTICS_VID, THORLABS_VID}


def _brand_for_vid(vid: str) -> str:
    if vid.upper() == OCEAN_OPTICS_VID:
        return "ocean_optics"
    if vid.upper() == THORLABS_VID:
        return "thorlabs"
    return "unknown"


def scan_usb_spectrometers() -> list[dict]:
    """Return the spectrometer-class devices currently on the USB bus.

    Each entry carries at least ``vid``, ``pid``, ``description``, ``driver``,
    ``status``, ``instance_id`` and ``brand``; the Windows path adds the driver
    provenance fields the diagnostics dialog shows. Returns an empty list when
    platform enumeration is unavailable rather than raising, because this runs
    inside a diagnostics report that must always render.
    """
    if platform.system() == "Linux":
        return _scan_linux()
    return _scan_windows()


def _scan_linux() -> list[dict]:
    devices: list[dict] = []

    sysfs_root = Path("/sys/bus/usb/devices")
    if sysfs_root.is_dir():
        for entry in sysfs_root.iterdir():
            vendor_path = entry / "idVendor"
            product_path = entry / "idProduct"
            if not vendor_path.is_file() or not product_path.is_file():
                continue
            try:
                vid = vendor_path.read_text(encoding="utf-8").strip().upper()
                pid = product_path.read_text(encoding="utf-8").strip().upper()
            except OSError:
                continue
            if vid not in _TARGET_VIDS:
                continue

            brand = _brand_for_vid(vid)
            devices.append({
                "vid": vid,
                "pid": pid,
                "description": _linux_usb_description(entry, brand),
                "driver": "usbfs/sysfs",
                "status": "Visible",
                "instance_id": entry.name,
                "brand": brand,
            })
        return devices

    try:
        import usb.core
    except Exception as exc:
        logger.debug("Linux USB scan unavailable: %s", exc)
        return []

    try:
        usb_devices = usb.core.find(find_all=True)
    except Exception as exc:
        logger.debug("Linux pyusb scan failed: %s", exc)
        return []

    for dev in usb_devices or []:
        vid = f"{int(dev.idVendor):04X}"
        pid = f"{int(dev.idProduct):04X}"
        if vid not in _TARGET_VIDS:
            continue
        brand = _brand_for_vid(vid)
        devices.append({
            "vid": vid,
            "pid": pid,
            "description": f"{brand} USB device",
            "driver": "pyusb",
            "status": "Visible",
            "instance_id": f"bus={getattr(dev, 'bus', '?')} address={getattr(dev, 'address', '?')}",
            "brand": brand,
        })

    return devices


def _linux_usb_description(entry: Path, brand: str) -> str:
    parts = []
    for file_name in ("manufacturer", "product", "serial"):
        try:
            value = (entry / file_name).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            parts.append(value)
    return " - ".join(parts) or f"{brand} USB device"


def _scan_windows() -> list[dict]:
    # Narrow to the supported VIDs inside PowerShell before the per-device
    # driver property lookups, which are the slow part. The Python-side VID
    # check below stays as a second filter on the returned JSON.
    ps_script = (
        "Get-PnpDevice -PresentOnly -Class USB,HIDClass,Ports,USBDevice -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -match 'USB\\\\VID_(2457|1313)' } | "
        "Select-Object InstanceId, FriendlyName, Status, "
        "@{N='DriverDescription';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_DriverDesc' -ErrorAction SilentlyContinue).Data}}, "
        "@{N='DriverService';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data}}, "
        "@{N='DriverProvider';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_DriverProvider' -ErrorAction SilentlyContinue).Data}}, "
        "@{N='DriverInf';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data}}, "
        "@{N='DriverVersion';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction SilentlyContinue).Data}} | "
        "ConvertTo-Json -Compress"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug("USB scan returned no data (rc=%s)", result.returncode)
            return []

        # PowerShell emits a bare object rather than a one-element array when
        # exactly one device matches.
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict):
            data = [data]

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
        logger.debug("USB scan failed: %s", exc)
        return []

    devices = []
    for entry in data:
        instance_id = entry.get("InstanceId", "")
        vid_match = re.search(r"VID_(\w{4})", instance_id, re.IGNORECASE)
        pid_match = re.search(r"PID_(\w{4})", instance_id, re.IGNORECASE)
        if not vid_match:
            continue
        vid = vid_match.group(1).upper()
        pid = pid_match.group(1).upper() if pid_match else "????"
        if vid not in _TARGET_VIDS:
            continue

        driver_description = entry.get("DriverDescription") or ""
        driver_service = entry.get("DriverService") or ""

        devices.append({
            "vid": vid,
            "pid": pid,
            "description": entry.get("FriendlyName") or "Unknown device",
            "driver": driver_service or driver_description or "(no driver)",
            "driver_description": driver_description,
            "driver_service": driver_service,
            "driver_provider": entry.get("DriverProvider") or "",
            "driver_inf": entry.get("DriverInf") or "",
            "driver_version": entry.get("DriverVersion") or "",
            "status": entry.get("Status") or "Unknown",
            "instance_id": instance_id,
            "brand": _brand_for_vid(vid),
        })

    return devices


def driver_ok_for_backend(driver_name: str, backend: str) -> tuple[bool, str]:
    """Check a Windows USB driver against a seabreeze backend's requirement.

    Returns ``(is_ok, advice)``. The two backends want different drivers bound
    to the same device: the C wrapper needs WinUSB, while the pure-Python one
    goes through libusb and accepts either. Getting this wrong is the single
    most common reason a device is on the bus but invisible to seabreeze, so
    the advice string names the fix rather than the symptom.
    """
    if platform.system() != "Windows":
        return True, ""

    driver = (driver_name or "").lower()
    if backend == "cseabreeze":
        if "winusb" in driver:
            return True, ""
        if "libusb" in driver or "libusbk" in driver:
            return False, (
                "cseabreeze needs the WinUSB driver.\n"
                "Run  seabreeze_os_setup  from an admin terminal to switch it."
            )
        return False, f"Unknown driver '{driver_name}' — cseabreeze may need WinUSB."
    if backend == "pyseabreeze":
        if "winusb" in driver or "libusb" in driver or "libusbk" in driver:
            return True, ""
        return False, f"Unknown driver '{driver_name}' — pyseabreeze may need WinUSB or libusb."
    return True, ""


def linux_usb_runtime_status() -> dict:
    """Report whether Linux has the pyusb/libusb pieces seabreeze needs."""
    status = {
        "pyusb_installed": False,
        "libusb_found": False,
        "libusb_name": None,
    }
    try:
        import usb.core  # noqa: F401

        status["pyusb_installed"] = True
    except Exception:
        pass

    try:
        from ctypes.util import find_library

        libusb_name = find_library("usb-1.0")
        status["libusb_found"] = bool(libusb_name)
        status["libusb_name"] = libusb_name
    except Exception:
        pass

    return status


__all__ = [
    "OCEAN_OPTICS_VID",
    "THORLABS_VID",
    "driver_ok_for_backend",
    "linux_usb_runtime_status",
    "scan_usb_spectrometers",
]
