#!/usr/bin/env python3
"""Checks the outputs of run_lvp_stageb.sh.  Usage: check_lvp_stageb.py OUT

OUT holds, for each run name A, B, C and CL, the directory OUT/<name> that
gem5 wrote, and OUT/<name>.out and OUT/<name>.rc with its output and exit
status.  A has LVP disabled, B has prediction only, C has address
speculation and CL is C again with the LVP debug trace.

Nothing is compared until every run has completed: each run must have
exit status 0 (as recorded by the runner in OUT/<name>.rc), its output,
statistics and trace must be regular, non-empty files that can be read as
text, and its statistics must contain the numbers that the comparisons
need.  Otherwise the checker reports what is wrong and stops with a
failure, without comparing anything.

The checks come in three groups, which are reported separately:
  - lifecycle invariants, which hold for any program;
  - expectations that come from the stable-pointer program ldld.c: every
    slot holds the same pointer and sum_slots() has no branch or fault
    between the two loads, so a speculative load is never squashed or
    faulting and no prediction is wrong.  These are NOT universal, and a
    failure means either the program or the implementation differs from
    what was assumed;
  - performance, which is only reported, and never changes the result.
"""

import filecmp
import math
import os
import re
import sys

NUM_SLOTS = 16384
EXPECTED_CHECKSUM = 7 * NUM_SLOTS

# The statistics that the comparisons use
BASE_STATS = ["simInsts", "system.cpu.numCycles"]
LVP_STATS = [
    "lookups",
    "confidentPredictions",
    "trainings",
    "correct",
    "incorrect",
    "predFaulted",
    "predSquashed",
    "predInFlight",
    "predCarryIn",
    "specLoadsIssued",
    "specLoadsCommitted",
    "specLoadsFaulted",
    "specLoadsSquashed",
    "specInFlight",
    "specCarryIn",
]

# Instructions that read memory, as the commit trace names them
LOAD_MNEMONIC = re.compile(
    r"(l[bhwd]u?|fl[wd]|lr\.[wd]|amo\w+\.[wd]|" r"sc\.[wd])$"
)

fails = []


def check(cond, msg, failures=None):
    """Report a check.  A failure is added to failures (by default the
    checker's own list)"""
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        (fails if failures is None else failures).append(msg)


def report(msg):
    print("INFO  " + msg)


def section(title):
    print()
    print("== " + title)


def trace_inventory(path):
    """What a commit-only execution trace says was committed: the number of
    instructions, how many times each load PC committed, and the lines that
    are not an instruction.  None if the trace can't be read.  A line is
        system.cpu: 0x<pc>   : <mnemonic> <operands> : ...
    """
    inv = {"instructions": 0, "loads": {}, "unparsed": []}
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                m = re.search(r"\b0x([0-9a-f]+)\s+:\s+(\S+)", line)
                if not m:
                    inv["unparsed"].append(line.strip())
                    continue
                inv["instructions"] += 1
                if LOAD_MNEMONIC.match(m.group(2)):
                    pc = int(m.group(1), 16)
                    inv["loads"][pc] = inv["loads"].get(pc, 0) + 1
    except (OSError, UnicodeDecodeError) as e:
        report("cannot read %s: %s" % (path, e))
        return None
    return inv


def read_stats(out, name):
    """All statistics except the host ones, as {name: value string}"""
    stats = {}
    with open(os.path.join(out, name, "stats.txt")) as f:
        for line in f:
            tokens = line.split()
            if (
                len(tokens) < 2
                or line.startswith("-")
                or tokens[0].startswith("host")
            ):
                continue
            stats[tokens[0]] = tokens[1]
    return stats


def lvp(stats):
    return {
        k.split("cpu.lvp.", 1)[1]: int(float(v))
        for k, v in stats.items()
        if "cpu.lvp." in k
    }


def checksum(out, name):
    with open(os.path.join(out, name + ".out")) as f:
        for line in f:
            m = re.match(r"checksum (\d+)\s*$", line)
            if m:
                return int(m.group(1))
    return None


def file_problem(path):
    """Why path can't be used as a text input of the comparisons, or None.
    The whole file is read so that anything the later code reads, in the
    same way, can't fail."""
    if not os.path.exists(path):
        return "missing"
    if not os.path.isfile(path):
        return "not a regular file"
    try:
        if os.path.getsize(path) == 0:
            return "empty"
        with open(path) as f:
            for _ in f:
                pass
    except (OSError, ValueError) as e:  # ValueError: not decodable text
        return "cannot be read (%s)" % e
    return None


