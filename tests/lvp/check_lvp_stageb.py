#!/usr/bin/env python3
"""Checks the outputs of run_lvp_stageb.sh.
Usage: check_lvp_stageb.py OUT [stable|phase|fanout]

The workload is stable (every slot holds the same pointer, the default) or
phase (the pointer alternates between two nodes in blocks of 64 slots, so
confident predictions are wrong at the start of each block and recovery is
exercised) or fanout (phase, with two dependent loads from each pointer, so
that one wrong prediction can be used by two speculative loads); see ldld.c.

OUT holds, for each run name A, B, C and CL, the directory OUT/<name> that
gem5 wrote, and OUT/<name>.out and OUT/<name>.rc with its output and exit
status.  A has LVP disabled, B has prediction only, C has address
speculation and CL is C again with the LVP debug trace.  The stable
workload's expectations are in one group of checks and the phase
workload's in another; only one of them applies to a run.

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

import collections
import filecmp
import math
import os
import re
import sys

NUM_SLOTS = 16384
PHASE_BLOCK = 64
# stable: every node value is 7.  phase: blocks of 64 slots alternate
# between nodes holding 7 and 11
# fanout: the phase-change pointers, and each pointer's node has two values
# that are loaded by two different loads.  The program adds v[3] and
# v[2] << 32, so the two consumers' contributions are separate fields of
# the checksum.  The values below are those ldld.c gives the nodes
# (v[2], v[3]) of node and node_b.
FANOUT_NODES = ((5, 7), (2, 11))


def fanout_sums():
    """(sum of the v[3] values, sum of the v[2] values) over all the slots,
    computed from the layout of the slots and the node values, not from any
    simulation output"""
    lo = hi = 0
    for i in range(NUM_SLOTS):
        v2, v3 = FANOUT_NODES[(i // PHASE_BLOCK) & 1]
        lo += v3
        hi += v2
    return lo, hi


EXPECTED_CHECKSUMS = {
    "stable": 7 * NUM_SLOTS,
    "phase": (7 + 11) * (NUM_SLOTS // 2),
    "fanout": fanout_sums()[0] + (fanout_sums()[1] << 32),
}
# The loads in sum_slots() for each slot: the producer and its consumers
LOADS_PER_SLOT = {"stable": 2, "phase": 2, "fanout": 3}

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
    "mispredictSquashes",
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


def read_load_pcs(out, workload):
    """The producer's PC and the consumers' PCs, as the assembly check
    (run_lvp_stageb.sh) wrote them from the binary's disassembly: (producer,
    [consumers]), or (None, why) if they can't be used"""
    path = os.path.join(out, "load_pcs.txt")
    try:
        with open(path) as f:
            lines = [line.split() for line in f if line.strip()]
    except (OSError, UnicodeDecodeError) as e:
        return None, "cannot read %s: %s" % (path, e)
    producers, consumers = [], []
    for fields in lines:
        if len(fields) != 2 or fields[0] not in ("producer", "consumer"):
            return None, "unexpected line in %s: %s" % (path, fields)
        try:
            pc = int(fields[1], 16)
        except ValueError:
            return None, "bad address in %s: %s" % (path, fields)
        (producers if fields[0] == "producer" else consumers).append(pc)
    n = LOADS_PER_SLOT[workload]
    if len(producers) != 1 or len(consumers) != n - 1:
        return (
            None,
            "%s has %d producer(s) and %d consumer(s), expected 1 and %d"
            % (
                path,
                len(producers),
                len(consumers),
                n - 1,
            ),
        )
    if len(set(producers + consumers)) != n:
        return None, "%s has repeated addresses" % path
    return (producers[0], consumers), "ok"


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


