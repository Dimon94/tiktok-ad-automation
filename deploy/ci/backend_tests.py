#!/usr/bin/env python3
"""并行运行后端 pytest：按历史耗时分组，每组独立数据库与 Redis 库号。

- 耗时超过阈值的测试文件按用例拆分到多个组，避免单个慢文件决定总时长。
- 端到端验收（tests/acceptance）是多分钟级批量搭建，对数据库负载敏感，不与普通用例
  共用同一个测试数据库实例：设置 ACCEPTANCE_DATABASE_URL（独立实例）时两类同时运行，
  否则先跑完普通用例再单独跑验收。
- --serial-file 将指定容量文件放到并行组结束后逐文件独立进程执行，保留所有收集和原性能门槛。
- --fast 只跑快速层（排除 slow 标记，含全部验收），供日常检查；发布门禁跑全部。
- 官方 MCP 通道退役中，mcp 标记的用例始终不运行。
- 每组内保持文件路径排序（已知部分夹具依赖收集顺序）。
- 并行失败的用例在全新数据库中单独重跑一次；重跑通过记为“并行不稳定”并列出，
  仍失败才判定失败。失败过多时视为真实故障，不重跑。
- 运行后把实测耗时写回缓存，下次分组更均衡。

必须在 backend 目录、uv 环境中执行，并已设置指向独立测试实例的 DATABASE_URL、
REDIS_URL、TEST_REDIS_URL（Redis 需开启足够多的库号）。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_WEIGHT = 1.0
ACCEPTANCE_WEIGHT = 120.0
SPLIT_SECONDS = 240.0
MAX_RERUNS = 20
EXCLUSIVE_PREFIX = "tests/acceptance/"


def weights_for(nodeids, durations):
    """每条用例的估计耗时：优先实测值，缺失时按目录给经验值。"""
    result = {}
    for nodeid in nodeids:
        if nodeid in durations:
            result[nodeid] = durations[nodeid]
        else:
            result[nodeid] = (
                ACCEPTANCE_WEIGHT if nodeid.startswith("tests/acceptance/") else DEFAULT_WEIGHT
            )
    return result


def partition(nodeids, durations, shards):
    """最长处理时间优先装箱；返回每组的 pytest 参数（文件路径或用例 ID）。"""
    weights = weights_for(nodeids, durations)
    by_file = defaultdict(list)
    for nodeid in nodeids:
        by_file[nodeid.split("::", 1)[0]].append(nodeid)
    units = []  # (估计耗时, 文件, 参数列表)
    for path, ids in by_file.items():
        total = sum(weights[i] for i in ids)
        if total > SPLIT_SECONDS and len(ids) > 1:
            units.extend((weights[i], path, [i]) for i in ids)
        else:
            units.append((total, path, [path]))
    bins = [[0.0, []] for _ in range(max(1, shards))]
    for weight, path, args in sorted(units, key=lambda unit: (-unit[0], unit[1])):
        target = min(bins, key=lambda b: b[0])
        target[0] += weight
        target[1].append((path, args))
    return [
        (load, [arg for _, args in sorted(items, key=lambda item: item[0]) for arg in args])
        for load, items in bins
        if items
    ]


def split_exclusive(nodeids):
    """拆出需独占阶段运行的验收用例；两部分均保持原收集顺序。"""
    regular = [n for n in nodeids if not n.startswith(EXCLUSIVE_PREFIX)]
    exclusive = [n for n in nodeids if n.startswith(EXCLUSIVE_PREFIX)]
    return regular, exclusive


def split_serial(nodeids, files):
    """明确指定的容量测量文件独占 CPU/数据库；不减少收集或改变原门槛。"""
    files = set(files)
    return (
        [n for n in nodeids if n.split("::", 1)[0] not in files],
        [n for n in nodeids if n.split("::", 1)[0] in files],
    )


def with_database(url, name):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


def with_redis_db(url, number):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + str(number)))


def recreate_database(base_url, name):
    import psycopg
    from psycopg import sql

    admin = urlsplit(base_url)._replace(path="/postgres")
    dsn = urlunsplit(admin).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def marker_args(fast):
    # 官方 MCP 通道退役中，按 tests/conftest.py 打上的 mcp 标记始终排除；
    # 快速层另按 slow 标记排除慢用例。
    return ["-m", "not slow and not mcp" if fast else "not mcp"]


def collect(targets, env, fast=False):
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            *marker_args(fast), *targets,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line for line in result.stdout.splitlines() if "::" in line]
    if result.returncode not in (0, 5) or (not ids and result.returncode != 5):
        sys.stdout.write(result.stdout[-4000:] + result.stderr[-4000:])
        raise SystemExit("后端测试收集失败")
    return ids


def read_results(path):
    outcomes, durations = {}, defaultdict(float)
    if not path.exists():
        return outcomes, durations
    for line in path.read_text().splitlines():
        record = json.loads(line)
        nodeid = record["nodeid"]
        durations[nodeid] += record["duration"]
        if record["outcome"] == "failed":
            outcomes[nodeid] = "failed"
        elif record["outcome"] == "skipped" and nodeid not in outcomes:
            # 条件跳过发生在 setup 阶段，不会再有 call 记录。
            outcomes[nodeid] = "skipped"
        elif record["when"] == "call" and outcomes.get(nodeid) != "failed":
            outcomes[nodeid] = record["outcome"]
    return outcomes, durations


def shard_env(base_env, index, name, results, database_url=None):
    env = dict(base_env)
    env["DATABASE_URL"] = with_database(database_url or base_env["DATABASE_URL"], name)
    env["REDIS_URL"] = with_redis_db(base_env["REDIS_URL"], 2 + 2 * index)
    env["TEST_REDIS_URL"] = with_redis_db(base_env["TEST_REDIS_URL"], 3 + 2 * index)
    env["CI_RESULTS_FILE"] = str(results)
    plugin_dir = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [plugin_dir, env.get("PYTHONPATH")]))
    return env


def pytest_command(args, fast=False):
    return [
        sys.executable, "-m", "pytest", "-q", "--tb=short", "--show-capture=no",
        "-p", "no:cacheprovider", "-p", "pytest_results", *marker_args(fast), *args,
    ]


def start_groups(groups, first_index, label, args, base_env, database_url=None):
    """启动一批分组；组号从 first_index 起，保证库名与 Redis 库号不冲突。"""
    database_url = database_url or base_env["DATABASE_URL"]
    running = []
    for offset, (load, group) in enumerate(groups):
        index = first_index + offset
        name = f"ci_shard{index}_test"
        recreate_database(database_url, name)
        results = args.work / f"shard{index}.jsonl"
        results.unlink(missing_ok=True)
        log = (args.work / f"shard{index}.log").open("w")
        process = subprocess.Popen(
            pytest_command(group, args.fast),
            env=shard_env(base_env, index, name, results, database_url),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        running.append((index, process, log, results))
        print(f"  {label}组 {index}：{len(group)} 个参数，预计 {load / 60:.1f} 分钟", flush=True)
    return running


def wait_groups(running, label, args, started):
    outcomes, measured = {}, {}
    process_errors = []
    for index, process, log, results in running:
        returncode = process.wait()
        log.close()
        shard_outcomes, shard_durations = read_results(results)
        outcomes.update(shard_outcomes)
        measured.update(shard_durations)
        # sessionfinish/插件错误可能发生在全部用例通过后，不能只看逐项结果。
        if returncode not in (0, 1) or (
            returncode == 1 and "failed" not in shard_outcomes.values()
        ):
            process_errors.append(f"{label}组 {index}：pytest 退出码 {returncode}")
        tail = (args.work / f"shard{index}.log").read_text().strip().splitlines()[-1:]
        print(f"  {label}组 {index} 结束（{(time.monotonic() - started) / 60:.1f} 分钟）：{tail[0] if tail else ''}", flush=True)
    return outcomes, measured, process_errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, default=int(os.environ.get("CHECK_BACKEND_SHARDS", "4")))
    parser.add_argument(
        "--exclusive-shards",
        type=int,
        default=int(os.environ.get("CHECK_ACCEPTANCE_SHARDS", "2")),
        help="验收独占阶段的并行组数",
    )
    parser.add_argument("--serial-file", action="append", default=[], help="普通组结束后串行运行的容量测量文件")
    parser.add_argument("--fast", action="store_true", help="只跑快速层，排除 slow 标记")
    parser.add_argument("--isolate-files", action="store_true", help="测试文件之间恢复独立的 head 测试库")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--durations", type=Path, action="append", default=[])
    parser.add_argument("--save-durations", type=Path)
    parser.add_argument("targets", nargs="*", help="测试文件；缺省为全部")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    base_env = dict(os.environ)
    if args.isolate_files:
        base_env["CI_ISOLATE_TEST_FILES"] = "1"
    durations = {}
    for path in args.durations:
        if path.exists():
            durations.update(json.loads(path.read_text()))
    nodeids = collect(args.targets or ["tests"], base_env, args.fast)
    if not nodeids:
        print("后端：没有需要运行的测试")
        return 0
    parallel, serial = split_serial(nodeids, args.serial_file)
    regular, exclusive = split_exclusive(parallel)
    shards = max(1, args.shards)
    regular_groups = partition(regular, durations, shards) if regular else []
    exclusive_groups = partition(exclusive, durations, args.exclusive_shards) if exclusive else []
    print(
        f"后端：{len(nodeids)} 项；普通 {len(regular)} 项分 {len(regular_groups)} 组并行，"
        f"验收 {len(exclusive)} 项随后分 {len(exclusive_groups)} 组独占运行；"
        f"容量 {len(serial)} 项最后串行运行",
        flush=True,
    )
    started = time.monotonic()
    # 验收组号接在普通组之后，库名与 Redis 库号不重复。
    acceptance_url = base_env.get("ACCEPTANCE_DATABASE_URL")
    concurrent = bool(exclusive_groups and acceptance_url)
    regular_running = start_groups(regular_groups, 0, "", args, base_env)
    acceptance_running = (
        start_groups(exclusive_groups, shards, "验收", args, base_env, acceptance_url)
        if concurrent
        else []
    )
    outcomes, measured, process_errors = {}, {}, []
    if concurrent:
        more, more_measured, errors = wait_groups(acceptance_running, "验收", args, started)
        outcomes.update(more)
        measured.update(more_measured)
        process_errors.extend(errors)
    more, more_measured, errors = wait_groups(regular_running, "", args, started)
    outcomes.update(more)
    measured.update(more_measured)
    process_errors.extend(errors)
    if exclusive_groups and not concurrent:
        more, more_measured, errors = wait_groups(
            start_groups(exclusive_groups, shards, "验收", args, base_env),
            "验收",
            args,
            started,
        )
        outcomes.update(more)
        measured.update(more_measured)
        process_errors.extend(errors)
    # 所有并行进程都已退出；每个容量文件使用新进程，避免历史RSS峰值跨文件继承。
    for serial_index, path in enumerate(sorted({n.split("::", 1)[0] for n in serial})):
        selected = [n for n in serial if n.split("::", 1)[0] == path]
        more, more_measured, errors = wait_groups(
            start_groups(partition(selected, durations, 1), shards + args.exclusive_shards + serial_index,
                         "容量串行", args, base_env),
            "容量串行", args, started,
        )
        outcomes.update(more)
        measured.update(more_measured)
        process_errors.extend(errors)
    failed = sorted(n for n, o in outcomes.items() if o == "failed")
    missing = sorted(set(nodeids) - set(outcomes))
    if missing:
        # 用例未产生结果通常是进程崩溃；当作失败处理，进入重跑。
        failed = sorted(set(failed) | set(missing))
    unstable = []
    if failed and len(failed) <= MAX_RERUNS:
        print(f"后端：{len(failed)} 项并行失败，在全新库中单独重跑", flush=True)
        name = "ci_rerun_test"
        recreate_database(base_env["DATABASE_URL"], name)
        results = args.work / "rerun.jsonl"
        results.unlink(missing_ok=True)
        # 含随机值的参数化 ID 每次收集都会变化，无法按 ID 重跑；改为重跑其所在文件，
        # 只取原失败用例的结果。任一 ID 无法匹配都会让 pytest 放弃整个重跑。
        # 先收集失败用例所在文件，再判断哪些 ID 仍可直接引用。
        current = set(
            collect(sorted({n.split("::", 1)[0] for n in failed}), base_env, args.fast)
        )
        rerun_args = sorted({n if n in current else n.split("::", 1)[0] for n in failed})
        with (args.work / "rerun.log").open("w") as log:
            rerun_process = subprocess.run(
                pytest_command(rerun_args, args.fast),
                env=shard_env(base_env, shards + args.exclusive_shards + len({n.split("::", 1)[0] for n in serial}), name, results),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        rerun, _ = read_results(results)
        # 重跑整个文件时，用例 ID 中的随机参数会改变；按文件内同名函数前缀对应。
        def rerun_outcome(nodeid):
            if rerun_process.returncode != 0:
                return "failed"
            if nodeid in rerun:
                return rerun[nodeid]
            prefix = nodeid.split("[", 1)[0]
            matches = [o for n, o in rerun.items() if n.split("[", 1)[0] == prefix]
            if matches and all(o in ("passed", "skipped") for o in matches):
                return "passed"
            return "failed"

        unstable = [n for n in failed if rerun_outcome(n) == "passed"]
        failed = [n for n in failed if rerun_outcome(n) != "passed"]
    if args.save_durations:
        merged = dict(durations)
        merged.update(measured)
        args.save_durations.parent.mkdir(parents=True, exist_ok=True)
        args.save_durations.write_text(json.dumps(merged, sort_keys=True))
    passed = sum(1 for o in outcomes.values() if o == "passed")
    print(
        f"后端合计：{len(nodeids)} 项，通过 {passed}，失败 {len(failed)}，"
        f"并行不稳定 {len(unstable)}，用时 {(time.monotonic() - started) / 60:.1f} 分钟",
        flush=True,
    )
    for nodeid in unstable:
        print("  并行不稳定（单独重跑通过）：" + nodeid)
    for nodeid in failed:
        print("  失败：" + nodeid)
    for error in process_errors:
        print("  进程失败：" + error)
    if failed or process_errors:
        # check.sh 退出会回收临时目录，先保留失败复跑的尾部证据供发布排查。
        rerun_log = args.work / "rerun.log"
        if rerun_log.exists():
            print("失败复跑输出：", flush=True)
            print("\n".join(rerun_log.read_text(errors="replace").splitlines()[-80:]))
        print(f"详细输出：{args.work}/shard*.log、rerun.log")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
