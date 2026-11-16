#!/usr/bin/env python3
"""并行分组：均衡装箱、慢文件拆分、组内路径有序。"""

import json
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_tests  # noqa: E402


class PartitionTests(unittest.TestCase):
    def test_fast_files_stay_whole_and_sorted(self):
        ids = [f"tests/b.py::t{i}" for i in range(3)] + [f"tests/a.py::t{i}" for i in range(3)]
        groups = backend_tests.partition(ids, {}, 1)
        self.assertEqual(groups[0][1], ["tests/a.py", "tests/b.py"])

    def test_slow_file_is_split_across_groups(self):
        ids = [f"tests/acceptance/test_slow.py::t{i}" for i in range(4)] + ["tests/x.py::a"]
        durations = dict.fromkeys(ids[:4], 300.0)
        groups = backend_tests.partition(ids, durations, 4)
        slow_groups = [g for _, g in groups if any("test_slow.py::" in a for a in g)]
        self.assertEqual(len(slow_groups), 4)

    def test_load_is_balanced(self):
        ids = [f"tests/f{i}.py::t" for i in range(8)]
        durations = {nodeid: float(10 + i) for i, nodeid in enumerate(ids)}
        loads = [load for load, _ in backend_tests.partition(ids, durations, 4)]
        self.assertLessEqual(max(loads) - min(loads), 2.0)

    def test_unmeasured_acceptance_gets_heavy_weight(self):
        weights = backend_tests.weights_for(["tests/acceptance/test_x.py::a", "tests/y.py::b"], {})
        self.assertGreater(weights["tests/acceptance/test_x.py::a"], weights["tests/y.py::b"])

    def test_shard_urls_use_distinct_database_and_redis(self):
        env = {
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/base_test",
            "REDIS_URL": "redis://127.0.0.1:2/0",
            "TEST_REDIS_URL": "redis://127.0.0.1:2/1",
        }
        first = backend_tests.shard_env(env, 0, "ci_shard0_test", Path("/tmp/a"))
        second = backend_tests.shard_env(env, 1, "ci_shard1_test", Path("/tmp/b"))
        self.assertTrue(first["DATABASE_URL"].endswith("/ci_shard0_test"))
        self.assertNotEqual(first["REDIS_URL"], second["REDIS_URL"])
        self.assertNotEqual(first["TEST_REDIS_URL"], second["TEST_REDIS_URL"])
        self.assertNotEqual(first["REDIS_URL"], first["TEST_REDIS_URL"])


    def test_acceptance_runs_in_exclusive_phase(self):
        ids = ["tests/x.py::a", "tests/acceptance/test_flow.py::b", "tests/y.py::c"]
        regular, exclusive = backend_tests.split_exclusive(ids)
        self.assertEqual(regular, ["tests/x.py::a", "tests/y.py::c"])
        self.assertEqual(exclusive, ["tests/acceptance/test_flow.py::b"])

    def test_serial_capacity_files_preserve_every_collected_case(self):
        ids = ["tests/x.py::a", "tests/cap.py::size[10000]",
               "tests/cap.py::rollback", "tests/acceptance/test_flow.py::b"]
        parallel, serial = backend_tests.split_serial(ids, ["tests/cap.py"])
        self.assertEqual(serial, ids[1:3])
        self.assertEqual(parallel, [ids[0], ids[3]])
        self.assertEqual(set(parallel + serial), set(ids))
        self.assertFalse(set(parallel) & set(serial))
        self.assertEqual(backend_tests.partition(serial, {}, 1)[0][1], ["tests/cap.py"])

    def test_serial_files_wait_for_parallel_and_keep_separate_results(self):
        ids = ["tests/normal.py::a", "tests/cap_a.py::b", "tests/cap_b.py::c"]
        calls = []
        def start(groups, index, label, *_args, **kwargs):
            paths = [p for _, group in groups for p in group]
            calls.append(("start", index, paths))
            if label == "容量串行":
                self.assertEqual(kwargs["redis_index"], 0)
            return (index, paths)
        def wait(running, *_args):
            index, paths = running
            calls.append(("wait", index, paths))
            return ({n: "passed" for n in ids if n.split("::")[0] in paths}, {}, [])
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            backend_tests.os.environ, {}, clear=True
        ), patch.object(sys, "argv", ["backend_tests.py", "--work", directory,
            "--serial-file", "tests/cap_a.py", "--serial-file", "tests/cap_b.py"
        ]), patch.object(backend_tests, "collect", return_value=ids), patch.object(
            backend_tests, "start_groups", side_effect=start
        ), patch.object(backend_tests, "wait_groups", side_effect=wait):
            self.assertEqual(backend_tests.main(), 0)
        self.assertEqual([c[0] for c in calls], ["start", "wait"] * 3)
        self.assertEqual([c[2] for c in calls[::2]],
                         [["tests/normal.py"], ["tests/cap_a.py"], ["tests/cap_b.py"]])
        self.assertEqual(len({c[1] for c in calls[::2]}), 3)

    def test_serial_artifact_index_does_not_allocate_more_redis_databases(self):
        env = {"DATABASE_URL": "postgresql://u:p@localhost/base_test",
               "REDIS_URL": "redis://localhost/0", "TEST_REDIS_URL": "redis://localhost/1"}
        args = Mock(work=Path("/tmp"), fast=False)
        with patch.object(backend_tests, "recreate_database"), patch.object(
            backend_tests.Path, "open", return_value=Mock()
        ), patch.object(backend_tests.Path, "unlink"), patch.object(
            backend_tests.subprocess, "Popen"
        ) as process:
            backend_tests.start_groups([(1, ["tests/cap.py"])], 100, "容量串行",
                                       args, env, redis_index=0)
            actual = process.call_args.kwargs["env"]
            self.assertTrue(actual["TEST_REDIS_URL"].endswith("/3"))
            self.assertTrue(actual["DATABASE_URL"].endswith("/ci_shard100_test"))

    def test_fast_tier_deselects_slow_marker(self):
        fast = backend_tests.pytest_command(["tests"], fast=True)
        full = backend_tests.pytest_command(["tests"], fast=False)
        self.assertIn("not slow and not mcp", fast)
        self.assertIn("not mcp", full)
        self.assertFalse(any("slow" in arg for arg in full))

    def test_exclusive_shards_do_not_reuse_regular_resources(self):
        env = {
            "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/base_test",
            "REDIS_URL": "redis://127.0.0.1:2/0",
            "TEST_REDIS_URL": "redis://127.0.0.1:2/1",
        }
        regular = backend_tests.shard_env(env, 5, "ci_shard5_test", Path("/tmp/a"))
        exclusive = backend_tests.shard_env(env, 6, "ci_shard6_test", Path("/tmp/b"))
        self.assertNotEqual(regular["REDIS_URL"], exclusive["REDIS_URL"])
        self.assertNotEqual(regular["TEST_REDIS_URL"], exclusive["TEST_REDIS_URL"])