def phase_checks(
    out,
    stats,
    b,
    c,
    cl,
    matched,
    accepted,
    validated,
    acc_line,
    val_line,
    redirects,
    pred_outcome,
    cons_outcome,
):
    """Expectations from the phase-change program ldld.c (-DLVP_PHASE): the
    producer's pointer changes at the start of every block of 64 slots, so a
    confident prediction made from the previous block is wrong there, and a
    speculative load that used it must be undone.  Events are matched by
    the instruction's thread and execSeqNum, never only by counts."""
    section(
        "Expectations from the phase-change program ldld.c "
        "(recovery; not universal invariants)"
    )

    for n, s in (("B", b), ("C", c)):
        check(
            s["incorrect"] > 0,
            "%s: incorrect predictions (%d)" % (n, s["incorrect"]),
        )
    check(
        c["specLoadsCommitted"] > 0,
        "C: speculative loads committed (%d)" % c["specLoadsCommitted"],
    )
    check(
        c["mispredictSquashes"] > 0,
        "C: a used wrong prediction triggered a recovery redirect (%d)"
        % c["mispredictSquashes"],
    )
    check(
        0 < c["mispredictSquashes"] <= c["incorrect"],
        "C: 0 < recovery redirects (%d) <= incorrect predictions (%d)"
        % (c["mispredictSquashes"], c["incorrect"]),
    )
    check(
        c["specLoadsSquashed"] >= c["mispredictSquashes"],
        "C: at least one speculative load squashed for each redirect "
        "(squashed %d, redirects %d)"
        % (c["specLoadsSquashed"], c["mispredictSquashes"]),
    )
    check(
        c["specLoadsFaulted"] == 0 and c["specInFlight"] == 0,
        "C: no speculative load faulted or in flight at the end "
        "(faulted %d, in flight %d)"
        % (c["specLoadsFaulted"], c["specInFlight"]),
    )

    # The log agrees with the statistics of the run that wrote it
    check(
        len(redirects) == cl["mispredictSquashes"],
        "CL: one redirect line per recovery redirect (%d of %d)"
        % (len(redirects), cl["mispredictSquashes"]),
    )
    outcomes = [o for _, o, _ in cons_outcome.values()]
    for o, stat in (
        ("committed", "specLoadsCommitted"),
        ("squashed", "specLoadsSquashed"),
        ("faulted", "specLoadsFaulted"),
    ):
        check(
            outcomes.count(o) == cl[stat],
            "CL: speculative load outcomes logged as %s match %s (%d of %d)"
            % (o, stat, outcomes.count(o), cl[stat]),
        )
    check(
        sorted(k for k in cons_outcome) == sorted(a[1] for a in accepted),
        "CL: every accepted speculative load has exactly one outcome",
    )
    for o in ("correct", "incorrect"):
        vals = [v for v in pred_outcome.values() if v == o]
        check(
            len(vals) == cl[o],
            "CL: prediction outcomes logged as %s match %s (%d of %d)"
            % (o, o, len(vals), cl[o]),
        )

    # Every redirect belongs to a producer that committed with an incorrect
    # prediction that a speculative load had used, and that load was
    # accepted before the producer was validated
    consumers_of = {}
    for _, cons, prod in accepted:
        consumers_of.setdefault(prod, []).append(cons)

    bad = []
    for prod, line in redirects.items():
        why = []
        if pred_outcome.get(prod) != "incorrect":
            why.append("producer outcome %s" % pred_outcome.get(prod))
        if prod not in val_line or not val_line[prod] < line:
            why.append("redirect not after the validation")
        users = consumers_of.get(prod, [])
        if not users:
            why.append("no speculative load used the prediction")
        for cons in users:
            if not acc_line[cons] < val_line.get(prod, -1):
                why.append("%s accepted after validation" % (cons,))
            outcome = cons_outcome.get(cons)
            if outcome is None or outcome[1] != "squashed":
                why.append(
                    "consumer %s not squashed (%s)"
                    % (cons, outcome and outcome[1])
                )
            elif not outcome[0] > line:
                why.append(
                    "consumer %s squashed before the redirect" % (cons,)
                )
        if why:
            bad.append((prod, why))
    check(
        not bad,
        "CL: each redirect follows the producer's incorrect validation, "
        "after its consumer was accepted, and the consumer was squashed "
        "after it (%d of %d fail)" % (len(bad), len(redirects)),
    )
    for prod, why in bad[:5]:
        report("producer %s: %s" % (prod, "; ".join(why)))

    # And the other way: a wrong prediction that was used is always
    # recovered from, and nothing that used a wrong prediction committed
    unrecovered = [
        prod
        for prod, users in consumers_of.items()
        if pred_outcome.get(prod) == "incorrect" and prod not in redirects
    ]
    check(
        not unrecovered,
        "CL: every used incorrect prediction raised a redirect (%d did not)"
        % len(unrecovered),
    )
    wrong_commits = [
        cons
        for cons, (_, outcome, prod) in cons_outcome.items()
        if outcome == "committed" and pred_outcome.get(prod) != "correct"
    ]
    check(
        not wrong_commits,
        "CL: every committed speculative load used a correct prediction "
        "(%d did not)" % len(wrong_commits),
    )
    # The program has no other source of squashes of these loads
    unexplained = [
        cons
        for cons, (_, outcome, prod) in cons_outcome.items()
        if outcome == "squashed" and prod not in redirects
    ]
    check(
        not unexplained,
        "CL: every squashed speculative load was squashed by its "
        "producer's redirect (%d were not)" % len(unexplained),
    )
    report(
        "recovery redirects: %d of %d block boundaries; speculative loads: "
        "%d committed, %d squashed"
        % (
            len(redirects),
            NUM_SLOTS // PHASE_BLOCK - 1,
            c["specLoadsCommitted"],
            c["specLoadsSquashed"],
        )
    )


