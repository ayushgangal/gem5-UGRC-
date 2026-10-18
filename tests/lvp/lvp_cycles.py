#!/usr/bin/env python3
"""Reports the cycles of tests/lvp/ldld.c (stable) with LVP off and on.
Usage: lvp_cycles.py RUNS

RUNS holds, for each run OFF, CTRL (prediction only) and ON (speculation),
the directory RUNS/<name> that gem5 wrote and RUNS/<name>.out and
RUNS/<name>.rc with its output and exit status (see run_lvp_cycles.sh).

The cycle count is `system.cpu.numCycles`, the cycles of the simulated CPU
(not host time, and not simTicks, which is the same count times the clock
period).  The checks are about correctness and about the comparison being
fair; the cycle saving is measured and reported, never asserted.
"""

import math
import os
import re
import sys

NAMES = ["OFF", "CTRL", "ON"]
CYCLES = "system.cpu.numCycles"
EXPECTED_CHECKSUM = 7 * 16384  # ldld.c: every one of 16384 loads reads 7
LVP_STATS = [
    "lookups",
    "confidentPredictions",
    "trainings",
    "correct",
    "incorrect",
    "predSquashed",
    "specLoadsIssued",
    "specLoadsCommitted",
    "specLoadsSquashed",
    "specLoadsFaulted",
    "mispredictSquashes",
]

fails = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def read_stats(path):
    stats = {}
    with open(path) as f:
        for line in f:
            t = line.split()
            if len(t) >= 2 and not line.startswith("-"):
                stats[t[0]] = t[1]
    return stats


def num(stats, key):
    v = float(stats[key])
    if not math.isfinite(v):
        raise ValueError(key)
    return int(v)


def checksum(path):
    with open(path) as f:
        for line in f:
            m = re.match(r"checksum (\d+)\s*$", line)
            if m:
                return int(m.group(1))
    return None


def read_config(path):
    with open(path) as f:
        return f.read().splitlines()