def number_problems(stats, integer_keys, count_keys):
    """The statistics that are not numbers the comparisons can use.  The
    keys in integer_keys must be integer literals, because they are
    converted with int(), and the keys in count_keys must have an integral
    value, because they are converted with int(float()): a malformed value
    must not be silently truncated."""
    bad = []
    for k in integer_keys + count_keys:
        try:
            v = float(stats[k])
            if not math.isfinite(v):
                raise ValueError("not finite")
            if k in integer_keys:
                int(stats[k])
            elif not v.is_integer():
                raise ValueError("not integral")
        except ValueError:
            bad.append("%s = %s" % (k, stats[k]))
    return bad


def preflight(out, names, lvp_runs):
    """Checks that every run completed and wrote what the comparisons need.
    Returns (the statistics of each run, []) or (None, the problems found):
    the problems are collected and reported here, independently of the
    checker's other results, and the statistics are never partial."""
    problems = []
    stats = {}

    def need(cond, msg):
        check(cond, msg, problems)

    for n in names:
        # The exit status the runner recorded, not inferred from the files
        rc_path = os.path.join(out, n + ".rc")
        try:
            with open(rc_path) as f:
                status = int(f.read().strip())
            need(status == 0, "%s: gem5 exit status 0 (got %d)" % (n, status))
        except (OSError, ValueError) as e:
            need(
                False, "%s: exit status recorded in %s (%s)" % (n, rc_path, e)
            )

        files = {
            "output": os.path.join(out, n + ".out"),
            "stats.txt": os.path.join(out, n, "stats.txt"),
            "trace.txt": os.path.join(out, n, "trace.txt"),
        }
        usable = {}
        for what, path in files.items():
            why = file_problem(path)
            usable[what] = why is None
            need(
                why is None,
                "%s: %s is a non-empty text file%s"
                % (n, what, "" if why is None else " (%s: %s)" % (path, why)),
            )

        if not usable["stats.txt"]:
            continue  # reported above
        try:
            stats[n] = read_stats(out, n)
        except (OSError, ValueError) as e:
            need(
                False,
                "%s: stats.txt can be parsed (%s: %s)"
                % (n, files["stats.txt"], e),
            )
            continue

        wanted = (
            ["system.cpu.lvp." + k for k in LVP_STATS] if n in lvp_runs else []
        )
        missing = [k for k in BASE_STATS + wanted if k not in stats[n]]
        need(
            not missing,
            "%s: statistics present%s"
            % (n, "" if not missing else ", missing: " + ", ".join(missing)),
        )
        if missing:
            continue

        # lvp() converts every cpu.lvp statistic there is, wanted or not
        lvp_stats = [k for k in stats[n] if "cpu.lvp." in k]
        bad = number_problems(stats[n], BASE_STATS, lvp_stats)
        need(
            not bad,
            "%s: statistics are finite numbers%s"
            % (n, "" if not bad else ", not: " + "; ".join(bad)),
        )

    if problems or set(names) - set(stats):
        return None, problems or ["incomplete statistics"]
    return stats, []


