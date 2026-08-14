"""Drive the real GRBL controller over an emulated serial port.

These exercise `GrblLaserController` itself -- the command framing, the ack
ledger, the status parser -- rather than the in-process
`SimulatedGrblLaserController`, which bypasses all of it. Everything here runs
against `GrblSerialSimulator`, so the bytes on the wire are the same ones a real
controller would see.
"""

from __future__ import annotations

import pytest

from prolibspector.hardware.grbl_laser import GrblLaserController, GrblLaserError
from prolibspector.hardware.grbl_simulator import GrblSerialSimulator, SimulatorFaults


def make_controller(faults: SimulatorFaults | None = None) -> GrblLaserController:
    """A connected controller talking to a fresh emulator.

    `startup_delay_s=0` skips the post-open settle a real board needs while its
    bootloader hands over; there is no bootloader here.
    """

    def factory(*args, **kwargs):
        return GrblSerialSimulator(*args, faults=faults, **kwargs)

    controller = GrblLaserController(
        "SIM", serial_factory=factory, startup_delay_s=0.0, timeout_s=0.5
    )
    controller.connect()
    return controller


def test_connect_reports_idle_and_reads_settings():
    controller = make_controller()
    try:
        settings = controller.read_grbl_settings({"30", "32", "130", "131"})
        # $32=1 is laser mode. If this ever reads 0 against real hardware the
        # beam stays on during rapids, so the value matters more than the parse.
        assert settings == {"30": 1000.0, "32": 1.0, "130": 300.0, "131": 200.0}
        assert controller.poll_status().state == "Idle"
    finally:
        controller.disconnect()


def test_homing_zeroes_work_position():
    controller = make_controller()
    try:
        controller.move_to(25.0, 15.0)
        assert controller.poll_status().work_position == (25.0, 15.0, 0.0)
        controller.home()
        assert controller.poll_status().work_position == (0.0, 0.0, 0.0)
    finally:
        controller.disconnect()


def test_absolute_move_and_relative_jog_use_different_distance_modes():
    controller = make_controller()
    try:
        controller.home()
        controller.move_to(10.0, 4.0, feed_mm_min=600.0)
        assert controller.poll_status().work_position == (10.0, 4.0, 0.0)

        # jog() sends `$J=G91 ...`, which is relative and must not reset the
        # modal G90 that move_to established.
        controller.jog(dx_mm=2.5, dy_mm=-1.0)
        assert controller.poll_status().work_position == (12.5, 3.0, 0.0)

        controller.move_to(1.0, 1.0)
        assert controller.poll_status().work_position == (1.0, 1.0, 0.0)
    finally:
        controller.disconnect()


def test_controller_starting_in_alarm_recovers_after_unlock():
    controller = make_controller(SimulatorFaults(start_in_alarm=True))
    try:
        assert controller.poll_status().state == "Alarm"
        controller.unlock()
        assert controller.poll_status().state == "Idle"
    finally:
        controller.disconnect()


def test_alarm_state_rejects_motion_until_unlocked():
    controller = make_controller(SimulatorFaults(start_in_alarm=True))
    try:
        with pytest.raises(GrblLaserError):
            controller.move_to(5.0, 5.0)
        controller.unlock()
        controller.move_to(5.0, 5.0)
        assert controller.poll_status().work_position == (5.0, 5.0, 0.0)
    finally:
        controller.disconnect()


def test_error_reply_surfaces_as_an_exception():
    controller = make_controller(SimulatorFaults(error_commands={"$H"}))
    try:
        with pytest.raises(GrblLaserError):
            controller.home()
    finally:
        controller.disconnect()


def test_port_disappearing_mid_session_is_reported():
    """A yanked USB cable surfaces as GrblLaserError, not a raw OSError."""
    simulators: list[GrblSerialSimulator] = []

    def factory(*args, **kwargs):
        sim = GrblSerialSimulator(*args, **kwargs)
        simulators.append(sim)
        return sim

    controller = GrblLaserController(
        "SIM", serial_factory=factory, startup_delay_s=0.0, timeout_s=0.5
    )
    controller.connect()
    simulators[0].faults.raise_on_write = OSError("device disconnected")
    with pytest.raises(GrblLaserError):
        controller.move_to(1.0, 1.0)


def test_simulated_controller_declares_itself_simulated():
    """The flag acquisition code gates motion-safety checks on.

    A real controller driving an emulated port is still 'real' -- the safety
    validation should run -- so this must stay False here.
    """
    controller = make_controller()
    try:
        assert controller.is_simulated is False
    finally:
        controller.disconnect()
