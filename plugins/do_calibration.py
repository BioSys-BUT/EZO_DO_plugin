# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import typing as t
from datetime import datetime

from pioreactor import structs
from pioreactor.calibrations.registry import CalibrationProtocol
from pioreactor.calibrations.session_flow import SessionStep
from pioreactor.calibrations.session_flow import StepRegistry
from pioreactor.calibrations.session_flow import fields
from pioreactor.calibrations.session_flow import steps
from pioreactor.calibrations.structured_session import CalibrationSession
from pioreactor.calibrations.structured_session import utc_iso_timestamp
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.whoami import get_unit_name

from atlas_ezo_do import AtlasEzoDO

logger = logging.getLogger("do_calibration")


class DoEzoCalibration(structs.CalibrationBase, kw_only=True, tag="do_ezo"):
    """
    Stores metadata for an Atlas EZO-DO calibration event.

    The EZO-DO board performs calibration internally; this record is primarily
    for traceability/exportability (which points were used, timestamps, etc).
    """

    x: str = "DO (mg/L)"
    y: str = "DO (mg/L)"
    points_used: list[str]
    ezo_calibration_status: str
    notes: str = ""


def _new_calibration_name() -> str:
    return f"ezo_do_{utc_iso_timestamp().replace(':', '').replace('-', '')}"


def _poly_identity() -> structs.PolyFitCoefficients:
    # y = 1*x + 0
    return structs.PolyFitCoefficients([1.0, 0.0])


def _build_chart_from_points(points: list[dict[str, float]]) -> dict[str, t.Any]:
    return {
        "title": "EZO-DO calibration checkpoints",
        "x_label": "Expected DO (mg/L)",
        "y_label": "Measured DO (mg/L)",
        "series": [
            {
                "id": "do",
                "label": "Measurements",
                "points": points,
            }
        ],
    }


def _ensure_probe_i2c(probe: AtlasEzoDO, *, desired_address: int) -> None:
    # Best-effort guard so the calibration commands don’t fail mysteriously.
    probe.ensure_i2c_mode_and_address(desired_address=desired_address)


def _exec_do_cmd(ctx, *, cmd: str, timeout_s: float) -> dict[str, t.Any]:
    """
    Execute a raw EZO-DO command.

    UI sessions delegate to the registered Huey calibration action via ctx.executor.
    CLI/non-UI calls talk directly to the probe.
    """
    if getattr(ctx, "executor", None) is not None and getattr(ctx, "mode", None) == "ui":
        # Retry on transient pending/no-data codes.
        last_status = 0
        last_body: t.Any = None
        for attempt in range(3):
            payload = ctx.executor(
                "do_ezo_cmd", {"cmd": cmd, "timeout_s": float(timeout_s)}
            )
            if isinstance(payload, dict):
                last_status = int(payload.get("status_code", 0))
                last_body = payload.get("body")
            logger.info(
                "do_calibration: exec_do_cmd (ui) attempt=%d cmd=%s status_code=%s",
                attempt + 1,
                cmd,
                last_status,
            )
            if last_status == 1:
                return {"status_code": last_status, "body": last_body}
        return {"status_code": last_status, "body": last_body}

    # CLI / non-UI fallback.
    desired_address = AtlasEzoDO.DEFAULT_I2C_ADDRESS
    probe = AtlasEzoDO.from_config()
    _ensure_probe_i2c(probe, desired_address=desired_address)

    try:
        resp = probe.query(cmd, timeout_s=float(timeout_s))
        return {"status_code": resp.status_code, "body": resp.body}
    except Exception as exc:
        logger.exception("do_calibration: exec_do_cmd error cmd=%s", cmd)
        raise RuntimeError(f"EZO-DO command '{cmd}' failed: {exc}") from exc


