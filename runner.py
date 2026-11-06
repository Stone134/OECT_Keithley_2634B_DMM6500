"""Single-OECT acquisition with optional high-impedance reference sensing.

The 2634B current readings are requested from smua and smub in one TSP query;
when selected, the DMM6500 reference reading follows over LAN. These readings
are not hardware simultaneous. Voltage columns are source setpoints.
"""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from instruments import Keithley2634B, SMUSample, DMM6500
from models import RunConfig, Sample


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# GateV and DrainV are verified source setpoints; Vref is blank without a DMM.
FIELDS = ("Time", "GateI", "GateV", "DrainI", "DrainV", "Vref")


@dataclass(frozen=True)
class RunResult:
    output_path: Path
    samples: int
    stopped: bool


class RawDataWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file, delimiter="\t", lineterminator="\n")
        self._writer.writerow(FIELDS)
        self._file.flush()
        self.samples = 0

    @staticmethod
    def _num(value: float) -> str:
        return f"{value:.7E}"

    def write(self, sample: Sample) -> None:
        self._writer.writerow((
            self._num(sample.elapsed_s),
            self._num(sample.gate_i_a),
            self._num(sample.gate_v),
            self._num(sample.drain_i_a),
            self._num(sample.drain_v),
            self._num(sample.vref_v) if sample.vref_v is not None else "",
        ))
        self._file.flush()  # preserve each reading even if a long run is interrupted
        self.samples += 1

    def close(self) -> None:
        self._file.close()


class SimulatedSMU:
    """Exercise the same run logic without opening a network connection."""

    def __init__(self, interval_s: float, feedback_polarity: int = 1) -> None:
        self.interval_s = interval_s
        # The simulated cell follows the selected response direction. This
        # exercises either controller setting; it cannot identify a real cell's
        # response direction, which must be established experimentally.
        self.feedback_polarity = feedback_polarity
        self.gate_v = 0.0
        self.vds_v = 0.0
        self.started: float | None = None
        self.last_index = 0
        self.closed = False

    def connect(self) -> str:
        return "SIMULATED Keithley 2634B"

    def configure(
        self, vds_v: float, gate_initial_v: float, drain_limit_a: float,
        gate_limit_a: float, interval_s: float,
    ) -> None:
        self.vds_v = vds_v
        self.gate_v = gate_initial_v
        self.interval_s = interval_s

    def start(self) -> None:
        self.started = time.monotonic()

    def read_next(self, timeout_s: float, stop_event: threading.Event | None = None) -> SMUSample:
        if self.started is None:
            raise RuntimeError("Simulation has not started")
        next_time = self.started + (self.last_index + 1) * self.interval_s
        wait = next_time - time.monotonic()
        if wait > timeout_s:
            raise TimeoutError("Simulated current sample timed out")
        if wait > 0:
            if stop_event is None:
                time.sleep(wait)
            elif stop_event.wait(wait):
                raise InterruptedError("Measurement stopped during the read interval")
        elapsed = time.monotonic() - self.started
        index = max(self.last_index + 1, int(elapsed / self.interval_s))
        skipped = index - self.last_index - 1
        self.last_index = index
        # The default convention is same-sign Vg/Vref/Eref.
        e = self.feedback_polarity * 0.82 * self.gate_v + 0.003 * math.sin(elapsed / 12)
        conductance = 1.5e-6 + 4.0e-5 * abs(e)
        id_a = self.vds_v * conductance + 2e-9 * math.sin(elapsed * 0.6)
        ig_a = -self.gate_v * 1.2e-8 + 4e-10 * math.cos(elapsed * 0.4)
        return SMUSample(index, id_a, ig_a, elapsed, skipped)

    def set_gate(self, voltage_v: float) -> float:
        self.gate_v = voltage_v
        return voltage_v

    def shutdown(self) -> None:
        self.gate_v = 0.0
        self.vds_v = 0.0
        self.closed = True

    def close(self) -> None:
        self.closed = True