class RunnerPartitionTests(unittest.TestCase):
    def test_whole_files_are_disjoint_and_cover_every_case(self):
        ids = [f"tests/heavy.py::case[{i}]" for i in range(300)] + [
            "tests/serial.py::a", "tests/small.py::b", "tests/small.py::c"
        ]
        selections = [backend_tests.runner_partition(ids, {}, i, 2) for i in range(2)]
        self.assertEqual(set(selections[0]) | set(selections[1]), set(ids))
        self.assertFalse(set(selections[0]) & set(selections[1]))
        files = [{n.split("::", 1)[0] for n in group} for group in selections]
        self.assertFalse(files[0] & files[1])
        self.assertEqual(sum("tests/heavy.py" in group for group in files), 1)
        parallel, serial = backend_tests.split_serial(
            selections[0] + selections[1], ["tests/serial.py"]
        )
        self.assertEqual(serial, ["tests/serial.py::a"])
        self.assertEqual(set(parallel + serial), set(ids))

    def test_assignment_depends_on_files_not_random_parameter_ids(self):
        first = ["tests/a.py::case[first]", "tests/b.py::case[x]"]
        later = ["tests/b.py::case[y]", "tests/a.py::case[second]"]
        for index in range(2):
            a = backend_tests.runner_partition(first, {}, index, 2)
            b = backend_tests.runner_partition(later, {}, index, 2)
            self.assertEqual({n.split("::")[0] for n in a}, {n.split("::")[0] for n in b})
        self.assertEqual(backend_tests.runner_partition(first, {}, 0, 1), first)

    def test_invalid_partition_cannot_silently_skip_tests(self):
        for index, count in [(-1, 2), (2, 2), (0, 0)]:
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                backend_tests.runner_partition(["tests/a.py::case"], {}, index, count)


