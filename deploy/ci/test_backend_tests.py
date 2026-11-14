#!/usr/bin/env python3
"""并行分组：均衡装箱、慢文件拆分、组内路径有序。"""

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
        def start(groups, index, label, *_args):
            paths = [p for _, group in groups for p in group]
            calls.append(("start", index, paths))
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

if __name__ == "__main__":
    unittest.main()