def _exec_do_read(ctx, *, samples: int) -> float:
    """
    Read DO (mg/L) from the probe.

    In UI mode, delegates to ctx.executor -> do_ezo_read action.
    """
    if getattr(ctx, "executor", None) is not None and getattr(ctx, "mode", None) == "ui":
        last_error = ""
        for attempt in range(3):
            payload = ctx.executor("do_ezo_read", {"samples": int(samples)})
            if not isinstance(payload, dict):
                last_error = "invalid payload"
                continue

            if "DO" in payload:
                return float(payload["DO"])

            status = int(payload.get("status_code", 0))
            body = str(payload.get("body", ""))
            last_error = f"status={status} body={body!r}"

            # 254/255: pending/no data; retry.
            if status not in (254, 255):
                break

        raise RuntimeError(f"EZO-DO read failed: {last_error or 'unknown error'}")

    desired_address = AtlasEzoDO.DEFAULT_I2C_ADDRESS
    probe = AtlasEzoDO.from_config()
    _ensure_probe_i2c(probe, desired_address=desired_address)
    return float(probe.read_do(samples=int(samples)))


def _register_do_calibration_actions() -> None:
    """
    Register calibration actions for UI sessions (Huey executor).

    This must run at import-time so the UI can call ctx.executor().
    """
    from pioreactor.web.config import huey
    from pioreactor.web.tasks import register_calibration_action

    @huey.task()
    def do_ezo_cmd(cmd: str, timeout_s: float = 1.5) -> dict[str, t.Any]:
        probe = AtlasEzoDO.from_config()
        probe.ensure_i2c_mode_and_address(desired_address=AtlasEzoDO.DEFAULT_I2C_ADDRESS)
        resp = probe.query(cmd, timeout_s=float(timeout_s))
        return {"status_code": resp.status_code, "body": resp.body}

    @huey.task()
    def do_ezo_read(samples: int = 2) -> dict[str, t.Any]:
        probe = AtlasEzoDO.from_config()
        probe.ensure_i2c_mode_and_address(desired_address=AtlasEzoDO.DEFAULT_I2C_ADDRESS)

        values: list[float] = []
        for _ in range(int(samples)):
            resp = probe.query("R", timeout_s=1.5)
            if not resp.ok:
                # Return error-like payload so the session can retry cleanly.
                return {"status_code": resp.status_code, "body": resp.body}
            values.append(float(resp.body))
        return {"DO": sum(values) / len(values)}

    register_calibration_action(
        "do_ezo_cmd",
        lambda payload: (
            do_ezo_cmd(str(payload["cmd"]), float(payload.get("timeout_s", 1.5))),
            "EZO-DO command",
            (lambda result: result if isinstance(result, dict) else {}),
        ),
    )
    register_calibration_action(
        "do_ezo_read",
        lambda payload: (
            do_ezo_read(int(payload.get("samples", 2))),
            "EZO-DO read",
            (lambda result: result if isinstance(result, dict) else {}),
        ),
    )


try:
    _register_do_calibration_actions()
except Exception:
    # Non-web contexts (or worker import issues) can skip registration.
    pass


class Intro(SessionStep):
    step_id = "intro"

    def render(self, ctx) -> structs.CalibrationStep:
        body = "\n".join(
            [
                "This guided protocol calibrates an Atlas Scientific EZO-DO dissolved oxygen sensor using its built-in calibration modes.",
                "",
                "Before you start:",
                "- Stop any running DO tracking job (`do_reading`) on this unit.",
                "- For 'Calibrate to air', expose the probe to air.",
                "- For 'Calibrate to zero', use a 0 dissolved oxygen calibration solution (optional point).",
                "",
                "Wait until the readings are stable on the main DO chart (if enabled), then press Continue.",
                "",
                "Press Continue to configure the protocol.",
            ]
        )
        return steps.info("DO calibration (EZO-DO)", body)

    def advance(self, ctx):
        logger.info("do_calibration: Intro.advance -> Configure")
        return Configure()