class ProcessResultTests(unittest.TestCase):
    def test_file_isolation_is_explicitly_requested_by_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            for option in ([], ["--isolate-files"]):
                with self.subTest(option=option), patch.dict(backend_tests.os.environ, {}, clear=True), patch.object(
                    sys, "argv", ["backend_tests.py", "--work", directory, *option]
                ), patch.object(backend_tests, "collect", return_value=[]) as collect:
                    self.assertEqual(backend_tests.main(), 0)
                    self.assertEqual(
                        collect.call_args.args[1].get("CI_ISOLATE_TEST_FILES"),
                        "1" if option else None,
                    )

    def test_progress_can_read_a_complete_prefix_while_report_is_being_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            record = {"nodeid": "tests/a.py::case", "duration": 1,
                      "outcome": "passed", "when": "call"}
            path.write_text(json.dumps(record) + "\n" + '{"nodeid":')
            outcomes, _ = backend_tests.read_results(path)
            self.assertEqual(outcomes, {"tests/a.py::case": "passed"})
            path.write_text("invalid json\n")
            with self.assertRaises(json.JSONDecodeError):
                backend_tests.read_results(path)

    def test_progress_is_emitted_before_child_process_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "shard0.log").write_text("pytest finished\n")
            args = Mock(work=work)
            process = Mock()
            process.poll.side_effect = [None, 0]
            process.wait.return_value = 0
            with patch.object(backend_tests, "read_results", return_value=(
                {"tests/a.py::case": "passed"}, {}
            )), patch.object(backend_tests.time, "sleep"), patch("builtins.print") as output:
                result = backend_tests.wait_groups(
                    [(0, process, Mock(), work / "shard0.jsonl")], "", args, 0
                )
            self.assertFalse(result[2])
            self.assertTrue(any("进度：完成 1" in str(call) for call in output.call_args_list))

    def wait_for_result(self, returncode, outcomes):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "shard0.log").write_text("pytest finished\n")
            args = Mock(work=work)
            process = Mock()
            process.wait.return_value = returncode
            with patch.object(backend_tests, "read_results", return_value=(outcomes, {})):
                return backend_tests.wait_groups(
                    [(0, process, Mock(), work / "shard0.jsonl")],
                    "", args, time.monotonic(),
                )[2]

    def test_passing_cases_do_not_hide_session_failure(self):
        self.assertTrue(self.wait_for_result(1, {"tests/x.py::case": "passed"}))

    def test_internal_error_remains_fatal_with_failed_cases(self):
        self.assertTrue(self.wait_for_result(3, {"tests/x.py::case": "failed"}))

    def test_normal_test_failure_is_available_for_existing_rerun(self):
        self.assertEqual(self.wait_for_result(1, {"tests/x.py::case": "failed"}), [])

    def test_successful_process_has_no_process_error(self):
        self.assertEqual(self.wait_for_result(0, {"tests/x.py::case": "passed"}), [])


class WorkflowGateTests(unittest.TestCase):
    def test_module_gate_requires_both_runners_and_rejects_non_success(self):
        import yaml
        path = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
        jobs = yaml.safe_load(path.read_text())["jobs"]
        modules = jobs["backend_modules"]
        self.assertEqual({r["partition"] for r in modules["strategy"]["matrix"]["include"]}, {0, 1})
        self.assertIs(modules["steps"], jobs["backend"]["steps"])
        self.assertIs(modules["services"], jobs["backend"]["services"])
        self.assertIs(modules["env"], jobs["backend"]["env"])
        gate = jobs["backend_modules_gate"]
        self.assertEqual(gate["name"], "backend (modules)")
        self.assertEqual(gate["needs"], "backend_modules")
        self.assertIn("always()", gate["if"])
        self.assertEqual(gate["steps"][0]["env"]["MODULE_RESULT"], "${{ needs.backend_modules.result }}")
        for status in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(status=status):
                actual = subprocess.run(["bash", "-c", gate["steps"][0]["run"]],
                                        env={"MODULE_RESULT": status}, check=False)
                self.assertEqual(actual.returncode == 0, status == "success")

    def test_embedded_browser_files_have_their_toolchain_and_exclusive_phase(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
        steps = jobs["backend_modules"]["steps"]
        run_index = next(i for i, step in enumerate(steps)
                         if step.get("name") == "Run all module tests in isolated groups")
        before = steps[:run_index]
        self.assertTrue(any("oven-sh/setup-bun@" in step.get("uses", "")
                            and "modules" in step.get("if", "") for step in before))
        self.assertTrue(any("bun install --frozen-lockfile" in step.get("run", "")
                            and "modules" in step.get("if", "") for step in before))
        self.assertTrue(any("playwright install" in step.get("run", "")
                            and "modules" in step.get("if", "") for step in before))
        command = shlex.split(steps[run_index]["run"])
        serial_files = {command[i + 1] for i, token in enumerate(command) if token == "--serial-file"}
        browser_files = {
            str(path.relative_to(root / "backend"))
            for path in (root / "backend/tests/modules").rglob("test_*.py")
            if any(name in path.read_text() for name in (
                "native_browser_http", "run_native_workspace", "live_campaign_workspace", '"@directory-live"'
            ))
        }
        self.assertTrue(browser_files)
        self.assertLessEqual(browser_files, serial_files)

if __name__ == "__main__":
    unittest.main()
