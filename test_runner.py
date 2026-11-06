"""Offline feedback and acquisition checks; no instrument connections are opened."""

from __future__ import annotations

import csv
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import runner
from instruments import SMUSample
from models import RunConfig


class FakeSMU:
    def __init__(self, *, shutdown_error: bool = False) -> None:
        self.connected = False
        self.configured = False
        self.started = False
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error
        self.configure_args: tuple[object, ...] = ()
        self.gate_commands: list[float] = []
        self.read_calls = 0

    def connect(self) -> str:
        self.connected = True
        return "Keithley 2634B fake"

    def configure(self, *args: object) -> None:
        self.configured = True
        self.configure_args = args

    def start(self) -> None:
        self.started = True

    def read_next(self, timeout_s: float, stop_event: threading.Event | None = None) -> SMUSample:
        self.read_calls += 1
        return SMUSample(1, -1e-6, 1e-9, 0.1, 0)

    def set_gate(self, voltage_v: float) -> float:
        self.gate_commands.append(voltage_v)
        return voltage_v

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise RuntimeError("outputs off readback failed")


class FakeDMM:
    def __init__(self, *, configure_error: bool = False, read_error: bool = False) -> None:
        self.configure_error = configure_error
        self.read_error = read_error
        self.closed = False
        self.read_calls = 0

    def connect(self) -> str:
        return "Keithley DMM6500 fake"

    def configure(self) -> None:
        if self.configure_error:
            raise RuntimeError("DMM impedance incorrect")

    def read_vref(self) -> float:
        self.read_calls += 1
        if self.read_error:
            raise RuntimeError("DMM read failed")
        return -0.1

    def close(self) -> None:
        self.closed = True