class Configure(SessionStep):
    step_id = "configure"

    def render(self, ctx) -> structs.CalibrationStep:
        return steps.form(
            "Protocol settings",
            "Choose whether to run 2-point calibration (air + optional zero).",
            [
                fields.bool("include_zero_point", label="Include 0 mg/L step (Cal,0)", default=True),
                fields.float(
                    "timeout_s",
                    label="Command timeout (seconds)",
                    minimum=0.5,
                    maximum=20.0,
                    default=1.5,
                ),
                fields.int(
                    "read_samples",
                    label="Samples per checkpoint",
                    minimum=1,
                    maximum=10,
                    default=2,
                ),
                fields.float(
                    "span_expected_do",
                    label="Expected DO in air (mg/L)",
                    minimum=0.0,
                    maximum=30.0,
                    default=8.26,
                ),
            ],
        )

    def advance(self, ctx):
        logger.info(
            "do_calibration: Configure.advance include_zero_point=%s",
            ctx.inputs.bool("include_zero_point", default=True),
        )
        ctx.data["include_zero_point"] = ctx.inputs.bool("include_zero_point", default=True)
        ctx.data["timeout_s"] = ctx.inputs.float("timeout_s", minimum=0.5, maximum=20.0, default=1.5)
        ctx.data["read_samples"] = ctx.inputs.int("read_samples", minimum=1, maximum=10, default=2)
        ctx.data["span_expected_do"] = ctx.inputs.float(
            "span_expected_do", minimum=0.0, maximum=30.0, default=8.26
        )
        ctx.data["points"] = []
        ctx.data["points_used"] = []
        return ClearExisting()


class ClearExisting(SessionStep):
    step_id = "clear_existing"

    def render(self, ctx) -> structs.CalibrationStep:
        return steps.action(
            "Clear existing calibration",
            "\n".join(
                [
                    "This will clear any existing calibration stored on the EZO-DO board.",
                    "Make sure you want to continue.",
                    "",
                    "Press Continue to clear calibration on the probe.",
                ]
            ),
        )

    def advance(self, ctx):
        timeout_s = float(ctx.data.get("timeout_s", 1.5))
        logger.info("do_calibration: ClearExisting.advance starting Cal,clear")
        result = _exec_do_cmd(ctx, cmd="Cal,clear", timeout_s=timeout_s)
        if int(result.get("status_code", 0)) != 1:
            logger.error("do_calibration: Cal,clear failed result=%s", result)
            raise ValueError(f"Cal,clear failed: {result}")
        return BufferAir()


class BufferAir(SessionStep):
    step_id = "buffer_air"

    def render(self, ctx) -> structs.CalibrationStep:
        return steps.action(
            "Calibrate to air (Cal)",
            "\n".join(
                [
                    "Expose the probe to air.",
                    "Wait for readings to stabilize on the main DO chart.",
                    "",
                    "Press Continue to calibrate to air (Cal).",
                ]
            ),
        )

    def advance(self, ctx):
        timeout_s = float(ctx.data.get("timeout_s", 1.5))
        samples = int(ctx.data.get("read_samples", 2))
        span_expected_do = float(ctx.data.get("span_expected_do", 8.26))

        logger.info("do_calibration: BufferAir.advance reading DO at air")
        measured = _exec_do_read(ctx, samples=samples)
        ctx.data["points"].append({"x": span_expected_do, "y": float(measured)})
        ctx.data["points_used"].append("air")

        logger.info("do_calibration: BufferAir.advance sending Cal")
        result = _exec_do_cmd(ctx, cmd="Cal", timeout_s=timeout_s)
        if int(result.get("status_code", 0)) != 1:
            logger.error("do_calibration: Cal failed result=%s", result)
            raise ValueError(f"Cal failed: {result}")
        if bool(ctx.data.get("include_zero_point", True)):
            return BufferZero()
        return Finalize()


class BufferZero(SessionStep):
    step_id = "buffer_zero"

    def render(self, ctx) -> structs.CalibrationStep:
        return steps.action(
            "Calibrate to zero (Cal,0)",
            "\n".join(
                [
                    "Place the probe in a 0 dissolved oxygen calibration solution (or as instructed by your supplier).",
                    "Remove bubbles and ensure the probe is fully immersed.",
                    "Wait for readings to stabilize on the main DO chart.",
                    "",
                    "Press Continue to calibrate to zero (Cal,0).",
                ]
            ),
        )

    def advance(self, ctx):
        timeout_s = float(ctx.data.get("timeout_s", 1.5))
        samples = int(ctx.data.get("read_samples", 2))

        logger.info("do_calibration: BufferZero.advance reading DO at zero")
        measured = _exec_do_read(ctx, samples=samples)
        ctx.data["points"].append({"x": 0.0, "y": float(measured)})
        ctx.data["points_used"].append("zero")

        logger.info("do_calibration: BufferZero.advance sending Cal,0")
        result = _exec_do_cmd(ctx, cmd="Cal,0", timeout_s=timeout_s)
        if int(result.get("status_code", 0)) != 1:
            logger.error("do_calibration: Cal,0 failed result=%s", result)
            raise ValueError(f"Cal,0 failed: {result}")
        return Finalize()