class SimulatedDMM:
    def __init__(self, smu: SimulatedSMU) -> None:
        self.smu = smu

    def connect(self) -> str:
        return "SIMULATED DMM6500"

    def configure(self) -> None:
        return None

    def read_vref(self) -> float:
        # The reference voltage is deliberately distinct from the programmed gate V.
        elapsed = 0.0 if self.smu.started is None else time.monotonic() - self.smu.started
        return (self.smu.feedback_polarity * 0.82 * self.smu.gate_v
                + 0.003 * math.sin(elapsed / 12))

    def close(self) -> None:
        return None


def next_feedback_gate(current_v: float, target_e_v: float, measured_e_v: float,
                       config: RunConfig) -> float:
    """Bounded proportional update. Polarity is the measured sign of dE/dVgate."""
    error = target_e_v - measured_e_v
    if abs(error) <= config.feedback_tolerance_v:
        return current_v
    desired_step = config.feedback_polarity * config.correction_factor * error
    step = max(-config.feedback_max_step_v,
               min(config.feedback_max_step_v, desired_step))
    return max(config.gate_min_v, min(config.gate_max_v, current_v + step))


def run_experiment(
    config: RunConfig,
    on_sample: Callable[[Sample], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> RunResult:
    """Run an Eref target or open-loop Vg sweep with optional DMM sensing.

    Feedback uses Eref = V(reference)−V(source); monitor and no-reference
    modes use the sweep levels as direct Vg setpoints.
    """
    config.validate()
    stop = stop_event or threading.Event()
    status = on_status or (lambda _message: None)
    report = on_sample or (lambda _sample: None)

    smu = SimulatedSMU(config.sample_interval_s, config.feedback_polarity) if config.simulation else Keithley2634B(
        config.smu_ip, timeout_s=10.0
    )
    dmm = None if config.reference_mode == "none" else (
        SimulatedDMM(smu) if config.simulation else DMM6500(config.dmm_ip, timeout_s=10.0)
    )
    writer: RawDataWriter | None = None
    started: float | None = None
    levels = config.levels
    gate_v = config.gate_initial_v if config.reference_mode == "feedback" else levels[0]
    saturated_count = 0
    stopped = False
    failure: Exception | None = None
    try:
        # When used, verify the high-impedance meter before enabling SMU output.
        if dmm is not None:
            status("Connecting DMM6500 and verifying high-impedance DC voltage mode")
            dmm_id = dmm.connect()
            dmm.configure()
            status(f"DMM ready: {dmm_id}")
        if stop.is_set():
            return RunResult(config.output_path, 0, True)

        status("Connecting Keithley 2634B; smua = drain, smub = gate")
        smu_id = smu.connect()
        smu.configure(config.vds_v, gate_v, config.drain_limit_a,
                        config.gate_limit_a, config.sample_interval_s)
        status(f"2634B ready: {smu_id}")
        writer = RawDataWriter(config.output_path)

        status("Starting OECT acquisition")
        smu.start()
        started = time.monotonic()
        stage_index = 0
        stage_start = started
        final_keep_s = config.final_keep_s if config.sweep_mode == "staircase" else 0.0
        stage_end = stage_start + config.hold_s + (final_keep_s if len(levels) == 1 else 0.0)
        last_target = levels[0]
        stage_has_sample = False
        while not stop.is_set():
            now = time.monotonic()
            if now >= stage_end and stage_has_sample:
                if stage_index + 1 >= len(levels):
                    break
                stage_index += 1
                last_target = levels[stage_index]
                if config.reference_mode != "feedback":
                    # Open-loop stages command Vg once, before their first read.
                    gate_v = smu.set_gate(last_target)
                stage_start = time.monotonic()
                stage_end = stage_start + config.hold_s
                if stage_index + 1 == len(levels):
                    stage_end += final_keep_s
                stage_has_sample = False
                saturated_count = 0
                quantity = "target Eref" if config.reference_mode == "feedback" else "Vg"
                cycle_text = (f" • cycle {config.cycle_for_stage(stage_index + 1)}/"
                              f"{config.cycle_count}" if config.sweep_mode == "cycles" else "")
                status(f"Level {stage_index + 1}/{len(levels)}{cycle_text}: "
                       f"{quantity} {last_target:+.4f} V")

            try:
                pair = smu.read_next(timeout_s=config.sample_interval_s + 10.0,
                                     stop_event=stop)
            except InterruptedError:
                stopped = True
                break
            if stop.is_set():
                stopped = True
                break
            vref = dmm.read_vref() if dmm is not None else None
            dmm_time = utc_now() if dmm is not None else None
            if stop.is_set():
                stopped = True
                break
            if vref is not None and (not math.isfinite(vref) or abs(vref) > 10):
                raise ValueError(f"DMM returned invalid Vref={vref!r}")
            now = time.monotonic()
            elapsed = now - started
            stage_age = now - stage_start
            if final_keep_s and stage_index + 1 == len(levels) and stage_age >= config.hold_s:
                phase = "final_keep"
            elif stage_age < config.delay_s:
                phase = "settling"
            else:
                phase = "measure"
            sample = Sample(
                utc=utc_now(), elapsed_s=elapsed, stage_index=stage_index + 1,
                phase=phase, target_v=last_target, gate_i_a=pair.gate_i_a,
                gate_v=gate_v, drain_i_a=pair.drain_i_a, drain_v=config.vds_v,
                vref_v=vref, e_reference_v=vref,
                smu_index=pair.index,
                smu_nominal_elapsed_s=pair.nominal_elapsed_s,
                smu_skipped=pair.skipped, dmm_read_utc=dmm_time,
                within_tolerance=(abs(last_target - vref) <= config.feedback_tolerance_v
                                  if config.reference_mode == "feedback" and vref is not None else None),
                cycle_index=config.cycle_for_stage(stage_index + 1),
            )
            writer.write(sample)
            stage_has_sample = True
            report(sample)
            if stop.is_set():
                stopped = True
                break
            if config.reference_mode == "feedback":
                assert vref is not None
                new_gate_v = next_feedback_gate(gate_v, last_target, vref, config)
                if not math.isclose(new_gate_v, gate_v, abs_tol=1e-12):
                    gate_v = smu.set_gate(new_gate_v)
                at_limit = (math.isclose(gate_v, config.gate_min_v, abs_tol=1e-8) or
                            math.isclose(gate_v, config.gate_max_v, abs_tol=1e-8))
                if at_limit and abs(last_target - vref) > max(0.05, 2 * config.feedback_tolerance_v):
                    saturated_count += 1
                    if saturated_count >= 10:
                        raise RuntimeError("Feedback could not reach target before the gate limit; outputs stopped")
                else:
                    saturated_count = 0
        stopped = stopped or stop.is_set()
        return RunResult(config.output_path, writer.samples, stopped)
    except Exception as exc:
        failure = exc
        raise
    finally:
        # A DMM, SMU or disk error must trigger an output-off cleanup attempt.
        cleanup_errors: list[str] = []
        try:
            smu.shutdown()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"2634B shutdown could not be verified: {cleanup_exc}")
        if dmm is not None:
            try:
                dmm.close()
            except Exception as cleanup_exc:
                cleanup_errors.append(f"DMM close failed: {cleanup_exc}")
        if writer is not None:
            try:
                writer.close()
            except Exception as cleanup_exc:
                cleanup_errors.append(f"Raw-data close failed: {cleanup_exc}")
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            status(message)
            if failure is not None:
                raise RuntimeError(f"{failure}; {message}") from failure
            raise RuntimeError(message)
        if started is not None:
            status("Stopped: 2634B drain/gate set to 0 V and outputs OFF")
