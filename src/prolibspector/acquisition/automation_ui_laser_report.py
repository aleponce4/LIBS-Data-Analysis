"""Laser controller diagnostic report builder for automated acquisition UI."""

from __future__ import annotations

from datetime import datetime

from prolibspector.acquisition.automation import (
    LASER_PROFILE_MONPORT_NDYAG_RELAY,
    effective_bed_area,
)


GRBL_LIMIT_PINS = frozenset({"X", "Y", "Z"})
GRBL_CONTROL_PINS = frozenset({"D", "H", "R", "S"})


def grbl_status_active_pins(status) -> frozenset[str]:
    pins = getattr(status, "active_pins", frozenset()) or frozenset()
    return frozenset(str(pin).upper() for pin in pins)


def grbl_status_line_for_report(status) -> str:
    if status is None:
        return "raw=<none>"
    raw = str(getattr(status, "raw", "") or "").strip() or "<no raw status>"
    state = str(getattr(status, "state", "") or "unknown")
    pins = "".join(sorted(grbl_status_active_pins(status))) or "none"
    kind = str(getattr(status, "coordinate_kind", "") or "none")

    def _fmt_position(values) -> str:
        x, y, z = values
        if x is None or y is None:
            return "unknown"
        return f"X{float(x):.3f} Y{float(y):.3f} Z{float(z or 0.0):.3f}"

    reported = _fmt_position((getattr(status, "x", None), getattr(status, "y", None), getattr(status, "z", None)))
    work = _fmt_position(getattr(status, "work_position", (None, None, None)))
    machine = _fmt_position(getattr(status, "machine_position", (None, None, None)))
    wco = _fmt_position(getattr(status, "wco", (None, None, None)))
    return (
        f"raw={raw}\n"
        f"state={state}\n"
        f"pins={pins}\n"
        f"coordinate mode={kind}\n"
        f"reported position={reported}\n"
        f"derived work position={work}\n"
        f"machine position={machine}\n"
        f"WCO={wco}"
    )