def main():
    out = sys.argv[1]
    names = ["A", "B", "C", "CL"]

    section("Preflight: every run completed and wrote its outputs")

    stats, problems = preflight(out, names, ["B", "C", "CL"])
    if stats is None:
        print()
        print(
            "%d preflight check(s) failed: nothing was compared"
            % len(problems)
        )
        return 1

    section("Architectural correctness")

    for n in names:
        check(
            checksum(out, n) == EXPECTED_CHECKSUM,
            "%s: checksum is %d" % (n, EXPECTED_CHECKSUM),
        )

    # Prediction-only must not change anything but the LVP statistics
    non_lvp = {
        n: {k: v for k, v in stats[n].items() if "cpu.lvp." not in k}
        for n in names
    }
    check(
        non_lvp["A"] == non_lvp["B"],
        "A and B: identical statistics apart from cpu.lvp.*",
    )

    # The committed instruction stream, with its results and addresses
    for n in ["B", "C"]:
        same = filecmp.cmp(
            os.path.join(out, "A", "trace.txt"),
            os.path.join(out, n, "trace.txt"),
            shallow=False,
        )
        check(same, "A and %s: identical committed trace" % n)
    report(
        "trace size: %d bytes"
        % os.path.getsize(os.path.join(out, "A", "trace.txt"))
    )

    b = lvp(stats["B"])
    c = lvp(stats["C"])

    section("Lifecycle invariants (any program)")

    check(b["specLoadsIssued"] == 0, "B: no speculative loads")

    for n, s in (("B", b), ("C", c)):
        check(
            s["predCarryIn"] == 0 and s["specCarryIn"] == 0,
            "%s: no live work at any statistics reset" % n,
        )
        check(
            s["confidentPredictions"] + s["predCarryIn"]
            == s["correct"]
            + s["incorrect"]
            + s["predFaulted"]
            + s["predSquashed"]
            + s["predInFlight"],
            "%s: prediction outcomes add up" % n,
        )
        check(
            s["specLoadsIssued"] + s["specCarryIn"]
            == s["specLoadsCommitted"]
            + s["specLoadsFaulted"]
            + s["specLoadsSquashed"]
            + s["specInFlight"],
            "%s: speculative load outcomes add up" % n,
        )
        check(
            s["lookups"]
            >= s["confidentPredictions"]
            >= s["correct"] + s["incorrect"],
            "%s: lookups >= confident >= validated" % n,
        )
        check(
            s["lookups"] >= s["trainings"] >= s["correct"] + s["incorrect"],
            "%s: lookups >= trainings >= validated" % n,
        )

    check(b["trainings"] == c["trainings"], "B and C: same trainings")

    # Every speculative load is accepted before its producer responds.
    # Both are identified by their thread and execSeqNum, which are unique,
    # taken from the instruction id the LVP trace prints:
    #   thread/stream.prediction/line/fetch.exec pc: 0x... (name ...)
    inst_id = r"(\d+)/(\d+)\.(\d+)/(\d+)/(\d+)\.(\d+) pc: 0x([0-9a-f]+)"
    acc_re = re.compile(
        r"^\s*(\d+):.*Speculative load accepted inst: "
        + inst_id
        + r" .*producer: (\d+) source"
    )
    val_re = re.compile(
        r"^\s*(\d+):.*Validated inst: " + inst_id + r" .*exec: (\d+) predicted"
    )
    accepted = []  # (tick, (thread, exec), (thread, producer exec))
    validated = {}  # (thread, exec) -> tick
    duplicate_validations = []
    id_mismatches = []
    with open(os.path.join(out, "CL", "trace.txt")) as f:
        for line in f:
            m = acc_re.match(line)
            if m:
                g = m.groups()
                accepted.append(
                    (int(g[0]), (int(g[1]), int(g[6])), (int(g[1]), int(g[8])))
                )
                continue
            m = val_re.match(line)
            if m:
                g = m.groups()
                key = (int(g[1]), int(g[6]))
                # The id's exec number and the logged one are the same
                if int(g[6]) != int(g[8]):
                    id_mismatches.append(line.strip())
                if key in validated:
                    duplicate_validations.append(key)
                validated[key] = int(g[0])

    check(
        len(accepted) == lvp(stats["CL"])["specLoadsIssued"],
        "CL: one log line per speculative load (%d)" % len(accepted),
    )
    check(
        len(set(a[1] for a in accepted)) == len(accepted),
        "CL: no speculative load accepted twice",
    )
    check(not duplicate_validations, "CL: no instruction validated twice")
    check(
        not id_mismatches,
        "CL: a validation's instruction id agrees with its logged exec "
        "number (%d disagree)" % len(id_mismatches),
    )
    for line in id_mismatches[:5]:
        report("disagreement: " + line)

    matched = [
        (t, cons, prod) for t, cons, prod in accepted if prod in validated
    ]
    late = [m for m in matched if not m[0] < validated[m[2]]]
    check(
        not late,
        "CL: every speculative load is accepted before its "
        "producer responds (%d late)" % len(late),
    )
    check(
        all(prod[1] < cons[1] for _, cons, prod in matched),
        "CL: every producer is older than its speculative load",
    )

    section(
        "Expectations from the stable-pointer program ldld.c "
        "(not universal invariants)"
    )

    check(
        c["specLoadsIssued"] > 0,
        "C: speculative loads issued (%d)" % c["specLoadsIssued"],
    )
    check(
        c["specLoadsCommitted"] > 0,
        "C: speculative loads committed (%d)" % c["specLoadsCommitted"],
    )
    check(c["correct"] > 0, "C: correct predictions (%d)" % c["correct"])
    check(
        c["incorrect"] == 0,
        "C: no incorrect predictions (all loaded values are constant)",
    )
    check(
        c["specLoadsSquashed"] == 0
        and c["specLoadsFaulted"] == 0
        and c["specInFlight"] == 0,
        "C: no speculative load squashed, faulted or in flight at the end "
        "(squashed %d, faulted %d, in flight %d)"
        % (c["specLoadsSquashed"], c["specLoadsFaulted"], c["specInFlight"]),
    )
    check(
        c["specLoadsCommitted"] == c["specLoadsIssued"],
        "C: every speculative load committed (%d of %d)"
        % (c["specLoadsCommitted"], c["specLoadsIssued"]),
    )
    check(
        len(matched) > 0,
        "CL: found producer responses (%d of %d)"
        % (len(matched), len(accepted)),
    )
    check(
        len(matched) == len(accepted),
        "CL: every speculative load matches a validation of its "
        "producer (%d of %d)" % (len(matched), len(accepted)),
    )

    # The loads that commit, from A's commit-only execution trace.  The
    # expectation that no prediction is wrong covers every load in the run,
    # so these are the loads of sum_slots and nothing else.
    inv = trace_inventory(os.path.join(out, "A", "trace.txt"))
    check(
        inv is not None and inv["instructions"] > 0 and not inv["unparsed"],
        "A trace: every line is a committed instruction (%s)"
        % (
            "unreadable"
            if inv is None
            else "%d instructions, %d other lines"
            % (inv["instructions"], len(inv["unparsed"]))
        ),
    )
    if inv is not None:
        for line in inv["unparsed"][:5]:
            report("unparsed trace line: " + line)
        check(
            inv["instructions"] == int(stats["A"]["simInsts"]),
            "A trace: one line for each committed instruction (trace %d, "
            "simInsts %s)" % (inv["instructions"], stats["A"]["simInsts"]),
        )
        total = sum(inv["loads"].values())
        check(
            total == 2 * NUM_SLOTS,
            "A trace: exactly %d committed loads (found %d)"
            % (2 * NUM_SLOTS, total),
        )
        check(
            len(inv["loads"]) == 2,
            "A trace: exactly two distinct load PCs (found %d)"
            % len(inv["loads"]),
        )
        by_pc = sorted(inv["loads"].items())[:8]
        report(
            "committed loads by PC: "
            + ", ".join("0x%x x%d" % (pc, n) for pc, n in by_pc)
        )

    # Performance is reported but never part of the result above
    section("Performance (reported only, not part of the result)")

    cycles = {n: int(stats[n]["system.cpu.numCycles"]) for n in names}
    insts = int(stats["C"]["simInsts"])
    report("cycles: A %d B %d C %d" % (cycles["A"], cycles["B"], cycles["C"]))
    report(
        "IPC: A %.4f B %.4f C %.4f"
        % tuple(insts / cycles[n] for n in ("A", "B", "C"))
    )
    report(
        "C coverage: %d of %d loop iterations used a speculative load "
        "(%.1f%%)"
        % (
            c["specLoadsCommitted"],
            NUM_SLOTS,
            100.0 * c["specLoadsCommitted"] / NUM_SLOTS,
        )
    )
    if matched:
        gaps = sorted(validated[prod] - t for t, _, prod in matched)
        report(
            "ticks from acceptance to producer validation: "
            "min %d median %d max %d"
            % (gaps[0], gaps[len(gaps) // 2], gaps[-1])
        )
    if cycles["C"] < cycles["B"]:
        saved = cycles["B"] - cycles["C"]
        print(
            "RESULT  speedup measured: C is %d cycles (%.2f%%) faster "
            "than B" % (saved, 100.0 * saved / cycles["B"])
        )
        if c["specLoadsCommitted"]:
            report(
                "cycles saved per committed speculative load: %.2f"
                % (saved / c["specLoadsCommitted"])
            )
    else:
        print(
            "RESULT  no speedup measured in this configuration "
            "(C %d cycles, B %d cycles)" % (cycles["C"], cycles["B"])
        )

    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