class Finalize(SessionStep):
    step_id = "finalize"

    def render(self, ctx) -> structs.CalibrationStep:
        step = steps.action(
            "Finalize and save",
            "Press Continue to verify EZO-DO calibration status and save a calibration record to Pioreactor.",
        )
        if ctx.data.get("points"):
            step.metadata = {"chart": _build_chart_from_points(ctx.data["points"])}
        return step

    def advance(self, ctx):
        timeout_s = float(ctx.data.get("timeout_s", 1.5))
        logger.info("do_calibration: Finalize.advance sending Cal,?")
        status_resp = _exec_do_cmd(ctx, cmd="Cal,?", timeout_s=timeout_s)
        if int(status_resp.get("status_code", 0)) != 1:
            logger.error("do_calibration: Cal,? failed result=%s", status_resp)
            raise ValueError(f"Cal,? failed: {status_resp}")

        status_body = str(status_resp.get("body", "")).strip()
        points: list[dict[str, float]] = list(ctx.data.get("points", []))
        xs = [float(p["x"]) for p in points]
        ys = [float(p["y"]) for p in points]

        unit = get_unit_name()
        created_at: datetime = current_utc_datetime()

        calibration = DoEzoCalibration(
            calibration_name=_new_calibration_name(),
            calibrated_on_pioreactor_unit=unit,
            created_at=created_at,
            curve_data_=_poly_identity(),
            recorded_data={"x": xs, "y": ys},
            points_used=list(ctx.data.get("points_used", [])),
            ezo_calibration_status=status_body,
            notes="Calibrated using UI protocol.",
        )

        link = ctx.store_calibration(calibration, "do")
        ctx.complete(
            {
                "title": "DO calibration saved",
                "calibration": link,
                "ezo_status": status_body,
            }
        )
        return None


DO_STEPS: StepRegistry = {
    Intro.step_id: Intro,
    Configure.step_id: Configure,
    ClearExisting.step_id: ClearExisting,
    BufferAir.step_id: BufferAir,
    BufferZero.step_id: BufferZero,
    Finalize.step_id: Finalize,
}


def start_do_ezo_session(target_device: str) -> CalibrationSession:
    now = utc_iso_timestamp()
    return CalibrationSession(
        session_id=_new_calibration_name(),
        protocol_name="ezo_do",
        target_device=target_device,
        status="in_progress",
        step_id=Intro.step_id,
        data={},
        created_at=now,
        updated_at=now,
    )


class EzoDOProtocol(CalibrationProtocol[str]):
    target_device = "do"
    protocol_name = "ezo_do"
    title = "Atlas EZO-DO (dissolved oxygen)"
    description = "Calibrate an Atlas Scientific EZO-DO board using air (Cal) and optional zero (Cal,0)."
    requirements = (
        "DO probe connected and readable",
        "Access to air (for Cal)",
        "Optional: 0 dissolved oxygen solution (for Cal,0)",
        "Optional: ensure Temperature/Salinity/Pressure compensation settings are correct",
    )
    priority = 50
    step_registry = DO_STEPS

    @classmethod
    def start_session(cls, target_device: str) -> CalibrationSession:
        return start_do_ezo_session(target_device)

    def run(self, target_device: str) -> structs.CalibrationBase | list[structs.CalibrationBase]:
        from pioreactor.calibrations.session_flow import run_session_in_cli

        session = start_do_ezo_session(target_device)
        calibrations = run_session_in_cli(self.step_registry, session)
        if not calibrations:
            raise RuntimeError("No calibration was produced.")
        return t.cast(structs.CalibrationBase, calibrations[-1])