def fanout_checks(
    producer_pc,
    consumer_pcs,
    c,
    accepted,
    acc_line,
    acc_pc,
    val_line,
    val_pc,
    redirects,
    cons_outcome,
):
    """Expectations from the fan-out program ldld.c (-DLVP_FANOUT): one
    producer load with two dependent loads at different PCs, both from the
    producer's register, so both can use one prediction.  The number of
    consumers that were accepted for each producer is not assumed: it is
    taken from the log, and the checks are made on what is there."""
    section("Expectations from the fan-out program ldld.c (two consumers)")

    consumers_of = collections.OrderedDict()
    for _, cons, prod in accepted:
        consumers_of.setdefault(prod, []).append(cons)

    check(
        all(pc in consumer_pcs for pc in acc_pc.values()),
        "CL: every accepted speculative load is at a consumer PC of the "
        "assembly check",
    )
    check(
        set(acc_pc.values()) == set(consumer_pcs),
        "CL: speculative loads were accepted at both consumer PCs "
        "(%s)" % ", ".join("0x%x" % pc for pc in sorted(set(acc_pc.values()))),
    )
    check(
        all(
            val_pc[prod] == producer_pc
            for prod in consumers_of
            if prod in val_pc
        ),
        "CL: every validated producer of a speculative load is at the "
        "producer PC 0x%x" % producer_pc,
    )
    repeated = [
        prod
        for prod, cons in consumers_of.items()
        if len({acc_pc[x] for x in cons}) != len(cons)
    ]
    check(
        not repeated,
        "CL: no producer has two accepted consumers at the same PC (%d do)"
        % len(repeated),
    )

    two = [prod for prod, cons in consumers_of.items() if len(cons) >= 2]
    check(
        len(two) >= 1,
        "CL: at least one producer has two accepted consumers (%d of %d "
        "producers with an accepted consumer)" % (len(two), len(consumers_of)),
    )
    late = [
        cons
        for prod, conss in consumers_of.items()
        if prod in val_line
        for cons in conss
        if not acc_line[cons] < val_line[prod]
    ]
    check(
        not late,
        "CL: every consumer, the second of a producer included, is "
        "accepted before its producer is validated (%d late)" % len(late),
    )

    # The recovery of a producer with two consumers
    two_redirected = [prod for prod in redirects if prod in two]
    check(
        len(two_redirected) >= 1,
        "CL: at least one recovery redirect accounts for two consumers "
        "(%d of %d redirects)" % (len(two_redirected), len(redirects)),
    )
    survivors = [
        cons
        for prod in two_redirected
        for cons in consumers_of[prod]
        if cons not in cons_outcome or cons_outcome[cons][1] != "squashed"
    ]
    check(
        not survivors,
        "CL: every consumer of a producer with two consumers that raised a "
        "redirect was squashed (%d were not)" % len(survivors),
    )

    # Squashed speculative loads are the consumers of redirected producers,
    # however many each redirect had
    of_redirected = sum(len(consumers_of.get(prod, [])) for prod in redirects)
    check(
        c["specLoadsSquashed"] == of_redirected,
        "C: squashed speculative loads are the consumers of the redirected "
        "producers (%d of %d)" % (c["specLoadsSquashed"], of_redirected),
    )
    check(
        c["specLoadsIssued"]
        == c["specLoadsCommitted"]
        + c["specLoadsSquashed"]
        + c["specLoadsFaulted"],
        "C: issued speculative loads (%d) = committed (%d) + squashed (%d) "
        "+ faulted (%d)"
        % (
            c["specLoadsIssued"],
            c["specLoadsCommitted"],
            c["specLoadsSquashed"],
            c["specLoadsFaulted"],
        ),
    )

    sizes = collections.Counter(len(cons) for cons in consumers_of.values())
    report(
        "producers by accepted consumers: "
        + ", ".join("%d consumer(s) x%d" % kv for kv in sorted(sizes.items()))
    )
    sizes = collections.Counter(
        len(consumers_of.get(prod, [])) for prod in redirects
    )
    report(
        "redirects by consumers accounted for: "
        + ", ".join("%d consumer(s) x%d" % kv for kv in sorted(sizes.items()))
    )