class AutomationLaserControllerReportBuilder:
    """Build the no-fire controller-readiness report shown from laser utilities."""

    def __init__(self, app, profile_labels: dict[str, str]):
        self.app = app
        self.profile_labels = profile_labels

    def build(self) -> dict[str, str]:
        app = self.app
        profile = app._laser_profile_value()
        profile_label = self.profile_labels.get(profile, profile)
        manual_p_override = app._grbl_p_input_nonblocking_enabled()
        effective_p_override = app._effective_grbl_p_input_nonblocking_enabled()
        p_policy = (
            "nonblocking by Monport relay profile"
            if profile == LASER_PROFILE_MONPORT_NDYAG_RELAY
            else ("nonblocking by manual override" if manual_p_override else "blocking for firing")
        )
        lines = [
            f"Laser Controller Check - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Port: {getattr(app.laser, 'port', 'unknown')}",
            f"Baud: {getattr(app.laser, 'baudrate', app.laser_baud_var.get())}",
            f"Laser profile: {profile_label}",
            f"P input override: {'ON' if manual_p_override else 'OFF'}",
            f"Effective P input policy: {p_policy}",
            f"Reconnect required: {'yes' if bool(getattr(app.laser, 'reconnect_required', False)) else 'no'}",
            "No homing, unlock, motion, or firing command is sent by this check.",
            "",
            "[Initial GRBL status]",
        ]
        reconnect_required = bool(getattr(app.laser, "reconnect_required", False))
        reconnect_reason = str(getattr(app.laser, "reconnect_required_reason", "") or "")
        if reconnect_reason:
            lines.append(f"Reconnect reason: {reconnect_reason}")

        if reconnect_required:
            status = getattr(app.laser, "last_status", None)
            lines.append("status read: skipped - reconnect required; using last known status.")
        else:
            try:
                status = app.laser.poll_status(timeout_s=2.0)
            except Exception as exc:
                lines.extend([
                    f"status read: FAILED - {exc}",
                    "",
                    "[Summary]",
                    "Motion ready: no - status could not be read.",
                    "Guarded firing ready: no - status could not be read.",
                ])
                return {
                    "summary": "Controller check failed: no GRBL status.",
                    "diagnostic_log": "\n".join(lines),
                }

        lines.append(grbl_status_line_for_report(status))
        state = str(getattr(status, "state", "") or "").lower()
        pins = grbl_status_active_pins(status)
        limit_pins = sorted(GRBL_LIMIT_PINS.intersection(pins))
        controller_pins = sorted(GRBL_CONTROL_PINS.intersection(pins))
        p_active = "P" in pins
        blocking_state = bool(state.startswith(("alarm", "door", "hold")))
        idle = state == "idle"
        homing_needed = state.startswith("alarm")
        work_position_ready = bool(getattr(status, "work_position_available", False))

        lines.extend([
            "",
            "[Readiness]",
            f"Homing needed: {'yes' if homing_needed else 'no'}",
            f"Reliable work position: {'yes' if work_position_ready else 'no'}",
        ])
        if reconnect_required:
            lines.append("Reconnect is required before more motion or firing because the command stream is unsafe.")
        if homing_needed:
            lines.append("Automatic runs will try $H first; if locked, they will try $X once, then $H.")
        if state.startswith("door"):
            lines.append("Door state is active; close/clear the door input before motion or firing.")
        if state.startswith("hold"):
            lines.append("Hold state is active; release the controller hold before motion or firing.")
        if limit_pins:
            lines.append(f"Limit input(s) active: {', '.join(limit_pins)}.")
        if controller_pins:
            lines.append(f"Controller input(s) active: {', '.join(controller_pins)}.")
        if p_active:
            if profile == LASER_PROFILE_MONPORT_NDYAG_RELAY:
                lines.append(
                    "P input active: motion and relay-profile firing can continue because this Monport profile treats "
                    "only P as the known false CO2 temperature signal. Do not use this profile if P is a safety interlock."
                )
            else:
                lines.append(
                    "P input active: motion can continue. Guarded firing blocks unless the machine-profile override is ON; "
                    "enable it only after hardware testing confirms P is not a cover, water, or other safety interlock."
                )

        lines.extend(["", "[Controller settings]"])
        settings = {}
        read_settings = getattr(app.laser, "read_grbl_settings", None)
        if callable(read_settings) and not reconnect_required:
            try:
                settings = read_settings({"30", "32", "130", "131"})
            except Exception as exc:
                lines.append(f"settings read: FAILED - {exc}")
        elif reconnect_required:
            lines.append("settings read: skipped - reconnect required.")
        else:
            lines.append("settings read: unavailable.")
        for key, label in (("30", "S max"), ("32", "laser mode"), ("130", "X travel"), ("131", "Y travel")):
            value = settings.get(key)
            lines.append(f"${key} {label}: {float(value):g}" if value is not None else f"${key} {label}: unknown")

        setting_block_reason = ""
        configured_s_max = None
        try:
            configured_s_max = int(app._float_var(app.s_max_var, "S max"))
        except Exception:
            configured_s_max = None
        if configured_s_max is not None and settings.get("30") is not None:
            if abs(float(settings["30"]) - float(configured_s_max)) > 1e-6:
                if profile == LASER_PROFILE_MONPORT_NDYAG_RELAY:
                    lines.append(
                        f"settings note: relay profile will use controller $30 ({float(settings['30']):g}) "
                        f"as the 100% trigger scale instead of configured S max ({configured_s_max})."
                    )
                else:
                    setting_block_reason = f"$30 ({float(settings['30']):g}) differs from configured S max ({configured_s_max})"
                    lines.append(f"settings issue: {setting_block_reason}.")
        if profile == LASER_PROFILE_MONPORT_NDYAG_RELAY and settings.get("32") is not None:
            if abs(float(settings["32"]) - 1.0) > 1e-6:
                setting_block_reason = f"$32 ({float(settings['32']):g}) is not laser mode 1 for Monport relay firing"
                lines.append(f"settings issue: {setting_block_reason}.")
        config = app.automation_config
        if config is not None:
            bed = effective_bed_area(config)
            x_limit = settings.get("130")
            if not setting_block_reason and x_limit is not None and (
                bed.x_min_mm < -1e-6 or bed.x_max_mm > float(x_limit) + 1e-6
            ):
                setting_block_reason = (
                    f"plan X range {bed.x_min_mm:g} to {bed.x_max_mm:g} mm exceeds $130 {float(x_limit):g} mm"
                )
                lines.append(f"settings issue: {setting_block_reason}.")
            y_limit = settings.get("131")
            if not setting_block_reason and y_limit is not None and (
                bed.y_min_mm < -1e-6 or bed.y_max_mm > float(y_limit) + 1e-6
            ):
                setting_block_reason = (
                    f"plan Y range {bed.y_min_mm:g} to {bed.y_max_mm:g} mm exceeds $131 {float(y_limit):g} mm"
                )
                lines.append(f"settings issue: {setting_block_reason}.")

        command_check_ok = False
        if not reconnect_required and not blocking_state and idle and not limit_pins and not controller_pins:
            lines.append("")
            lines.append("[No-fire command check]")
            send_command = getattr(app.laser, "send_command", None)
            if callable(send_command):
                try:
                    for command in ("G90", "G21", "M5"):
                        send_command(command)
                        lines.append(f"{command}: ok")
                    command_check_ok = True
                except Exception as exc:
                    lines.append(f"command check: FAILED - {exc}")
                    if "error:9" in str(exc).lower() or "locked" in str(exc).lower():
                        lines.append("Controller appears locked; automatic preparation should home/unlock before motion.")
            else:
                lines.append("command check: unavailable - controller has no send_command method.")
        else:
            lines.extend([
                "",
                "[No-fire command check]",
                "skipped: controller is not idle/ready for modal command verification.",
            ])

        motion_ready = bool(
            not reconnect_required
            and not blocking_state
            and idle
            and not limit_pins
            and not controller_pins
            and work_position_ready
            and not setting_block_reason
            and command_check_ok
        )
        firing_ready = motion_ready and (not p_active or effective_p_override)
        firing_reason = "ready"
        if not firing_ready:
            if reconnect_required:
                firing_reason = "blocked because reconnect is required"
            elif blocking_state:
                firing_reason = f"blocked by GRBL state {getattr(status, 'state', 'unknown')}"
            elif controller_pins:
                firing_reason = f"blocked by controller input(s) {', '.join(controller_pins)}"
            elif limit_pins:
                firing_reason = f"blocked by limit input(s) {', '.join(limit_pins)}"
            elif not work_position_ready:
                firing_reason = "blocked because reliable work coordinates are unavailable"
            elif setting_block_reason:
                firing_reason = f"blocked by controller settings: {setting_block_reason}"
            elif p_active and not effective_p_override:
                firing_reason = "blocked by active P input; enable the override only if P is proven non-safety on this machine"
            elif not idle:
                firing_reason = f"blocked because controller is {getattr(status, 'state', 'unknown')}"
            elif not command_check_ok:
                firing_reason = "blocked because modal/off command check did not pass"

        lines.extend([
            "",
            "[Summary]",
            f"Motion ready: {'yes' if motion_ready else 'no'}",
            f"Guarded firing ready: {'yes' if firing_ready else 'no'} - {firing_reason}",
        ])
        summary = "Controller check complete: "
        summary += "firing ready." if firing_ready else f"firing blocked ({firing_reason})."
        return {
            "summary": summary,
            "diagnostic_log": "\n".join(lines),
        }