class RunnerTests(unittest.TestCase):
    def test_cycle_levels_duration_and_open_loop_validation(self) -> None:
        config = RunConfig(
            reference_mode="none", dmm_ip="", sweep_mode="cycles", cycle_count=2,
            sweep_start_v=0, sweep_stop_v=-0.2, sweep_step_v=-0.1,
            hold_s=10, final_keep_s=3600,
        )
        config.validate()
        self.assertEqual(config.levels, (0, -0.1, -0.2, -0.1, 0, -0.1, -0.2, -0.1, 0))
        self.assertEqual(config.duration_s, 90)
        self.assertEqual([config.cycle_for_stage(i) for i in range(1, 10)],
                         [1, 1, 1, 1, 1, 2, 2, 2, 2])
        with self.assertRaisesRegex(ValueError, "different initial and final"):
            RunConfig(reference_mode="none", sweep_mode="cycles",
                      sweep_start_v=0, sweep_stop_v=0).validate()
        with self.assertRaisesRegex(ValueError, "inside the gate bounds"):
            RunConfig(reference_mode="monitor", sweep_start_v=0,
                      sweep_stop_v=-2, sweep_step_v=-0.1).validate()
        with self.assertRaisesRegex(ValueError, "Cycle count"):
            RunConfig(sweep_mode="cycles", cycle_count=0).validate()
        with self.assertRaisesRegex(ValueError, "IP addresses"):
            RunConfig(reference_mode="monitor", dmm_ip="").validate()

    def test_config_defaults_and_feedback_bounds(self) -> None:
        config = RunConfig()
        self.assertEqual((config.smu_ip, config.dmm_ip), ("192.168.1.150", "192.168.1.160"))
        self.assertFalse(hasattr(config, "port"))
        self.assertEqual((config.drain_limit_a, config.gate_limit_a), (0.003, 0.003))
        self.assertEqual(runner.next_feedback_gate(0, -0.5, 0, config), -0.1)
        with self.assertRaisesRegex(ValueError, r"\.txt extension"):
            RunConfig(output_file="data.csv").validate()
        for polarity in (1, -1):
            with self.subTest(polarity=polarity):
                cfg = RunConfig(feedback_polarity=polarity)
                gate = 0.0
                for _ in range(80):
                    measured = polarity * 0.82 * gate
                    next_gate = runner.next_feedback_gate(gate, -0.5, measured, cfg)
                    self.assertLessEqual(abs(next_gate - gate), cfg.feedback_max_step_v + 1e-12)
                    self.assertLessEqual(cfg.gate_min_v, next_gate)
                    self.assertLessEqual(next_gate, cfg.gate_max_v)
                    gate = next_gate
                self.assertLessEqual(abs(-0.5 - polarity * 0.82 * gate), cfg.feedback_tolerance_v)

    def test_simulated_run_writes_six_columns_and_converges(self) -> None:
        for polarity in (1, -1):
            with self.subTest(polarity=polarity), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.txt"
                config = RunConfig(
                    feedback_polarity=polarity,
                    sweep_start_v=-0.05, sweep_stop_v=-0.05,
                    hold_s=0.8, delay_s=0, final_keep_s=0,
                    sample_interval_s=0.1, output_file=str(path),
                )
                samples = []
                result = runner.run_experiment(config, on_sample=samples.append)
                with path.open(newline="", encoding="utf-8") as stream:
                    data = csv.DictReader(stream, delimiter="\t")
                    self.assertEqual(data.fieldnames, ["Time", "GateI", "GateV", "DrainI", "DrainV", "Vref"])
                    rows = list(data)
                self.assertEqual(len(rows), result.samples)
                self.assertGreaterEqual(result.samples, 4)
                self.assertTrue(samples[-1].within_tolerance)
                self.assertTrue(all(math.isfinite(float(row["Vref"])) for row in rows))
                self.assertAlmostEqual(float(rows[0]["GateV"]), 0)
                self.assertLess(float(rows[-1]["GateV"]) * polarity, 0)
                self.assertTrue(all(sample.dmm_read_utc for sample in samples))

    def test_dmm_validation_precedes_smu_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            smu, dmm = FakeSMU(), FakeDMM(configure_error=True)
            config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                with self.assertRaisesRegex(RuntimeError, "impedance incorrect"):
                    runner.run_experiment(config)
            self.assertFalse(smu.connected)
            self.assertFalse(smu.started)
            self.assertTrue(dmm.closed)
            self.assertFalse(config.output_path.exists())

    def test_monitor_sweeps_gate_and_records_vref_without_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.txt"
            config = RunConfig(
                reference_mode="monitor", sweep_start_v=0, sweep_stop_v=-0.1,
                sweep_step_v=-0.1, hold_s=0.2, delay_s=0, final_keep_s=0,
                sample_interval_s=0.1, output_file=str(path),
            )
            samples = []
            with patch.object(runner, "next_feedback_gate", side_effect=AssertionError("feedback called")):
                runner.run_experiment(config, on_sample=samples.append)
            self.assertEqual({sample.stage_index for sample in samples}, {1, 2})
            self.assertEqual({sample.gate_v for sample in samples if sample.stage_index == 1}, {0})
            self.assertEqual({sample.gate_v for sample in samples if sample.stage_index == 2}, {-0.1})
            self.assertTrue(all(sample.vref_v is not None for sample in samples))
            self.assertTrue(all(sample.within_tolerance is None for sample in samples))
            with path.open(newline="", encoding="utf-8") as stream:
                self.assertTrue(all(row["Vref"] for row in csv.DictReader(stream, delimiter="\t")))

    def test_no_reference_live_path_never_creates_dmm_and_writes_blank_vref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no_reference.txt"
            config = RunConfig(
                simulation=False, reference_mode="none", dmm_ip="",
                sweep_start_v=-0.2, sweep_stop_v=-0.2,
                output_file=str(path),
            )
            smu = FakeSMU()
            stop = threading.Event()
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", side_effect=AssertionError("DMM constructed")
            ), patch.object(runner, "next_feedback_gate", side_effect=AssertionError("feedback called")):
                result = runner.run_experiment(config, on_sample=lambda _: stop.set(), stop_event=stop)
            self.assertEqual(result.samples, 1)
            self.assertEqual(smu.configure_args[1], -0.2)
            self.assertEqual(smu.gate_commands, [])
            self.assertEqual(smu.shutdown_calls, 1)
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(rows[0]["Vref"], "")

    def test_stop_during_dmm_read_does_not_issue_feedback_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            smu = FakeSMU()

            class StopAfterDMM(FakeDMM):
                def read_vref(self) -> float:
                    stop.set()
                    return -0.1

            dmm = StopAfterDMM()
            config = RunConfig(simulation=False, output_file=str(Path(directory) / "stop.txt"))
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                result = runner.run_experiment(config, stop_event=stop)
            self.assertTrue(result.stopped)
            self.assertEqual(result.samples, 0)
            self.assertEqual(smu.gate_commands, [])
            self.assertEqual(smu.shutdown_calls, 1)

    def test_cyclic_sweep_commands_levels_without_final_keep(self) -> None:
        for reference_mode in ("feedback", "monitor", "none"):
            with self.subTest(reference_mode=reference_mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cycles.txt"
                config = RunConfig(
                    reference_mode=reference_mode, sweep_mode="cycles", cycle_count=2,
                    sweep_start_v=0, sweep_stop_v=-0.1, sweep_step_v=-0.1,
                    hold_s=0.1, delay_s=0, final_keep_s=3600,
                    sample_interval_s=0.1, output_file=str(path),
                )
                samples = []
                result = runner.run_experiment(config, on_sample=samples.append)
                self.assertFalse(result.stopped)
                self.assertEqual({sample.stage_index for sample in samples}, set(range(1, 5 + 1)))
                self.assertEqual({sample.cycle_index for sample in samples}, {1, 2})
                self.assertTrue(all(sample.phase != "final_keep" for sample in samples))
                if reference_mode != "feedback":
                    stage_values = [next(sample.gate_v for sample in samples
                                         if sample.stage_index == i) for i in range(1, 6)]
                    self.assertEqual(stage_values, list(config.levels))

    def test_staircase_keeps_final_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = RunConfig(
                reference_mode="none", sweep_start_v=0, sweep_stop_v=0,
                hold_s=0.1, delay_s=0, final_keep_s=0.2,
                sample_interval_s=0.1,
                output_file=str(Path(directory) / "keep.txt"),
            )
            self.assertAlmostEqual(config.duration_s, 0.3)
            samples = []
            runner.run_experiment(config, on_sample=samples.append)
            self.assertIn("final_keep", {sample.phase for sample in samples})

    def test_live_path_passes_current_limits_and_stops_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            smu, dmm = FakeSMU(), FakeDMM()
            stop = threading.Event()
            config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                result = runner.run_experiment(config, on_sample=lambda _: stop.set(), stop_event=stop)
            self.assertEqual(smu.configure_args, (
                config.vds_v, config.gate_initial_v, 0.003, 0.003, config.sample_interval_s
            ))
            self.assertTrue(result.stopped)
            self.assertEqual(result.samples, 1)
            self.assertEqual(smu.shutdown_calls, 1)
            self.assertTrue(dmm.closed)

    def test_dmm_read_error_or_unverified_shutdown_is_reported(self) -> None:
        for read_error, shutdown_error, expected in (
            (True, False, "DMM read failed"),
            (False, True, "2634B shutdown could not be verified"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                smu = FakeSMU(shutdown_error=shutdown_error)
                dmm = FakeDMM(read_error=read_error)
                stop = threading.Event()
                config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
                with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                    runner, "DMM6500", return_value=dmm
                ):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        runner.run_experiment(config, on_sample=lambda _: stop.set(), stop_event=stop)
                self.assertTrue(smu.started)
                self.assertEqual(smu.shutdown_calls, 1)
                self.assertTrue(dmm.closed)


if __name__ == "__main__":
    unittest.main()