def main():
    if len(sys.argv) not in (2, 3) or (
        len(sys.argv) == 3 and sys.argv[2] not in EXPECTED_CHECKSUMS
    ):
        print(__doc__)
        return 2
    out = sys.argv[1]
    workload = sys.argv[2] if len(sys.argv) == 3 else "stable"
    expected_checksum = EXPECTED_CHECKSUMS[workload]
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

    pcs, why = read_load_pcs(out, workload)
    if pcs is None:
        check(False, "load PCs from the assembly check: " + why)
        print()
        print("the load PCs are unusable: nothing was compared")
        return 1
    producer_pc, consumer_pcs = pcs

    section("Architectural correctness")

    for n in names:
        check(
            checksum(out, n) == expected_checksum,
            "%s: checksum is %d" % (n, expected_checksum),
        )
    if workload == "fanout":
        lo_sum, hi_sum = fanout_sums()
        for n in names:
            value = checksum(out, n)
            check(
                value is not None
                and value & 0xFFFFFFFF == lo_sum
                and value >> 32 == hi_sum,
                "%s: v[3] contributions sum to %d and v[2] contributions "
                "to %d, independently" % (n, lo_sum, hi_sum),
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

    # CL is C with a debug trace, which changes nothing the simulation does
    check(
        lvp(stats["CL"]) == c,
        "C and CL: identical cpu.lvp.* statistics",
    )

    section("Lifecycle invariants (any program)")

    check(b["specLoadsIssued"] == 0, "B: no speculative loads")
    check(b["mispredictSquashes"] == 0, "B: no recovery redirects")

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
        check(
            s["mispredictSquashes"] <= s["incorrect"],
            "%s: recovery redirects (%d) <= incorrect predictions (%d)"
            % (n, s["mispredictSquashes"], s["incorrect"]),
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
    redir_re = re.compile(
        r"^\s*(\d+):.*Recovery redirect inst: "
        + inst_id
        + r" .*exec: (\d+) next"
    )
    pout_re = re.compile(
        r"^\s*(\d+):.*Prediction outcome inst: "
        + inst_id
        + r" .*exec: (\d+) \((\w+)\)"
    )
    cout_re = re.compile(
        r"^\s*(\d+):.*Speculative load outcome inst: "
        + inst_id
        + r" .*exec: (\d+) producer: (\d+) \((\w+)\)"
    )
    accepted = []  # (tick, (thread, exec), (thread, producer exec))
    validated = {}  # (thread, exec) -> tick
    acc_pc = {}  # consumer (thread, exec) -> its PC
    val_pc = {}  # validated producer (thread, exec) -> its PC
    # Where each event is in the log, which orders events with the same tick
    acc_line = {}  # consumer (thread, exec) -> line number
    val_line = {}  # producer -> line number
    redirects = {}  # producer -> line number
    pred_outcome = {}  # producer -> "correct", "incorrect", ...
    cons_outcome = {}  # consumer -> (line number, outcome, producer)
    duplicate_events = []
    duplicate_validations = []
    id_mismatches = []
    with open(os.path.join(out, "CL", "trace.txt")) as f:
        for lineno, line in enumerate(f):
            m = acc_re.match(line)
            if m:
                g = m.groups()
                accepted.append(
                    (int(g[0]), (int(g[1]), int(g[6])), (int(g[1]), int(g[8])))
                )
                acc_line[(int(g[1]), int(g[6]))] = lineno
                acc_pc[(int(g[1]), int(g[6]))] = int(g[7], 16)
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
                val_line[key] = lineno
                val_pc[key] = int(g[7], 16)
                continue
            m = redir_re.match(line)
            if m:
                g = m.groups()
                key = (int(g[1]), int(g[6]))
                if int(g[6]) != int(g[8]):
                    id_mismatches.append(line.strip())
                if key in redirects:
                    duplicate_events.append(("redirect", key))
                redirects[key] = lineno
                continue
            m = pout_re.match(line)
            if m:
                g = m.groups()
                key = (int(g[1]), int(g[6]))
                if int(g[6]) != int(g[8]):
                    id_mismatches.append(line.strip())
                if key in pred_outcome:
                    duplicate_events.append(("prediction outcome", key))
                pred_outcome[key] = g[9]
                continue
            m = cout_re.match(line)
            if m:
                g = m.groups()
                key = (int(g[1]), int(g[6]))
                if int(g[6]) != int(g[8]):
                    id_mismatches.append(line.strip())
                if key in cons_outcome:
                    duplicate_events.append(("load outcome", key))
                cons_outcome[key] = (lineno, g[10], (int(g[1]), int(g[9])))

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
        not duplicate_events,
        "CL: no redirect or outcome logged twice (%d)" % len(duplicate_events),
    )
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

    if workload == "stable":
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
            % (
                c["specLoadsSquashed"],
                c["specLoadsFaulted"],
                c["specInFlight"],
            ),
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

    else:
        phase_checks(
            out,
            stats,
            b,
            c,
            lvp(stats["CL"]),
            matched,
            accepted,
            validated,
            acc_line,
            val_line,
            redirects,
            pred_outcome,
            cons_outcome,
        )
        if workload == "fanout":
            fanout_checks(
                producer_pc,
                consumer_pcs,
                c,
                accepted,
                acc_line,
                acc_pc,
                val_line,
                val_pc,
                redirects,
                cons_outcome,
            )

    section("Program structure (all workloads)")

    # The loads that commit, from A's commit-only execution trace.  The
    # assembly check (run_lvp_stageb.sh) has shown that sum_slots() holds
    # every load in the program, and its PCs are in load_pcs.txt.
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
        n_loads = LOADS_PER_SLOT[workload]
        total = sum(inv["loads"].values())
        check(
            total == n_loads * NUM_SLOTS,
            "A trace: exactly %d committed loads (found %d)"
            % (n_loads * NUM_SLOTS, total),
        )
        check(
            len(inv["loads"]) == n_loads,
            "A trace: exactly %d distinct load PCs (found %d)"
            % (n_loads, len(inv["loads"])),
        )
        check(
            inv["loads"]
            == {pc: NUM_SLOTS for pc in [producer_pc] + consumer_pcs},
            "A trace: the committed loads are exactly the producer 0x%x "
            "and the consumer(s) %s of the assembly check, %d times each"
            % (
                producer_pc,
                ", ".join("0x%x" % pc for pc in consumer_pcs),
                NUM_SLOTS,
            ),
        )
        by_pc = sorted(inv["loads"].items())[:8]
        report(
            "committed loads by PC: "
            + ", ".join("0x%x x%d" % (pc, n) for pc, n in by_pc)
        )

    # Branch predictor statistics: reported, never compared.  Recovery
    # refetches instructions, so the lookups and squashes may differ.
    section("Branch predictor statistics (reported only)")
    bp = sorted({k for n in names for k in stats[n] if "branchPred." in k})
    for k in bp:
        report(
            k
            + ": "
            + " ".join("%s %s" % (n, stats[n].get(k, "-")) for n in names)
        )

    if c["mispredictSquashes"]:
        try:
            extra = int(
                float(stats["C"]["system.cpu.branchPred.lookups"])
            ) - int(float(stats["A"]["system.cpu.branchPred.lookups"]))
            report(
                "extra branch predictor lookups in C over A: %d (%.2f per "
                "recovery redirect)" % (extra, extra / c["mispredictSquashes"])
            )
        except (KeyError, ValueError):
            report("branch predictor lookups are not available")

    # Performance is reported but never part of the result above
    section("Performance (reported only, not part of the result)")

    cycles = {n: int(stats[n]["system.cpu.numCycles"]) for n in names}
    insts = int(stats["C"]["simInsts"])
    report("cycles: A %d B %d C %d" % (cycles["A"], cycles["B"], cycles["C"]))
    report(
        "IPC: A %.4f B %.4f C %.4f"
        % tuple(insts / cycles[n] for n in ("A", "B", "C"))
    )
    n_consumers = LOADS_PER_SLOT[workload] - 1
    report(
        "C coverage: %d of %d consumer loads committed having been "
        "speculative (%.1f%%)"
        % (
            c["specLoadsCommitted"],
            n_consumers * NUM_SLOTS,
            100.0 * c["specLoadsCommitted"] / (n_consumers * NUM_SLOTS),
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
