"""Run bounded TLC baselines and require named invariant counterexamples."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("FORMAL_OUT", tempfile.mkdtemp(prefix="jb16-formal-")))
OUT.mkdir(parents=True, exist_ok=True)
jar = Path(os.environ["TLA_JAR"])
assert (
    hashlib.sha256(jar.read_bytes()).hexdigest()
    == "e1f11b75c4b5bc4478ba0ac5cb6ae28e40a01b0016bc33e0aa97da22dfdbcd7f"
)


def run(module, bug, constants, invariants):
    name = f"{module}-{bug}-" + constants.replace(" ", "")
    with tempfile.TemporaryDirectory(prefix="jb16-tlc-") as temp:
        shutil.copyfile(HERE / f"{module}.tla", Path(temp) / f"{module}.tla")
        cfg = Path(temp) / "run.cfg"
        cfg.write_text(
            f'CONSTANT Bug = "{bug}" {constants}\nINIT Init\nNEXT Next\nINVARIANT {invariants}\nCHECK_DEADLOCK FALSE\n'
        )
        result = subprocess.run(
            [
                os.environ["TLA_JAVA"],
                "-XX:+UseParallelGC",
                "-cp",
                str(jar),
                "tlc2.TLC",
                "-workers",
                "1",
                "-config",
                str(cfg),
                "-metadir",
                temp,
                str(Path(temp) / f"{module}.tla"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        log = result.stdout + result.stderr
        (OUT / f"{name}.log").write_text(log)
        if bug == "none":
            assert result.returncode == 0 and "No error has been found" in log, log[
                -4000:
            ]
        else:
            assert (
                result.returncode != 0 and "Invariant " in log and "is violated" in log
            ), log[-4000:]
        print(
            name,
            [
                line
                for line in log.splitlines()
                if "states generated," in line or "is violated" in line
            ],
            flush=True,
        )


if os.environ.get("FORMAL_ONLY") == "historical-witness":
    run("Submission", "witness", "N = 2 K = 3", "NoHistoricalTerminal")
    raise SystemExit(0)

if os.environ.get("FORMAL_ONLY") == "historical":
    for n in (1, 2):
        run(
            "Submission",
            "none",
            f"N = {n} K = 3",
            "UniqueSubmission WriteOnce FrozenBody ExactBudget Excluded Completion Recovery NegativeProof\nPROPERTY HistoricalPreservation",
        )
    run("Submission", "witness", "N = 2 K = 3", "NoHistoricalTerminal")
    for bug, invariant in [
        ("negativeproof", "NegativeProof"),
        ("recovery", "Recovery"),
        ("retry", "WriteOnce"),
        ("completion", "Completion"),
    ]:
        run("Submission", bug, "N = 1 K = 3", invariant)
    raise SystemExit(0)

if os.environ.get("FORMAL_ONLY") == "recovery":
    run(
        "Submission",
        "none",
        "N = 1 K = 3",
        "UniqueSubmission WriteOnce FrozenBody ExactBudget Excluded Completion Recovery NegativeProof",
    )
    run(
        "Submission",
        "none",
        "N = 2 K = 3",
        "UniqueSubmission WriteOnce FrozenBody ExactBudget Excluded Completion Recovery NegativeProof",
    )
    run("Submission", "recovery", "N = 1 K = 3", "Recovery")
    run("Submission", "retry", "N = 1 K = 2", "WriteOnce")
    run("Submission", "completion", "N = 1 K = 2", "Completion")
    run("Authority", "none", "", "FinalFence HeldAuthority")
    raise SystemExit(0)

for n, k in [(1, 4), (2, 3)]:
    run(
        "Submission",
        "none",
        f"N = {n} K = {k}",
        "UniqueSubmission WriteOnce FrozenBody ExactBudget Excluded Completion Recovery NegativeProof",
    )
for bug, inv in [
    ("recovery", "Recovery"),
    ("budget", "ExactBudget"),
    ("unique", "UniqueSubmission"),
    ("excluded", "Excluded"),
    ("retry", "WriteOnce"),
    ("body", "FrozenBody"),
    ("completion", "Completion"),
]:
    run("Submission", bug, "N = 1 K = 2", inv)
run("Authority", "none", "", "FinalFence HeldAuthority")
for bug in ["stamp", "route", "null", "lock", "aba"]:
    run("Authority", bug, "", "HeldAuthority" if bug == "lock" else "FinalFence")
print(f"Evidence: {OUT}")
