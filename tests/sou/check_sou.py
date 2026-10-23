#!/usr/bin/env python3
"""Checks of the stall-on-use baseline characterization (run_sou.sh).
Usage: check_sou.py OUT

OUT/b (canonical baseline) and OUT/s (configs/research/vp_stall_on_use.py)
hold gem5's output with the MinorExecute, MinorMem and Exec traces, and
OUT/b.out, OUT/s.out, OUT/b.rc, OUT/s.rc, OUT/pcs.txt (symbol, pc).
The criterion is the observed timing in the traces, not the name of the
parameter: when a dependent is issued relative to its producer's issue, data
arrival and commit, and whether independent instructions keep issuing."""

import os
import re
import sys

failures = 0


def check(cond, msg):
    global failures
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failures += 1


def info(msg):
    print("INFO  " + msg)


ID = r"(\d+/\d+\.\d+/\d+/(\d+)\.\d+) pc: (0x[0-9a-f]+)"
RX = {
    "issue": re.compile(
        r"^\s*(\d+): system\.cpu\.execute: Issuing inst: " + ID
    ),
    "commit": re.compile(
        r"^\s*(\d+): system\.cpu\.execute: Committing inst: " + ID
    ),
    "resp": re.compile(
        r"^\s*(\d+): system\.cpu\.execute: Memory response inst: " + ID
    ),
    "arrive": re.compile(
        r"^\s*(\d+): system\.cpu\.execute\.lsq: Received response packet inst: "
        + ID
    ),
}
COMMIT_LINE = re.compile(r"^\s*(\d+): system\.cpu: (0x[0-9a-f]+)\s+:(.*)$")


def read_pcs(path):
    return {k: int(v, 16) for k, v in (l.split() for l in open(path))}


class Trace:
    def __init__(self, path, pcs):
        ev = {}  # exec seq -> {kind: tick, 'pc': pc}
        self.exec_lines = []
        with open(path, errors="replace") as f:
            for line in f:
                m = COMMIT_LINE.match(line)
                if m:
                    self.exec_lines.append(m.group(2) + m.group(3))
                    continue
                for kind, rx in RX.items():
                    m = rx.match(line)
                    if m:
                        e = ev.setdefault(int(m.group(3)), {})
                        e[kind] = int(m.group(1))
                        e["pc"] = int(m.group(4), 16)
                        break
        # committed: a commit line or a memory response, and an issue
        self.byname = {n: [] for n in pcs}
        by_pc = {pc: n for n, pc in pcs.items()}
        for seq in sorted(ev):
            e = ev[seq]
            done = e.get("commit", e.get("resp"))
            if done is None or "issue" not in e or e["pc"] not in by_pc:
                continue
            self.byname[by_pc[e["pc"]]].append(
                {
                    "issue": e["issue"] // 1000,
                    "commit": done // 1000,
                    "arrive": e["arrive"] // 1000 if "arrive" in e else None,
                }
            )


def per_iter(tr, sec, names):
    n = min(len(tr.byname[sec + x]) for x in names)
    return [{x: tr.byname[sec + x][i] for x in names} for i in range(n)]