def cpu_param(lines, key):
    inside = False
    for line in lines:
        if line.startswith("["):
            inside = line.strip() == "[system.cpu]"
        elif inside and line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    runs = sys.argv[1]
    data = {}

    print("== Runs")
    for n in NAMES:
        rc_path = os.path.join(runs, n + ".rc")
        try:
            with open(rc_path) as f:
                rc = int(f.read().strip())
            stats = read_stats(os.path.join(runs, n, "stats.txt"))
            cfg = read_config(os.path.join(runs, n, "config.ini"))
            data[n] = {
                "rc": rc,
                "stats": stats,
                "cfg": cfg,
                "checksum": checksum(os.path.join(runs, n + ".out")),
                "cycles": num(stats, CYCLES),
                "insts": num(stats, "simInsts"),
            }
        except (OSError, ValueError, KeyError) as e:
            check(False, "%s: outputs readable (%s)" % (n, e))
            continue
        check(rc == 0, "%s: gem5 exit status 0 (got %d)" % (n, rc))
    if len(data) != len(NAMES) or any(d["rc"] != 0 for d in data.values()):
        print("\nnothing was compared")
        return 1

    print("\n== The comparison is fair")
    for n in NAMES:
        d = data[n]
        enable = cpu_param(d["cfg"], "enableLVP")
        spec = cpu_param(d["cfg"], "lvpSpecAddr")
        astra = cpu_param(d["cfg"], "enableAstra")
        want = {
            "OFF": ("false", "false"),
            "CTRL": ("true", "false"),
            "ON": ("true", "true"),
        }[n]
        check(
            (enable, spec) == want,
            "%s: enableLVP=%s lvpSpecAddr=%s in config.ini (expected %s %s)"
            % (n, enable, spec, want[0], want[1]),
        )
        check(
            astra in (None, "false"),
            "%s: ASTRA disabled (enableAstra is %s in config.ini)"
            % (
                n,
                "absent: this gem5 has no ASTRA" if astra is None else astra,
            ),
        )
    # The only other difference is each run's own output directory, which
    # gem5 writes into the paths of its fake /proc, /sys and /tmp
    skip = ("enableLVP=", "lvpSpecAddr=")

    def comparable(n):
        return [
            l.replace("/runs/%s/" % n, "/runs/RUN/")
            for l in data[n]["cfg"]
            if not l.startswith(skip)
        ]

    for n in ("CTRL", "ON"):
        check(
            comparable(n) == comparable("OFF"),
            "OFF and %s: config.ini identical apart from the LVP "
            "parameters and the run's own output directory (executable, "
            "CPU, caches, clocks, memory)" % n,
        )

    print("\n== Architectural equivalence")
    for n in NAMES:
        check(
            data[n]["checksum"] == EXPECTED_CHECKSUM,
            "%s: checksum is %s (expected %d = 7 x 16384, from the "
            "program)" % (n, data[n]["checksum"], EXPECTED_CHECKSUM),
        )
    for n in ("CTRL", "ON"):
        check(
            data[n]["insts"] == data["OFF"]["insts"],
            "OFF and %s: same committed instruction count (%d and %d)"
            % (n, data["OFF"]["insts"], data[n]["insts"]),
        )

    print("\n== LVP activity")
    lvp = {}
    for n in ("CTRL", "ON"):
        try:
            lvp[n] = {
                k: num(data[n]["stats"], "system.cpu.lvp." + k)
                for k in LVP_STATS
            }
        except (KeyError, ValueError) as e:
            check(False, "%s: lvp statistics present (%s)" % (n, e))
            return 1
    check(
        not any(".lvp." in k for k in data["OFF"]["stats"]),
        "OFF: no LVP statistics (LVP disabled)",
    )
    check(
        lvp["CTRL"]["specLoadsIssued"] == 0
        and lvp["CTRL"]["confidentPredictions"] > 0,
        "CTRL: confident predictions (%d) but no speculative load"
        % lvp["CTRL"]["confidentPredictions"],
    )
    on = lvp["ON"]
    check(
        on["specLoadsIssued"] > 0 and on["specLoadsCommitted"] > 0,
        "ON: speculation happened: %d speculative loads issued, %d committed"
        % (on["specLoadsIssued"], on["specLoadsCommitted"]),
    )
    check(
        on["incorrect"] == 0
        and on["mispredictSquashes"] == 0
        and on["specLoadsSquashed"] == 0,
        "ON: no wrong prediction, redirect or speculative squash, as "
        "expected for the stable workload",
    )

    print("\n== Cycles (system.cpu.numCycles)")
    off, ctrl, onc = (data[n]["cycles"] for n in NAMES)
    print("INFO  OFF  (LVP disabled)            %12d cycles" % off)
    print("INFO  CTRL (prediction only)         %12d cycles" % ctrl)
    print("INFO  ON   (speculation)             %12d cycles" % onc)
    saved = off - onc
    print(
        "INFO  committed instructions: OFF %d, ON %d"
        % (data["OFF"]["insts"], data["ON"]["insts"])
    )
    check(
        ctrl == off,
        "CTRL costs nothing: prediction only has the cycles of OFF "
        "(%d and %d)" % (ctrl, off),
    )
    print()
    if saved > 0:
        print(
            "RESULT  LVP saved %d cycles (%.3f%% of %d): %d -> %d"
            % (saved, 100.0 * saved / off, off, off, onc)
        )
        print(
            "INFO  per committed speculative load: %.2f cycles; per "
            "iteration of the loop (16384): %.2f of %.1f cycles"
            % (saved / on["specLoadsCommitted"], saved / 16384, off / 16384)
        )
    else:
        print(
            "RESULT  no cycle reduction measured (OFF %d, ON %d, "
            "difference %d)" % (off, onc, saved)
        )

    print()
    print(
        "%d check(s) failed" % len(fails)
        if fails
        else "all checks passed (the cycle saving is a measurement)"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