def main(out):
    pcs = read_pcs(os.path.join(out, "pcs.txt"))
    rc = {v: int(open(os.path.join(out, v + ".rc")).read()) for v in "bs"}
    check(
        rc["b"] == 0 and rc["s"] == 0, "both configurations run to completion"
    )
    outs = {
        v: re.findall(
            r"checksum (\d+)",
            open(os.path.join(out, v + ".out"), errors="replace").read(),
        )
        for v in "bs"
    }
    check(
        outs["b"] and outs["b"] == outs["s"],
        "architectural output identical (checksum %s)" % outs["b"],
    )
    tr = {v: Trace(os.path.join(out, v, "trace.txt"), pcs) for v in "bs"}
    check(
        len(tr["b"].exec_lines) > 0
        and tr["b"].exec_lines == tr["s"].exec_lines,
        "committed instruction stream identical (%d instructions, "
        "ticks removed)" % len(tr["b"].exec_lines),
    )
    cyc = {}
    for v in "bs":
        for l in open(os.path.join(out, v, "stats.txt")):
            if l.startswith("system.cpu.numCycles"):
                cyc[v] = int(l.split()[1])
                break
    info(
        "numCycles (supporting information only): baseline %d, stall-on-use %d"
        % (cyc["b"], cyc["s"])
    )

    names_a = ["L", "I1", "I2", "I3", "D", "J"]
    names_b = ["M", "L", "I1", "I2", "I3", "D", "J"]
    for v, label in (("b", "baseline"), ("s", "stall-on-use")):
        a = per_iter(tr[v], "soa_", names_a)
        b = per_iter(tr[v], "sob_", names_b)
        n_expected = 12
        check(
            len(a) == n_expected and len(b) == n_expected,
            "%s: all %d iterations of both sections found in the trace"
            % (label, n_expected),
        )
        # The first iteration of each section runs cold code (instruction
        # cache misses delay its younger instructions' issue): the timing
        # checks use iterations 1..11
        a_all, b_all = a, b
        a, b = a[1:], b[1:]
        lat = [it["L"]["commit"] - it["L"]["issue"] for it in a]
        check(
            min(lat) >= 50,
            "%s A: the load is long-latency (issue to "
            "commit %d..%d cycles)" % (label, min(lat), max(lat)),
        )
        dl = [it["D"]["issue"] - it["L"]["issue"] for it in a]
        d_vs_c = [it["D"]["issue"] - it["L"]["commit"] for it in a]
        i_vs_c = [
            max(it[x]["issue"] for x in ("I1", "I2", "I3")) - it["L"]["commit"]
            for it in a
        ]
        j_vs_c = [it["J"]["issue"] - it["L"]["commit"] for it in a]
        j_vs_d = [it["J"]["issue"] - it["D"]["issue"] for it in a]
        info(
            "%s A: D issue - L issue = %s; D issue - L commit = %s; "
            "J issue - L commit = %s"
            % (
                label,
                sorted(set(dl)),
                sorted(set(d_vs_c)),
                sorted(set(j_vs_c)),
            )
        )
        if v == "b":
            check(
                all(x <= 3 for x in dl),
                "baseline A: the dependent issues within 3 cycles of the "
                "load's issue (the assumed latency), long before the "
                "load's result",
            )
            check(
                all(x < -40 for x in d_vs_c),
                "baseline A: the dependent issues >40 cycles before the "
                "load commits",
            )
            check(
                all(x < -40 for x in j_vs_c),
                "baseline A: the independent instruction after it also "
                "issues far before the load commits",
            )
        else:
            check(
                all(x >= 0 for x in d_vs_c),
                "stall-on-use A: the dependent never issues before the "
                "load commits (D issue - L commit = %s cycles)"
                % sorted(set(d_vs_c)),
            )
            check(
                all(x < 0 for x in i_vs_c),
                "stall-on-use A: the three independent instructions "
                "between the load and its use do issue before the load "
                "commits",
            )
            check(
                all(x >= 0 for x in j_vs_d) and all(x >= 0 for x in j_vs_c),
                "stall-on-use A: the independent instruction after the "
                "stalled dependent is held behind it (in-order issue): "
                "J issue >= D issue and >= L commit",
            )
        # Section B: M older, L hits in the L1
        arr = [
            it["L"]["commit"] - it["L"]["arrive"]
            if it["L"]["arrive"] is not None
            else None
            for it in b
        ]
        gap = [x for x in arr if x is not None]
        d_vs_arr = [
            it["D"]["issue"] - it["L"]["arrive"]
            for it in b
            if it["L"]["arrive"] is not None
        ]
        d_vs_c = [it["D"]["issue"] - it["L"]["commit"] for it in b]
        info(
            "%s B: L data arrives %s cycles before L commits; D issue - L "
            "commit = %s" % (label, sorted(set(gap)), sorted(set(d_vs_c)))
        )
        check(
            len(gap) == len(b) and min(gap) >= 30,
            "%s B: the load's data arrives long before the load can "
            "commit (older miss holds the head): %d..%d cycles"
            % (label, min(gap), max(gap)),
        )
        if v == "b":
            check(
                all(x < 0 for x in d_vs_c),
                "baseline B: the dependent issues before the load commits",
            )
        else:
            check(
                all(x >= 0 for x in d_vs_c) and all(x > 20 for x in d_vs_arr),
                "stall-on-use B: the dependent issues at the load's "
                "COMMIT, not at the arrival of its data (D issue - L data "
                "arrival >= %d cycles)" % min(d_vs_arr),
            )
    info(
        "conclusion: the dependents are held until the producer's in-order "
        "commit (not until its data arrives), which is stall-on-use in "
        "Minor's model but not identical to the paper's"
    )
    if failures:
        print("%d correctness check(s) failed" % failures)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1])
