#!/usr/bin/env python3
"""Checks the outputs of run_lvp_stageb.sh.
Usage: check_lvp_stageb.py OUT [stable|phase|fanout|fault|chain|threshold|confidence|alias|
                               pcwindow|invalid-config]

The workload is stable (every slot holds the same pointer, the default) or
phase (the pointer alternates between two nodes in blocks of 64 slots, so
confident predictions are wrong at the start of each block and recovery is
exercised) or fanout (phase, with two dependent loads from each pointer, so
that one wrong prediction can be used by two speculative loads) or fault (a
wrong prediction that points into a page that has been unmapped, so the
speculative load that uses it takes a translation fault that must not be
taken) or chain (three dependent loads, L1, L2 and L3, where L2 is both a
consumer of L1 and a predicted producer for L3); see ldld.c.
The phase and fanout runs also check the Fetch2 recovery hook, from the
Branch debug flag that the runner enables for their CL run.

The threshold, pcwindow and invalid-config modes are separate configuration
tests (see threshold_main, pcwindow_main and invalid_config_main).

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


# fault: FAULT_CYCLES cycles of: map the page at FAULT_ADDR, call sum_slots()
# over FAULT_TRAIN slots that point to the node in it (v[3] = 7), unmap the
# page, call sum_slots() over FAULT_USE slots that point to node B (v[3] =
# 11).  The values below are those of ldld.c.
FAULT_ADDR = 0x20000000
FAULT_PAGE = 4096
FAULT_CYCLES = 32
FAULT_TRAIN = 8
FAULT_USE = 3
FAULT_VALUES = (7, 11)  # v[3] of the node in the page, and of node B
FAULT_OFFSET = 24  # of v[3] in a node


def fault_checksum():
    """The program's sum, from the structure of its cycles"""
    total = 0
    for _ in range(FAULT_CYCLES):
        total += FAULT_TRAIN * FAULT_VALUES[0]
        total += FAULT_USE * FAULT_VALUES[1]
    return total


# chain: CHAIN_CYCLES cycles of: root->next is node A (v[3] = 7) for
# CHAIN_TRAIN iterations, then node B (v[3] = 11) for CHAIN_USE iterations.
# The slots, the root and the nodes are CHAIN_STRIDE apart (ldld.c).
CHAIN_CYCLES = 32
CHAIN_TRAIN = 8
CHAIN_USE = 3
CHAIN_VALUES = (7, 11)  # v[3] of node A and of node B
CHAIN_STRIDE = 32768
CHAIN_SLOT_COUNT = CHAIN_TRAIN + CHAIN_USE


def chain_checksum():
    """The program's sum, from the structure of its cycles"""
    total = 0
    for _ in range(CHAIN_CYCLES):
        total += CHAIN_TRAIN * CHAIN_VALUES[0]
        total += CHAIN_USE * CHAIN_VALUES[1]
    return total


EXPECTED_CHECKSUMS = {
    "stable": 7 * NUM_SLOTS,
    "phase": (7 + 11) * (NUM_SLOTS // 2),
    "fanout": fanout_sums()[0] + (fanout_sums()[1] << 32),
    "fault": fault_checksum(),
    "chain": chain_checksum(),
}
# The loads in sum_slots() for each slot: the producer and its consumers
LOADS_PER_SLOT = {
    "stable": 2,
    "phase": 2,
    "fanout": 3,
    "fault": 2,
    "chain": 3,
}
# How many times sum_slots() runs its loop in the whole program
ITERATIONS = {
    "stable": NUM_SLOTS,
    "phase": NUM_SLOTS,
    "fanout": NUM_SLOTS,
    "fault": FAULT_CYCLES * (FAULT_TRAIN + FAULT_USE),
    "chain": CHAIN_CYCLES * (CHAIN_TRAIN + CHAIN_USE),
}

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
    producers, consumers, chain = [], [], []
    for fields in lines:
        if len(fields) != 2 or fields[0] not in (
            "producer",
            "consumer",
            "chain",
        ):
            return None, "unexpected line in %s: %s" % (path, fields)
        try:
            pc = int(fields[1], 16)
        except ValueError:
            return None, "bad address in %s: %s" % (path, fields)
        {"producer": producers, "consumer": consumers, "chain": chain}[
            fields[0]
        ].append(pc)
    n = LOADS_PER_SLOT[workload]
    if workload == "chain":
        # L1, L2 and L3 in program order
        if len(chain) != 3 or producers or consumers:
            return None, (
                "%s has %d chain line(s), %d other, expected 3 and 0"
                % (path, len(chain), len(producers) + len(consumers))
            )
        if len(set(chain)) != 3 or chain != sorted(chain):
            return None, "%s: the loads are not 3 distinct, ordered PCs" % path
        return (chain[0], chain[1:]), "ok"
    if chain:
        return None, "%s has chain lines, but %s is not a chain" % (
            path,
            workload,
        )
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
    inv = {"instructions": 0, "loads": {}, "load_addrs": {}, "unparsed": []}
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
                    ea = re.search(r"\bA=0x([0-9a-f]+)", line)
                    if ea:
                        addrs = inv["load_addrs"].setdefault(pc, {})
                        a = int(ea.group(1), 16)
                        addrs[a] = addrs.get(a, 0) + 1
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
    boundaries,
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
        "recovery redirects: %d of %d changes of the pointer; speculative "
        "loads: %d committed, %d squashed"
        % (
            len(redirects),
            boundaries,
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


# Messages of the Fetch2 recovery hook and of the branch predictor calls it
# makes, as the Branch debug flag prints them (src/cpu/minor/fetch2.cc,
# src/cpu/pred/bpred_unit.cc).  Each is preceded by "<tick>: <object>: ".
INST_ID = r"(\d+)/(\d+)\.(\d+)/(\d+)/(\d+)\.(\d+) pc: 0x([0-9a-f]+)"
HOOK_RE = re.compile(r"^\s*(\d+):.*LVP recovery redirect inst: " + INST_ID)
# BPredUnit::squash(sn, tid) prints, for each history entry it removes, an
# optional return address stack line, then the entry and the size left:
#   [tid:T] [squash sn:N] Removing history for [sn:M] PC ...
#   [tid:T] [squash sn:N] predHist.size(): S
SQUASH_RE = re.compile(r"\[tid:(\d+)\] \[squash sn:(\d+)\] (.*)$")
REMOVING_RE = re.compile(r"Removing history for \[sn:(\d+)\] PC ")
SIZE_RE = re.compile(r"predHist\.size\(\): (\d+)\s*$")
RAS_RE = re.compile(r"(Squashing\s+Call \[sn:|Restoring top of RAS)")
# BPredUnit::update(sn, tid) always prints, then retires histories silently
COMMIT_RE = re.compile(r"\[tid:(\d+)\] Committing branches until sn:(\d+)\]")
# BPredUnit::predict prints when it has added a history entry
PUSH_RE = re.compile(
    r"\[tid:(\d+)\] \[sn:(\d+)\] History entry added\. "
    r"predHist\.size\(\): (\d+)\s*$"
)


def parse_hook_log(path):
    """The Fetch2 recovery hooks in a Branch debug log, and what each did.

    The hook (Fetch2::updateBranchPrediction) prints one line and then makes
    two calls with nothing in between: BPredUnit::squash(N, tid), which
    prints for each entry it removes and nothing if it removes none, and
    BPredUnit::update(N, tid), which always prints.  So between the hook's
    line and the update's line there may be only the squash's lines, and
    anything else is reported as a problem.  After the update the history of
    the thread is empty (the squash leaves only entries up to N, and the
    update retires every entry up to N), so the next entry that predict adds
    for the thread must find the history empty.  That entry is evidence for
    one hook only: if another hook of the thread comes before it, nothing
    shows what the earlier hook left, because the later one empties the
    history whatever it was.  The earlier hook is then marked ambiguous and
    its history is not checked, and the checker fails.

    Returns (hooks, problems).  hooks maps the hook's (thread, exec) to a
    dict, and problems lists everything that was not as expected.
    """
    hooks = {}
    problems = []
    block = None  # the hook whose calls are being read
    awaiting = {}  # thread -> its last hook, still to see a history entry

    with open(path) as f:
        for lineno, line in enumerate(f):
            if block is not None:
                # Only the calls of the hook can print here
                m = SQUASH_RE.search(line)
                if m:
                    tid, sn, text = (
                        int(m.group(1)),
                        int(m.group(2)),
                        m.group(3),
                    )
                    if (tid, sn) != (block["tid"], block["fetch"]):
                        problems.append(
                            "line %d: squash message for tid %d sn %d inside "
                            "the hook of tid %d sn %d"
                            % (lineno, tid, sn, block["tid"], block["fetch"])
                        )
                    r = REMOVING_RE.search(text)
                    s = SIZE_RE.search(text)
                    if block["expect_size"] and not s:
                        problems.append(
                            "line %d: a removal was not followed by its size"
                            % lineno
                        )
                        block["expect_size"] = False
                    if r:
                        block["removed"].append(int(r.group(1)))
                        block["expect_size"] = True
                    elif s:
                        if not block["expect_size"]:
                            problems.append(
                                "line %d: a size without a removal" % lineno
                            )
                        block["expect_size"] = False
                        block["sizes"].append(int(s.group(1)))
                    elif RAS_RE.search(text):
                        block["ras"] += 1
                    else:
                        problems.append(
                            "line %d: unexpected squash message: %s"
                            % (lineno, line.strip()[:120])
                        )
                    continue
                m = COMMIT_RE.search(line)
                if m:
                    if block["expect_size"]:
                        problems.append(
                            "line %d: the update came before the size of "
                            "the last removal" % lineno
                        )
                    block["commit"] = (int(m.group(1)), int(m.group(2)))
                    block["commit_line"] = lineno
                    awaiting[block["tid"]] = block
                    block = None
                    continue
                problems.append(
                    "line %d: unexpected message between the hook and its "
                    "update: %s" % (lineno, line.strip()[:120])
                )
                continue

            if "LVP recovery redirect" in line:
                m = HOOK_RE.match(line)
                if not m:
                    problems.append(
                        "line %d: unparsable hook line: %s"
                        % (lineno, line.strip()[:120])
                    )
                    continue
                g = m.groups()
                key = (int(g[1]), int(g[6]))
                if key in hooks:
                    problems.append("hook for %s seen twice" % (key,))
                # An earlier hook of this thread that has not seen a history
                # entry yet can no longer be told apart from this one
                earlier = awaiting.pop(int(g[1]), None)
                if earlier is not None:
                    earlier["ambiguous"] = key
                block = {
                    "key": key,
                    "line": lineno,
                    "tick": int(g[0]),
                    "tid": int(g[1]),
                    "fetch": int(g[5]),
                    "pc": int(g[7], 16),
                    "removed": [],
                    "sizes": [],
                    "ras": 0,
                    "expect_size": False,
                    "commit": None,
                    "commit_line": None,
                    "next_push": None,
                    "ambiguous": None,
                }
                hooks[key] = block
            elif "History entry added" in line:
                m = PUSH_RE.search(line)
                if m:
                    tid, sn, size = (int(x) for x in m.groups())
                    hook = awaiting.pop(tid, None)
                    if hook is not None:
                        hook["next_push"] = (sn, size)
                else:
                    # The entry that this line stands for can't be read, so
                    # no waiting hook may take a later entry in its place:
                    # they are dropped and are reported as having none
                    problems.append(
                        "line %d: malformed history entry line: %s"
                        % (lineno, line.strip()[:120])
                    )
                    awaiting.clear()

    if block is not None:
        problems.append(
            "the log ends inside the hook of %s (no update)" % (block["key"],)
        )
    return hooks, problems


def hook_checks(path, redirects, val_pc, cl, workload):
    """The Fetch2 recovery hook, from the Branch debug log of the CL run: for
    every LVP recovery redirect the hook ran, called squash and update with
    the producer's fetch sequence number, and left the thread's branch
    history empty.  Only valid when the CL run logged the Branch flag.

    What the log can and cannot show: update() always prints, but squash()
    prints nothing when there is no younger history to remove, so a squash
    that removed nothing is not seen.  The emptiness of the history after
    the hook is observed through the size that predict reports when it adds
    the next entry: it is 1 only if no entry was left by either call, so a
    squash that left a younger entry, or an update that left an older one,
    would show as a larger size.  That is the combined effect of the two
    calls, not a separate proof of what each does.

    Coverage differs between the workloads.  In phase and fanout the squash
    of each recovery removes every younger history entry and leaves the
    history empty (the size after the last removal is 0) before the update
    runs, so those workloads do not exercise the update retiring an older,
    uncommitted history entry.  In fault the squash leaves exactly one older
    entry (the size after the last removal is 1), which the update retires,
    and the next entry that predict adds finds the history empty and a size
    of 1: that is asserted for the fault workload, for every hook.  Even so,
    it is the combined effect of the squash and the update that is seen, and
    only for one older entry; what the update does with several, or with
    the predictor tables, is supported by reading BPredUnit::update(), not
    demonstrated here."""
    section("Fetch2 recovery hook (Branch debug log of CL)")

    hooks, problems = parse_hook_log(path)

    # 1. One hook for each recovery redirect of the Execute stage
    check(
        len(hooks) == len(redirects) == cl["mispredictSquashes"],
        "CL: hooks (%d) = recovery redirects logged (%d) = "
        "mispredictSquashes (%d)"
        % (len(hooks), len(redirects), cl["mispredictSquashes"]),
    )
    missing = sorted(set(redirects) - set(hooks))
    extra = sorted(set(hooks) - set(redirects))
    check(
        not missing and not extra,
        "CL: the hooks are exactly the recovery redirects, by (thread, exec) "
        "(%d redirects without a hook, %d hooks without a redirect)"
        % (len(missing), len(extra)),
    )
    for key in (missing + extra)[:5]:
        report("unmatched: %s" % (key,))
    both = [k for k in hooks if k in redirects]
    late = [k for k in both if not redirects[k] < hooks[k]["line"]]
    check(
        not late,
        "CL: every hook is logged after its redirect (%d are not)" % len(late),
    )
    wrong_pc = [k for k in both if val_pc.get(k) != hooks[k]["pc"]]
    check(
        not wrong_pc,
        "CL: every hook is for the instruction that was validated (%d are "
        "not)" % len(wrong_pc),
    )

    # 2. The synchronous block of each hook
    check(
        not problems,
        "CL: the hook log is as expected: only squash lines between a hook "
        "and its update, and well formed history entry lines (%d problems)"
        % len(problems),
    )
    for p in problems[:5]:
        report("hook log: " + p)
    unclosed = [k for k, h in hooks.items() if h["commit"] is None]
    wrong_update = [
        k
        for k, h in hooks.items()
        if h["commit"] is not None and h["commit"] != (h["tid"], h["fetch"])
    ]
    check(
        not unclosed and not wrong_update,
        "CL: every hook is followed by an update of the same thread and "
        "sequence number (%d without an update, %d with another)"
        % (len(unclosed), len(wrong_update)),
    )

    # 3. What the squash removed: only entries younger than the recovery,
    # youngest first, and each removal leaves one entry fewer
    not_younger = [
        k
        for k, h in hooks.items()
        if any(m <= h["fetch"] for m in h["removed"])
    ]
    check(
        not not_younger,
        "CL: every history entry removed is younger than the recovery "
        "(%d hooks removed an older one)" % len(not_younger),
    )
    not_descending = [
        k
        for k, h in hooks.items()
        if any(a <= b for a, b in zip(h["removed"], h["removed"][1:]))
    ]
    check(
        not not_descending,
        "CL: entries are removed youngest first (%d hooks are not)"
        % len(not_descending),
    )
    bad_sizes = [
        k
        for k, h in hooks.items()
        if len(h["sizes"]) != len(h["removed"])
        or any(b != a - 1 for a, b in zip(h["sizes"], h["sizes"][1:]))
        or (h["sizes"] and h["sizes"][-1] < 0)
    ]
    check(
        not bad_sizes,
        "CL: the history size falls by one with each removal (%d hooks do "
        "not)" % len(bad_sizes),
    )

    # 4. The history is empty afterwards: the next entry added is the only
    # one, and it is for a younger instruction.  Each history entry is
    # evidence for the last hook before it only; a hook that another hook of
    # its thread followed before any entry cannot be checked
    ambiguous = [k for k, h in hooks.items() if h["ambiguous"] is not None]
    check(
        not ambiguous,
        "CL: no hook is followed by another hook of its thread before a "
        "history entry, which would leave its own history unchecked "
        "(%d are)" % len(ambiguous),
    )
    for key in ambiguous[:5]:
        report(
            "ambiguous: the hook of %s was followed by the hook of %s "
            "before any history entry" % (key, hooks[key]["ambiguous"])
        )
    no_push = [
        k
        for k, h in hooks.items()
        if h["next_push"] is None and h["ambiguous"] is None
    ]
    check(
        not no_push,
        "CL: a history entry is added after every hook (%d hooks have none)"
        % len(no_push),
    )
    for key in no_push[:5]:
        report("no history entry after the hook of %s" % (key,))
    pushed = [(k, h) for k, h in hooks.items() if h["next_push"] is not None]
    not_empty = [k for k, h in pushed if h["next_push"][1] != 1]
    check(
        not not_empty,
        "CL: the next history entry after every hook finds the history "
        "empty, size 1 (%d hooks left it non-empty)" % len(not_empty),
    )
    for key in not_empty[:5]:
        report(
            "hook %s: next entry sn %d size %d"
            % ((key,) + hooks[key]["next_push"])
        )
    not_new = [k for k, h in pushed if not h["next_push"][0] > h["fetch"]]
    check(
        not not_new,
        "CL: the next history entry after every hook is for a younger "
        "instruction (%d are not)" % len(not_new),
    )

    # 4b. The fault workload's recoveries leave an older entry for the
    # update to retire: the size after the squash's last removal is exactly
    # 1, and the next entry is for a younger instruction and finds the
    # history empty.  A hook that logged no removal has no size to show.
    if workload == "fault":
        not_retired = [
            k
            for k, h in hooks.items()
            if not h["sizes"]
            or h["sizes"][-1] != 1
            or h["next_push"] is None
            or h["next_push"][0] <= h["fetch"]
            or h["next_push"][1] != 1
        ]
        check(
            not not_retired,
            "CL (fault): every hook's squash left exactly one older history "
            "entry before the update (size 1 after the last removal), and "
            "the next entry, for a younger instruction, found the history "
            "empty (size 1) (%d hooks do not)" % len(not_retired),
        )
        for key in not_retired[:5]:
            h = hooks[key]
            report(
                "hook %s: sizes %s, next entry %s"
                % (key, h["sizes"], h["next_push"])
            )

    # 5. The squash removed something at least once (a removal is the only
    # sign that squash ran, so a hook with none proves nothing either way)
    counts = collections.Counter(len(h["removed"]) for h in hooks.values())
    check(
        any(n > 0 for n in counts),
        "CL: at least one hook removed history entries (%d of %d did)"
        % (len(hooks) - counts.get(0, 0), len(hooks)),
    )
    report(
        "hooks by history entries removed: "
        + ", ".join("%d x%d" % kv for kv in sorted(counts.items()))
    )
    report(
        "%d hooks logged no removal: squash either had nothing to remove or "
        "its output is not visible, which the log cannot tell apart"
        % counts.get(0, 0)
    )


def fault_checks(
    c,
    accepted,
    acc_line,
    acc_fault,
    acc_src,
    val_line,
    redirects,
    pred_outcome,
    cons_outcome,
):
    """Expectations from the fault program ldld.c (-DLVP_FAULT): in each of
    its cycles the producer's last prediction points into a page that has
    been unmapped, so a speculative consumer that used it has taken a
    translation fault, which is recorded in the instruction and is taken
    only if the instruction reaches its response.  It must not: the
    producer's prediction is found out, the redirect discards the consumer,
    and its response is dropped.  The consumers that have faulted are found
    from the acceptance lines of the CL log, which print
    "(translation fault: ...)" for them.

    The number of cycles in which a consumer is accepted with a fault is not
    assumed: it is reported against the number of cycles."""
    section("Expectations from the fault program ldld.c (unmapped page)")

    faulting = [cons for cons, faulted in acc_fault.items() if faulted]
    check(
        len(faulting) >= 1,
        "CL: at least one speculative consumer was accepted with a "
        "translation fault (%d)" % len(faulting),
    )
    report(
        "speculative consumers accepted with a translation fault: %d in %d "
        "cycles" % (len(faulting), FAULT_CYCLES)
    )

    outside = [
        cons
        for cons in faulting
        if acc_src[cons] is None
        or not FAULT_ADDR <= acc_src[cons] < FAULT_ADDR + FAULT_PAGE
    ]
    check(
        not outside,
        "CL: the source of every faulting consumer is in the unmapped page "
        "[0x%x, 0x%x) (%d are not)"
        % (FAULT_ADDR, FAULT_ADDR + FAULT_PAGE, len(outside)),
    )

    producer_of = {cons: prod for _, cons, prod in accepted}
    bad = []
    for cons in faulting:
        prod = producer_of[cons]
        why = []
        outcome = cons_outcome.get(cons)
        if outcome is None:
            why.append("no outcome")
        elif outcome[1] != "squashed":
            why.append("outcome %s" % outcome[1])
        if pred_outcome.get(prod) != "incorrect":
            why.append("producer outcome %s" % pred_outcome.get(prod))
        if prod not in redirects:
            why.append("no recovery redirect for the producer")
        else:
            if prod not in val_line or not (
                acc_line[cons] < val_line[prod] < redirects[prod]
            ):
                why.append("acceptance, validation, redirect out of order")
            if outcome is not None and not outcome[0] > redirects[prod]:
                why.append("squashed before the redirect")
        if why:
            bad.append((cons, why))
    check(
        not bad,
        "CL: every faulting consumer was accepted before its producer was "
        "validated as incorrect, and was squashed after the recovery "
        "redirect, never committed (%d are not)" % len(bad),
    )
    for cons, why in bad[:5]:
        report("faulting consumer %s: %s" % (cons, "; ".join(why)))

    check(
        c["specLoadsFaulted"] == 0,
        "C: no speculative load committed with a fault: the translation "
        "fault never reached the architectural fault path (%d did)"
        % c["specLoadsFaulted"],
    )
    producers = {producer_of[cons] for cons in faulting}
    check(
        c["mispredictSquashes"] >= len(producers),
        "C: at least one recovery redirect for each producer of a faulting "
        "consumer (%d redirects, %d producers)"
        % (c["mispredictSquashes"], len(producers)),
    )
    report(
        "recovery redirects: %d, faulting consumers: %d, cycles: %d"
        % (c["mispredictSquashes"], len(faulting), FAULT_CYCLES)
    )


def fault_trace_checks(inv, consumer_pcs):
    """What A's committed trace says about the consumer of the fault
    program: it loaded from the node in the page in the training calls and
    from node B in the last call of each cycle, so the consumers that were
    fetched again after a recovery loaded from B.  The program's own
    structure gives the numbers, and the trace is the same in all of A, B
    and C."""
    (consumer_pc,) = consumer_pcs
    addrs = inv["load_addrs"].get(consumer_pc, {})
    in_page = FAULT_ADDR + FAULT_OFFSET
    check(
        len(addrs) == 2,
        "A trace: the consumer committed loads from exactly two addresses "
        "(%s)" % ", ".join("0x%x" % a for a in sorted(addrs)),
    )
    check(
        addrs.get(in_page) == FAULT_CYCLES * FAULT_TRAIN,
        "A trace: %d consumer loads from the node in the page, 0x%x (found "
        "%s)" % (FAULT_CYCLES * FAULT_TRAIN, in_page, addrs.get(in_page)),
    )
    others = {a: n for a, n in addrs.items() if a != in_page}
    check(
        len(others) == 1
        and all(not FAULT_ADDR <= a < FAULT_ADDR + FAULT_PAGE for a in others)
        and sum(others.values()) == FAULT_CYCLES * FAULT_USE,
        "A trace: %d consumer loads, every one from the same address "
        "outside the unmapped page, that of node B (found %s)"
        % (
            FAULT_CYCLES * FAULT_USE,
            ", ".join("0x%x x%d" % kv for kv in sorted(others.items())),
        ),
    )


def chain_checks(
    l1,
    l2,
    l3,
    c,
    accepted,
    acc_line,
    acc_pc,
    val_line,
    val_pc,
    redirects,
    pred_outcome,
    cons_outcome,
):
    """Expectations from the chain program ldld.c (-DLVP_CHAIN): L1, L2 and
    L3 are three dependent loads at the PCs l1, l2 and l3, and L2 is both a
    consumer of L1 and a producer for L3.  A speculative load can use a
    prediction only while its producer is the oldest instruction, so L2 can
    use L1's prediction before L1 is validated, and L3 can use L2's
    prediction before L2 is, but L3 never uses L1's: the chain is
    speculative one pair at a time.  Every relationship is taken from the
    log: an accepted load names its producer by its execSeqNum, and a
    producer's PC is the PC of its validation (or, for L2, of its own
    acceptance); a PC that can't be found is not guessed."""
    section("Expectations from the chain program ldld.c (three loads)")

    producer_of = {cons: prod for _, cons, prod in accepted}

    def pc_of(inst):
        # a producer's PC: from its validation, else from its acceptance
        return val_pc.get(inst, acc_pc.get(inst))

    l2s = [k for k, pc in acc_pc.items() if pc == l2]
    l3s = [k for k, pc in acc_pc.items() if pc == l3]
    check(
        all(pc in (l2, l3) for pc in acc_pc.values()),
        "CL: every accepted speculative load is at L2 (0x%x) or L3 (0x%x)"
        % (l2, l3),
    )
    check(
        len(l2s) >= 1 and len(l3s) >= 1,
        "CL: speculative loads were accepted at both L2 (%d) and L3 (%d)"
        % (len(l2s), len(l3s)),
    )

    # Which producer each accepted load used
    bad2 = [k for k in l2s if pc_of(producer_of[k]) != l1]
    check(
        not bad2,
        "CL: every accepted L2 used a producer at L1's PC 0x%x (%d did not, "
        "or their producer's PC is not in the log)" % (l1, len(bad2)),
    )
    bad3 = [k for k in l3s if pc_of(producer_of[k]) != l2]
    check(
        not bad3,
        "CL: every accepted L3 used a producer at L2's PC 0x%x (%d did not, "
        "or their producer's PC is not in the log)" % (l2, len(bad3)),
    )

    # One level at a time: when L3 is accepted, L2's own producer (if L2 was
    # itself accepted) has been validated
    nested = []
    for k in l3s:
        p2 = producer_of[k]
        if p2 in producer_of:
            p1 = producer_of[p2]
            if p1 not in val_line or not val_line[p1] < acc_line[k]:
                nested.append(k)
    check(
        not nested,
        "CL: no L3 was accepted before the producer of its L2 was validated "
        "(pairwise speculation, not nested) (%d were)" % len(nested),
    )

    # Complete chains: accept(L2, producer L1) < validate(L1) <
    # accept(L3, producer L2) < validate(L2), with the same L2 in both roles
    chains = [
        (producer_of[producer_of[k]], producer_of[k], k)
        for k in l3s
        if producer_of[k] in producer_of
        and producer_of[k] in val_line
        and producer_of[producer_of[k]] in val_line
        and acc_line[producer_of[k]]
        < val_line[producer_of[producer_of[k]]]
        < acc_line[k]
        < val_line[producer_of[k]]
    ]
    check(
        len(chains) >= 1,
        "CL: at least one complete chain: accept(L2, producer L1) < "
        "validate(L1) < accept(L3, producer L2) < validate(L2) (%d of %d "
        "accepted L3)" % (len(chains), len(l3s)),
    )

    # L3 used L2's prediction before L2 was validated, whenever L2 was
    late = [
        k
        for k in l3s
        if producer_of[k] in val_line
        and not acc_line[k] < val_line[producer_of[k]]
    ]
    used = [k for k in l3s if producer_of[k] in val_line]
    check(
        len(used) >= 1 and not late,
        "CL: L3 was accepted before the validation of the L2 whose "
        "prediction it used (%d of %d, %d late)"
        % (len(used) - len(late), len(l3s), len(late)),
    )

    # Recovery from a wrong L2 prediction that L3 used
    l2_redirects = [p for p in redirects if val_pc.get(p) == l2]
    used_redirects = [
        p for p in l2_redirects if any(producer_of[k] == p for k in l3s)
    ]
    check(
        len(used_redirects) >= 1,
        "CL: at least one incorrect L2 prediction that L3 used triggered a "
        "recovery redirect (%d of %d redirects of L2)"
        % (len(used_redirects), len(l2_redirects)),
    )
    bad = []
    for p2 in used_redirects:
        why = []
        if pred_outcome.get(p2) != "incorrect":
            why.append("L2 outcome %s" % pred_outcome.get(p2))
        for k in l3s:
            if producer_of[k] != p2:
                continue
            outcome = cons_outcome.get(k)
            if outcome is None or outcome[1] != "squashed":
                why.append("L3 %s: %s" % (k, outcome and outcome[1]))
            elif not outcome[0] > redirects[p2]:
                why.append("L3 %s squashed before the redirect" % (k,))
        if p2 in producer_of:
            outcome = cons_outcome.get(p2)
            if outcome is None or outcome[1] != "committed":
                why.append("L2 as a consumer: %s" % (outcome and outcome[1]))
        if why:
            bad.append((p2, why))
    check(
        not bad,
        "CL: after each such redirect the L3 was squashed after it and the "
        "L2, a consumer of L1, committed (%d fail)" % len(bad),
    )
    for p2, why in bad[:5]:
        report("L2 %s: %s" % (p2, "; ".join(why)))
    dual = [
        p
        for p in used_redirects
        if p in producer_of
        and cons_outcome.get(p, (0, None, None))[1] == "committed"
    ]
    check(
        len(dual) >= 1,
        "CL: at least one redirected L2 was also an accepted consumer of "
        "L1 and committed as one (%d of %d)"
        % (len(dual), len(used_redirects)),
    )

    other = [p for p in redirects if val_pc.get(p) not in (l1, l2)]
    check(
        not other,
        "CL: every recovery redirect is for an L1 or an L2 (%d are not)"
        % len(other),
    )
    report(
        "accepted: %d L2, %d L3; complete chains: %d; redirects: %d of L2, "
        "%d of L1"
        % (
            len(l2s),
            len(l3s),
            len(chains),
            len(l2_redirects),
            len([p for p in redirects if val_pc.get(p) == l1]),
        )
    )


def chain_trace_checks(inv, producer_pc, consumer_pcs):
    """What A's committed trace says about the three loads of the chain
    program: L1 loaded from each of the slots, a constant stride apart, L2
    from the root every time, and L3 from node A in the training calls and
    from node B in the last call of each cycle.  The refetched L3 loaded from
    node B, whatever happened to a speculative L3."""
    l1, l2, l3 = producer_pc, consumer_pcs[0], consumer_pcs[1]
    a1 = inv["load_addrs"].get(l1, {})
    a2 = inv["load_addrs"].get(l2, {})
    a3 = inv["load_addrs"].get(l3, {})
    check(
        len(a1) == CHAIN_SLOT_COUNT
        and all(n == CHAIN_CYCLES for n in a1.values())
        and all(a % CHAIN_STRIDE == 0 for a in a1),
        "A trace: L1 loaded from %d slots, %d times each, at multiples of "
        "%d (found %d addresses)"
        % (CHAIN_SLOT_COUNT, CHAIN_CYCLES, CHAIN_STRIDE, len(a1)),
    )
    iterations = CHAIN_CYCLES * CHAIN_SLOT_COUNT
    check(
        len(a2) == 1
        and sum(a2.values()) == iterations
        and all(a % CHAIN_STRIDE == 8 for a in a2),
        "A trace: L2 loaded %d times from one address, the root's next "
        "field (found %s)"
        % (
            iterations,
            ", ".join("0x%x x%d" % kv for kv in sorted(a2.items())),
        ),
    )
    check(
        len(a3) == 2
        and sorted(a3.values())
        == sorted([CHAIN_CYCLES * CHAIN_TRAIN, CHAIN_CYCLES * CHAIN_USE])
        and all(a % CHAIN_STRIDE == 24 for a in a3)
        and max(a3) - min(a3) == CHAIN_STRIDE,
        "A trace: L3 loaded %d times from node A and %d times from node B, "
        "%d bytes apart (found %s)"
        % (
            CHAIN_CYCLES * CHAIN_TRAIN,
            CHAIN_CYCLES * CHAIN_USE,
            CHAIN_STRIDE,
            ", ".join("0x%x x%d" % kv for kv in sorted(a3.items())),
        ),
    )


THRESHOLDS = (1, 2, 3)


def read_cpu_config(out, name):
    """The parameters of [system.cpu] in OUT/<name>/config.ini, as
    {name: value string}, or None if the file or the section is missing"""
    try:
        with open(os.path.join(out, name, "config.ini")) as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    params = None
    for line in lines:
        if line.startswith("["):
            if params is not None:
                break
            if line.strip() == "[system.cpu]":
                params = {}
        elif params is not None and "=" in line:
            key, value = line.split("=", 1)
            params[key.strip()] = value.strip()
    return params


def threshold_main(out):
    """The confidence-threshold test.  The fault program is run with LVP off
    (TA) and with prediction only, at lvpConfThreshold 1, 2 and 3 (T1, T2 and
    T3).  Prediction only changes nothing but the cpu.lvp statistics (the
    non-LVP statistics and the committed trace must equal TA's, which this
    checks), so the pipeline's timing is the same in all four runs.  The
    predictor's table is trained only by committed loads, in program order,
    and the threshold only decides whether a lookup returns the entry it
    finds (LastValuePredictor::lookup), so with identical timing every
    lookup finds the same entry in every run.  Hence:
      - lookups and trainings are equal across thresholds;
      - the lookups that are confident at a higher threshold are a subset
        of those at a lower one, and each of them reaches the same terminal
        outcome, so confidentPredictions, correct and incorrect never
        increase with the threshold.
    No exact counter totals are asserted."""
    names = ["TA"] + ["T%d" % t for t in THRESHOLDS]
    runs = names[1:]
    expected_checksum = fault_checksum()

    section("Preflight: every threshold run completed and wrote its outputs")
    stats, problems = preflight(out, names, runs)
    if stats is None:
        print()
        print(
            "%d preflight check(s) failed: nothing was compared"
            % len(problems)
        )
        return 1

    section("Run configuration")
    for n in names:
        params = read_cpu_config(out, n)
        check(
            params is not None,
            "%s: [system.cpu] parameters found in config.ini" % n,
        )
        if params is None:
            continue
        want = (
            {"enableLVP": "false"}
            if n == "TA"
            else {
                "enableLVP": "true",
                "lvpSpecAddr": "false",
                "lvpConfThreshold": n[1:],
            }
        )
        for key, value in want.items():
            check(
                params.get(key) == value,
                "%s: %s is %s (config.ini says %s)"
                % (n, key, value, params.get(key)),
            )

    section("Architectural correctness")
    for n in names:
        check(
            checksum(out, n) == expected_checksum,
            "%s: checksum is %d" % (n, expected_checksum),
        )
    non_lvp = {
        n: {k: v for k, v in stats[n].items() if "cpu.lvp." not in k}
        for n in names
    }
    for n in runs:
        check(
            non_lvp["TA"] == non_lvp[n],
            "TA and %s: identical statistics apart from cpu.lvp." % n,
        )
        check(
            filecmp.cmp(
                os.path.join(out, "TA", "trace.txt"),
                os.path.join(out, n, "trace.txt"),
                shallow=False,
            ),
            "TA and %s: identical committed trace" % n,
        )

    section("Confidence classification (prediction only)")
    lv = {n: lvp(stats[n]) for n in runs}
    for n in runs:
        s = lv[n]
        check(
            s["specLoadsIssued"] == 0
            and s["specLoadsCommitted"] == 0
            and s["specLoadsFaulted"] == 0
            and s["specLoadsSquashed"] == 0
            and s["mispredictSquashes"] == 0,
            "%s: no speculative loads and no recovery redirects" % n,
        )
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
        all(lv[n]["lookups"] > 0 for n in runs),
        "every run looked the predictor up (the test is not vacuous)",
    )
    for key in ("lookups", "trainings"):
        check(
            len({lv[n][key] for n in runs}) == 1,
            "%s is the same at every threshold (%s)"
            % (key, ", ".join("%s %d" % (n, lv[n][key]) for n in runs)),
        )
    for key in ("confidentPredictions", "correct", "incorrect"):
        values = [lv[n][key] for n in runs]
        check(
            all(a >= b for a, b in zip(values, values[1:])),
            "%s never increases with the threshold (%s)"
            % (key, ", ".join("%s %d" % (n, lv[n][key]) for n in runs)),
        )
    check(
        lv[runs[0]]["confidentPredictions"] > 0,
        "the lowest threshold makes confident predictions "
        "(a repeated pointer reaches confidence 1 after two trainings)",
    )

    section("Counters by threshold (reported only)")
    for key in (
        "lookups",
        "confidentPredictions",
        "correct",
        "incorrect",
        "predSquashed",
        "trainings",
    ):
        report(
            "%s: %s"
            % (key, ", ".join("%s %d" % (n, lv[n][key]) for n in runs))
        )

    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


def check_config(out, name, want):
    """Check that [system.cpu] of OUT/<name>/config.ini has each parameter in
    want ({parameter: expected string, or int for an address}).  Addresses
    are compared as integers, whatever their notation in the file."""
    params = read_cpu_config(out, name)
    check(
        params is not None,
        "%s: [system.cpu] parameters found in config.ini" % name,
    )
    if params is None:
        return
    for key, value in want.items():
        got = params.get(key)
        if isinstance(value, int):
            try:
                same = got is not None and int(got, 0) == value
            except ValueError:
                same = False
            text = "%#x" % value
        else:
            same = got == value
            text = value
        check(
            same,
            "%s: %s is %s (config.ini says %s)" % (name, key, text, got),
        )


PCWINDOW_RUNS = ["BASE", "FULL", "NO_PRODUCER", "NO_CONSUMER"]


def pcwindow_main(out):
    """The PC-window test.  The fault program is run with LVP off (BASE) and
    with LVP and address speculation under three windows, from the producer
    and consumer PCs of its own binary (load_pcs.txt; both loads are 4-byte
    instructions, so consumer + 4 is the end of the consumer):
      FULL         [producer, consumer + 4)  both loads inside
      NO_PRODUCER  [consumer, consumer + 4)  producer outside
      NO_CONSUMER  [producer, consumer)      consumer outside
    The window is half-open, and Execute::lvpInSpecWindow needs the
    producer and the consumer of a speculative load inside it, so only FULL
    can issue speculative loads.  Speculation changes timing but never the
    committed trace, so every run must match BASE's trace.  FULL's non-LVP
    statistics (cycles, ...) differ from BASE's by design; those of
    NO_PRODUCER and NO_CONSUMER, which issue no speculative load, must not.
    No exact counter totals are asserted."""
    names = PCWINDOW_RUNS
    runs = names[1:]
    expected_checksum = fault_checksum()

    section("Preflight: every PC-window run completed and wrote its outputs")
    stats, problems = preflight(out, names, runs)
    if stats is None:
        print()
        print(
            "%d preflight check(s) failed: nothing was compared"
            % len(problems)
        )
        return 1

    pcs, why = read_load_pcs(out, "fault")
    if pcs is None:
        check(False, "load PCs from the assembly check: " + why)
        print()
        print("the load PCs are unusable: nothing was compared")
        return 1
    producer, consumers = pcs
    consumer = consumers[0]
    check(
        consumer > producer,
        "the consumer (%#x) follows the producer (%#x)" % (consumer, producer),
    )
    windows = {
        "FULL": (producer, consumer + 4),
        "NO_PRODUCER": (consumer, consumer + 4),
        "NO_CONSUMER": (producer, consumer),
    }

    section("Run configuration")
    check_config(out, "BASE", {"enableLVP": "false"})
    for n in runs:
        begin, end = windows[n]
        check_config(
            out,
            n,
            {
                "enableLVP": "true",
                "lvpSpecAddr": "true",
                "executeAllowEarlyMemoryIssue": "true",
                "lvpSpecPcBegin": begin,
                "lvpSpecPcEnd": end,
            },
        )
    full_begin, full_end = windows["FULL"]
    check(
        full_begin <= producer < full_end
        and full_begin <= consumer < full_end,
        "FULL contains the producer and the consumer",
    )
    begin, end = windows["NO_PRODUCER"]
    check(
        not begin <= producer < end and begin <= consumer < end,
        "NO_PRODUCER excludes the producer and contains the consumer",
    )
    begin, end = windows["NO_CONSUMER"]
    check(
        begin <= producer < end and not begin <= consumer < end,
        "NO_CONSUMER contains the producer and excludes the consumer",
    )

    section("Architectural correctness")
    for n in names:
        check(
            checksum(out, n) == expected_checksum,
            "%s: checksum is %d" % (n, expected_checksum),
        )
    for n in runs:
        check(
            filecmp.cmp(
                os.path.join(out, "BASE", "trace.txt"),
                os.path.join(out, n, "trace.txt"),
                shallow=False,
            ),
            "BASE and %s: identical committed trace" % n,
        )

    non_lvp = {
        n: {k: v for k, v in stats[n].items() if "cpu.lvp." not in k}
        for n in names
    }
    for n in ("NO_PRODUCER", "NO_CONSUMER"):
        check(
            non_lvp["BASE"] == non_lvp[n],
            "BASE and %s: identical statistics apart from cpu.lvp." % n,
        )

    section("Speculation by window")
    lv = {n: lvp(stats[n]) for n in runs}
    for n in runs:
        s = lv[n]
        check(
            s["predCarryIn"] == 0
            and s["specCarryIn"] == 0
            and s["predInFlight"] == 0
            and s["specInFlight"] == 0,
            "%s: no carry-in and nothing in flight at the end" % n,
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
    check(
        lv["FULL"]["specLoadsIssued"] > 0,
        "FULL: speculative loads were issued (%d)"
        % lv["FULL"]["specLoadsIssued"],
    )
    for n in ("NO_PRODUCER", "NO_CONSUMER"):
        check(
            lv[n]["specLoadsIssued"] == 0 and lv[n]["mispredictSquashes"] == 0,
            "%s: no speculative loads (%d) and no recovery redirects (%d)"
            % (n, lv[n]["specLoadsIssued"], lv[n]["mispredictSquashes"]),
        )

    section("Counters by window (reported only)")
    for key in (
        "lookups",
        "trainings",
        "confidentPredictions",
        "correct",
        "incorrect",
        "specLoadsIssued",
        "specLoadsCommitted",
        "specLoadsFaulted",
        "specLoadsSquashed",
        "mispredictSquashes",
    ):
        report(
            "%s: %s"
            % (key, ", ".join("%s %d" % (n, lv[n][key]) for n in runs))
        )

    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


# The markers in gem5's output: "fatal: " and "panic: " anywhere in a line
# (a source location comes before them), and the wrapper that fatal_if adds
# to a fatal message, which is not a second marker
FATAL_MARKER = re.compile(r"\bfatal: ")
PANIC_MARKER = re.compile(r"\bpanic: ")
FATAL_CONDITION = re.compile(r"^fatal condition .*? occurred: ")

# The configurations that the constructors must refuse: (name, the text of
# the fatal message, from Execute::Execute and LastValuePredictor).  The
# fatal in Execute names the stage ("<stage>: "), the predictor's does not.
INVALID_CONFIGS = [
    ("spec_without_lvp", "execute", "lvpSpecAddr requires enableLVP"),
    (
        "spec_without_early_issue",
        "execute",
        "lvpSpecAddr requires executeAllowEarlyMemoryIssue",
    ),
    (
        "window_without_spec",
        "execute",
        "lvpSpecPcBegin and lvpSpecPcEnd require lvpSpecAddr",
    ),
    (
        "window_begin_after_end",
        "execute",
        "lvpSpecPcBegin (0x10200) must be below lvpSpecPcEnd (0x10100)",
    ),
    (
        "window_begin_equals_end",
        "execute",
        "lvpSpecPcBegin (0x10100) must be below lvpSpecPcEnd (0x10100)",
    ),
    (
        "threshold_zero",
        None,
        "lvpConfThreshold must be in [1, 3] (got 0)",
    ),
    (
        "threshold_four",
        None,
        "lvpConfThreshold must be in [1, 3] (got 4)",
    ),
    (
        "table_not_power_of_two",
        None,
        "lvpTableEntries must be a non-zero power of two (got 1000)",
    ),
    (
        "table_zero",
        None,
        "lvpTableEntries must be a non-zero power of two (got 0)",
    ),
]


def invalid_config_main(out):
    """The invalid-configuration test.  gem5 must refuse each configuration
    in INVALID_CONFIGS when it builds the CPU, with the fatal message from
    the source: a nonzero exit status (recorded by the runner in
    OUT/<name>.rc) and, in OUT/<name>.out, exactly one fatal marker whose
    message, without the source location before it and the fatal_if
    "fatal condition ... occurred:" wrapper, is the expected one, and no
    panic, traceback or program output (an unrelated failure, or a run that
    went on).  A refused run writes none of the artifacts of a
    successful one, so none are required."""
    section("Invalid configurations are refused with the expected message")
    for name, stage, message in INVALID_CONFIGS:
        rc_path = os.path.join(out, name + ".rc")
        status = None
        try:
            with open(rc_path) as f:
                status = int(f.read().strip())
        except (OSError, ValueError) as e:
            check(
                False,
                "%s: exit status recorded in %s (%s)" % (name, rc_path, e),
            )
            continue
        check(
            status != 0,
            "%s: gem5 exit status is nonzero (got %d)" % (name, status),
        )

        out_path = os.path.join(out, name + ".out")
        try:
            with open(out_path) as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError) as e:
            check(
                False, "%s: output can be read (%s: %s)" % (name, out_path, e)
            )
            continue

        # gem5 writes "[<source>:<line>: ]fatal: <message>", and fatal_if
        # puts "fatal condition <condition> occurred: " before the message
        markers = [
            line[m.end() :].rstrip()
            for line in lines
            for m in FATAL_MARKER.finditer(line)
        ]
        expected = re.compile(
            (r"\S*%s: " % stage if stage else "") + re.escape(message)
        )
        check(
            len(markers) == 1,
            "%s: exactly one fatal marker (%d)%s"
            % (
                name,
                len(markers),
                "" if len(markers) < 2 else ": " + markers[1],
            ),
        )
        check(
            bool(markers)
            and expected.fullmatch(FATAL_CONDITION.sub("", markers[0]))
            is not None,
            "%s: output has the fatal message: %s" % (name, message),
        )
        unrelated = [
            l
            for l in lines
            if PANIC_MARKER.search(l)
            or l.startswith("Traceback")
            or l.startswith("checksum")
        ]
        check(
            not unrelated,
            "%s: no panic, traceback or program output (%d)%s"
            % (
                name,
                len(unrelated),
                "" if not unrelated else ": " + unrelated[0],
            ),
        )

    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# The predictor-policy tests: "confidence" and "alias" (lvp_policy.c)
# ---------------------------------------------------------------------------

POLICY_CONF_N = 48  # CONF_N of lvp_policy.c
POLICY_ALIAS_TRAIN = 12  # ALIAS_TRAIN of lvp_policy.c
POLICY_ENTRIES = 1024
POLICY_MAX_CONF = 3
POLICY_THRESHOLD = 3

POLICY_CHECKSUMS = {
    "confidence": POLICY_CONF_N * (1000 + 2000 + 1000),
    "alias": POLICY_ALIAS_TRAIN * 111 * 2 + 222,
}

LOOKUP_RE = re.compile(
    r"table lookup pc: (0x[0-9a-f]+) index: (\d+) value: (0x[0-9a-f]+|0) "
    r"confidence: (\d+) confident: (\d)\s*$"
)
TRAIN_RE = re.compile(
    r"table train pc: (0x[0-9a-f]+) index: (\d+) stored: (0x[0-9a-f]+|0) "
    r"actual: (0x[0-9a-f]+|0) confidence: (\d+) -> (\d+)\s*$"
)
VALIDATED_RE = re.compile(
    r"Validated inst: .* pc: (0x[0-9a-f]+) .*predicted: (0x[0-9a-f]+|0) "
    r"actual: (0x[0-9a-f]+|0) \((correct|incorrect)\)"
)


def policy_index(pc):
    """The table index of a load at pc, from the formula of
    LastValuePredictor::index (not from the simulator's own output)"""
    return (pc >> 1) & (POLICY_ENTRIES - 1)


def read_policy_pcs(out):
    """{label: pc} from OUT/policy_pcs.txt, written by the runner from the
    binary's disassembly"""
    pcs = {}
    with open(os.path.join(out, "policy_pcs.txt")) as f:
        for line in f:
            label, pc = line.split()
            pcs[label] = int(pc, 16)
    return pcs


def read_policy_events(path):
    """The table lookups and trainings in the LVP debug log, in the order
    that the simulator wrote them (the order that it did them in), as
    ('lookup', pc, index, value, confidence, confident) and
    ('train', pc, index, stored, actual, before, after); and the validated
    predictions as (pc, predicted, actual, outcome)"""
    events = []
    validated = []
    with open(path, errors="replace") as f:
        for line in f:
            m = LOOKUP_RE.search(line)
            if m:
                events.append(
                    (
                        "lookup",
                        int(m.group(1), 16),
                        int(m.group(2)),
                        int(m.group(3), 16),
                        int(m.group(4)),
                        int(m.group(5)),
                    )
                )
                continue
            m = TRAIN_RE.search(line)
            if m:
                events.append(
                    (
                        "train",
                        int(m.group(1), 16),
                        int(m.group(2)),
                        int(m.group(3), 16),
                        int(m.group(4), 16),
                        int(m.group(5)),
                        int(m.group(6)),
                    )
                )
                continue
            m = VALIDATED_RE.search(line)
            if m:
                validated.append(
                    (
                        int(m.group(1), 16),
                        int(m.group(2), 16),
                        int(m.group(3), 16),
                        m.group(4),
                    )
                )
    return events, validated


class TableModel:
    """A reference model of the last-value table, to replay the events with.

    kind "paper": untagged, a match increments the 2-bit counter and a
        mismatch decrements it, the actual value is stored (the current
        implementation); entries start at value 0, confidence 0;
    kind "reset": untagged, but a mismatch resets the counter to 0;
    kind "tagged": what the implementation did before: a full-PC tag and a
        valid bit, a mismatch resets the counter, and a PC that finds
        another PC's entry replaces it with confidence 0.
    """

    def __init__(self, kind):
        self.kind = kind
        self.value = {}
        self.conf = {}
        self.tag = {}

    def entry(self, idx, pc):
        """(value, confidence) that a lookup of pc sees"""
        if self.kind == "tagged" and self.tag.get(idx) != pc:
            return (0, 0)
        return (self.value.get(idx, 0), self.conf.get(idx, 0))

    def lookup(self, idx, pc):
        value, conf = self.entry(idx, pc)
        return value, conf, conf >= POLICY_THRESHOLD

    def train(self, idx, pc, actual):
        """returns (stored value, confidence before, confidence after)"""
        if self.kind == "tagged":
            if self.tag.get(idx) != pc:
                before = self.entry(idx, pc)
                self.tag[idx] = pc
                self.value[idx] = actual
                self.conf[idx] = 0
                return before[0], before[1], 0
        stored = self.value.get(idx, 0)
        before = self.conf.get(idx, 0)
        if stored == actual:
            after = min(before + 1, POLICY_MAX_CONF)
        elif self.kind == "paper":
            after = max(before - 1, 0)
        else:
            after = 0
        self.value[idx] = actual
        self.conf[idx] = after
        return stored, before, after


def replay(events, kind):
    """Replays the events on a TableModel.  Returns (disagreements with the
    logged events, [(event number, value, confidence, confident) of each
    lookup as the model has it])."""
    model = TableModel(kind)
    bad = []
    lookups = []
    for n, ev in enumerate(events):
        if ev[0] == "lookup":
            _, pc, idx, value, conf, confident = ev
            mv, mc, mt = model.lookup(idx, pc)
            lookups.append((n, mv, mc, int(mt)))
            if (value, conf, confident) != (mv, mc, int(mt)):
                bad.append(n)
        else:
            _, pc, idx, stored, actual, before, after = ev
            ms, mb, ma = model.train(idx, pc, actual)
            if (stored, before, after) != (ms, mb, ma):
                bad.append(n)
    return bad, lookups


def policy_common(out, mode):
    """The checks both policy tests share.  Returns (stats, events,
    validated, pcs) or None if the runs are not usable"""
    names = ["A", "B", "CL"]

    section("Preflight: every run completed and wrote its outputs")
    stats, problems = preflight(out, names, ["B", "CL"])
    if stats is None:
        print()
        print(
            "%d preflight check(s) failed: nothing was compared"
            % len(problems)
        )
        return None

    section("Run configuration")
    check_config(out, "A", {"enableLVP": "false"})
    for n in ("B", "CL"):
        check_config(
            out,
            n,
            {
                "enableLVP": "true",
                "lvpSpecAddr": "false",
                "lvpConfThreshold": str(POLICY_THRESHOLD),
                "lvpTableEntries": str(POLICY_ENTRIES),
            },
        )

    section("Architectural correctness")
    want = POLICY_CHECKSUMS[mode]
    for n in names:
        check(checksum(out, n) == want, "%s: checksum is %d" % (n, want))
    check(
        filecmp.cmp(
            os.path.join(out, "A", "trace.txt"),
            os.path.join(out, "B", "trace.txt"),
            shallow=False,
        ),
        "A and B: identical committed trace (prediction only changes "
        "nothing architectural)",
    )
    non_lvp = {
        n: {k: v for k, v in stats[n].items() if "cpu.lvp." not in k}
        for n in ("A", "B")
    }
    check(
        non_lvp["A"] == non_lvp["B"],
        "A and B: identical statistics apart from cpu.lvp.",
    )

    pcs = read_policy_pcs(out)
    events, validated = read_policy_events(
        os.path.join(out, "CL", "trace.txt")
    )
    return stats, events, validated, pcs


def policy_accounting(stats, events, validated):
    """Cross-checks of the log against the statistics of CL, and the
    lifecycle identity"""
    section("Statistics against the log")
    s = lvp(stats["CL"])
    n_lookup = sum(1 for e in events if e[0] == "lookup")
    n_conf = sum(1 for e in events if e[0] == "lookup" and e[5])
    n_train = sum(1 for e in events if e[0] == "train")
    check(
        s["lookups"] == n_lookup,
        "lookups statistic (%d) = lookup lines in the log (%d)"
        % (s["lookups"], n_lookup),
    )
    check(
        s["confidentPredictions"] == n_conf,
        "confidentPredictions (%d) = confident lookup lines (%d)"
        % (s["confidentPredictions"], n_conf),
    )
    check(
        s["trainings"] == n_train,
        "trainings (%d) = train lines (%d)" % (s["trainings"], n_train),
    )
    check(
        s["correct"] + s["incorrect"] == len(validated),
        "correct + incorrect (%d) = validated lines (%d)"
        % (s["correct"] + s["incorrect"], len(validated)),
    )
    check(
        s["confidentPredictions"] + s["predCarryIn"]
        == s["correct"]
        + s["incorrect"]
        + s["predFaulted"]
        + s["predSquashed"]
        + s["predInFlight"],
        "confident + carry-in = correct + incorrect + faulted + squashed "
        "+ in flight",
    )
    check(
        s["predInFlight"] == 0 and s["predCarryIn"] == 0,
        "no prediction in flight at the end, none carried in",
    )
    check(
        s["specLoadsIssued"] == 0,
        "no speculative load (address speculation is off)",
    )
    report(
        "lookups %d, confident %d, trainings %d, correct %d, incorrect %d, "
        "squashed %d"
        % (
            n_lookup,
            n_conf,
            n_train,
            s["correct"],
            s["incorrect"],
            s["predSquashed"],
        )
    )
    return s


def confidence_main(out):
    """The confidence-counter test (see lvp_policy.c, POLICY_CONF).

    The log of every table lookup and training of the one load is replayed
    on a model of the paper's counter (match +1, mismatch -1, saturating at
    0 and 3).  Every logged lookup and training must agree with it.  Then
    the same events are replayed on a model that resets the counter on a
    mismatch, and the two are compared where they must differ: the counter
    after the value change, and when confident predictions of the new value
    resume."""
    res = policy_common(out, "confidence")
    if res is None:
        return 1
    stats, events, validated, pcs = res
    pc = pcs["load"]

    section("Replay against the model of the table")
    check(len(events) > 0, "the log has table events (%d)" % len(events))
    check(
        all(e[1] == pc and e[2] == policy_index(pc) for e in events),
        "every event is for the one load, at index %d of the formula"
        % policy_index(pc),
    )
    trains = [e for e in events if e[0] == "train"]
    check(
        len(trains) == 3 * POLICY_CONF_N,
        "one training for each of the %d loads (got %d)"
        % (3 * POLICY_CONF_N, len(trains)),
    )
    bad, lookups_paper = replay(events, "paper")
    check(
        not bad,
        "every lookup and training agrees with the decrementing untagged "
        "model (%d disagreements)" % len(bad),
    )

    section("The value change lowers the counter by one")
    changes = [
        i
        for i, e in enumerate(events)
        if e[0] == "train" and e[5] == POLICY_MAX_CONF and e[3] != e[4]
    ]
    check(
        len(changes) >= 1,
        "a training with saturated confidence sees a different value "
        "(%d of them)" % len(changes),
    )
    if not changes:
        return finish_policy()
    first = changes[0]
    ev = events[first]
    check(
        (ev[3], ev[4]) == (1000, 2000),
        "the first such training replaces 1000 by 2000 (stored %d, "
        "actual %d)" % (ev[3], ev[4]),
    )
    check(
        ev[6] == POLICY_MAX_CONF - 1,
        "its confidence goes from 3 to 2, not to 0 (3 -> %d)" % ev[6],
    )
    nxt = [e for e in events[first + 1 :] if e[0] == "train"][0]
    check(
        (nxt[3], nxt[4], nxt[5], nxt[6]) == (2000, 2000, 2, 3),
        "the next training repeats 2000 and goes from 2 back to 3 "
        "(%d -> %d)" % (nxt[5], nxt[6]),
    )

    section("When confident predictions resume (lookups after the change)")
    _, lookups_reset = replay(events, "reset")

    def resume(lookups, source):
        """(lookups, trainings) after the change and before the first
        confident lookup of the new value.  The table is read at issue and
        trained at commit, so lookups run ahead of the trainings; the number
        of trainings is the measure that does not depend on that lag.
        source is the log itself or a model of it."""
        n_lookups = 0
        for k, mv, mc, mt in lookups:
            if k < first:
                continue
            if source == "log":
                _, _, _, value, conf, confident = events[k]
                got = (value, confident)
            else:
                got = (mv, mt)
            if got[1] and got[0] == 2000:
                n_trains = sum(
                    1 for e in events[first + 1 : k] if e[0] == "train"
                )
                return n_lookups, n_trains
            n_lookups += 1
        return None, None

    r_log = resume(lookups_paper, "log")
    r_paper = resume(lookups_paper, "model")
    r_reset = resume(lookups_reset, "model")
    report(
        "after the change, before the first confident prediction of 2000 "
        "(lookups, trainings): log %s, decrementing model %s, resetting "
        "model %s" % (r_log, r_paper, r_reset)
    )
    check(
        r_log[1] is not None and r_log == r_paper,
        "the log resumes exactly where the decrementing model does",
    )
    check(
        r_log[1] == 1,
        "after one more matching training (2 -> 3), not three "
        "(%s trainings)" % (r_log[1],),
    )
    check(
        r_reset[1] is None or (r_log[1] is not None and r_log[1] < r_reset[1]),
        "earlier than a counter that is reset on a mismatch would "
        "(%s < %s trainings)" % (r_log[1], r_reset[1]),
    )
    low = [
        events[k]
        for k, mv, mc, mt in lookups_paper
        if k > first and events[k][4] == 2
    ]
    check(
        len(low) >= 1 and all(not e[5] for e in low),
        "a lookup sees the lowered confidence 2 and is not confident "
        "(%d such lookups)" % len(low),
    )
    check(
        r_log[0] == 1,
        "with every lookup after the earlier trainings, one lookup is lost "
        "to the lowered counter and the next is confident (%s lookups)"
        % (r_log[0],),
    )

    policy_accounting(stats, events, validated)
    section("Predictions used before the change was seen")
    s = lvp(stats["CL"])
    check(
        s["incorrect"] >= 1,
        "at least one confident prediction is validated incorrect "
        "(%d): the change is seen after loads were predicted" % s["incorrect"],
    )
    return finish_policy()


def alias_main(out):
    """The untagged-table test (see lvp_policy.c, POLICY_ALIAS)."""
    res = policy_common(out, "alias")
    if res is None:
        return 1
    stats, events, validated, pcs = res
    pa, pb = pcs["ld_a"], pcs["ld_b"]

    section("The two loads alias (from the formula)")
    check(pa != pb, "ld_a %#x and ld_b %#x are different loads" % (pa, pb))
    check(
        policy_index(pa) == policy_index(pb),
        "(pc >> 1) & %d is %d for both"
        % (POLICY_ENTRIES - 1, policy_index(pa)),
    )
    check(
        all(e[2] == policy_index(e[1]) for e in events),
        "every logged index is the formula's",
    )
    check(
        {e[1] for e in events} == {pa, pb},
        "the log has events only for these two loads",
    )

    section("Replay against the model of the table")
    bad, _ = replay(events, "paper")
    check(
        not bad,
        "every lookup and training agrees with the untagged model "
        "(%d disagreements)" % len(bad),
    )
    bad_tagged, _ = replay(events, "tagged")
    check(
        len(bad_tagged) > 0,
        "and the model of the previous, tagged table disagrees with the "
        "log in %d events: the test tells them apart" % len(bad_tagged),
    )

    section("One load uses what the other trained")
    lb = [i for i, e in enumerate(events) if e[0] == "lookup" and e[1] == pb]
    tb = [i for i, e in enumerate(events) if e[0] == "train" and e[1] == pb]
    check(len(lb) == 1 and len(tb) == 1, "ld_b runs once")
    if len(lb) != 1 or len(tb) != 1:
        return finish_policy()
    look = events[lb[0]]
    check(
        look[5] == 1 and look[4] == POLICY_MAX_CONF and look[3] == 111,
        "ld_b's lookup is confident (confidence %d) with ld_a's value %d, "
        "which only ld_a has trained" % (look[4], look[3]),
    )
    check(
        any(
            v[0] == pb and v[1] == 111 and v[2] == 222 and v[3] == "incorrect"
            for v in validated
        ),
        "ld_b's prediction 111 is validated against its real value 222 "
        "(incorrect)",
    )
    tr = events[tb[0]]
    check(
        (tr[3], tr[4], tr[5], tr[6]) == (111, 222, 3, 2),
        "ld_b's training sees ld_a's entry (stored %d, actual %d) and lowers "
        "its confidence %d -> %d" % (tr[3], tr[4], tr[5], tr[6]),
    )
    after = [e for e in events[tb[0] + 1 :] if e[0] == "lookup" and e[1] == pa]
    check(len(after) > 0, "ld_a runs again after ld_b")
    if after:
        e = after[0]
        check(
            e[4] == 2 and e[5] == 0 and e[3] == 222,
            "ld_a's next lookup sees ld_b's training: value %d, confidence "
            "%d, not confident (a tagged table would still be confident)"
            % (e[3], e[4]),
        )

    policy_accounting(stats, events, validated)
    return finish_policy()


def finish_policy():
    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


def main():
    if len(sys.argv) not in (2, 3) or (
        len(sys.argv) == 3
        and sys.argv[2] not in EXPECTED_CHECKSUMS
        and sys.argv[2]
        not in (
            "threshold",
            "pcwindow",
            "invalid-config",
            "confidence",
            "alias",
        )
    ):
        print(__doc__)
        return 2
    out = sys.argv[1]
    if len(sys.argv) == 3 and sys.argv[2] == "threshold":
        return threshold_main(out)
    if len(sys.argv) == 3 and sys.argv[2] == "pcwindow":
        return pcwindow_main(out)
    if len(sys.argv) == 3 and sys.argv[2] == "invalid-config":
        return invalid_config_main(out)
    if len(sys.argv) == 3 and sys.argv[2] == "confidence":
        return confidence_main(out)
    if len(sys.argv) == 3 and sys.argv[2] == "alias":
        return alias_main(out)
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
    acc_fault = {}  # consumer -> its acceptance line shows a translation fault
    acc_src = {}  # consumer -> the predicted source value it used
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
                # The instruction prints "(translation fault: ...)" instead
                # of its name once its translation has faulted
                acc_fault[(int(g[1]), int(g[6]))] = (
                    "(translation fault:" in line
                )
                src = re.search(r"source: 0x([0-9a-f]+)", line)
                acc_src[(int(g[1]), int(g[6]))] = (
                    int(src.group(1), 16) if src else None
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
            {
                "phase": NUM_SLOTS // PHASE_BLOCK - 1,
                "fanout": NUM_SLOTS // PHASE_BLOCK - 1,
                "fault": FAULT_CYCLES,
                "chain": CHAIN_CYCLES,
            }[workload],
        )
        hook_checks(
            os.path.join(out, "CL", "trace.txt"),
            redirects,
            val_pc,
            lvp(stats["CL"]),
            workload,
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
        if workload == "fault":
            fault_checks(
                c,
                accepted,
                acc_line,
                acc_fault,
                acc_src,
                val_line,
                redirects,
                pred_outcome,
                cons_outcome,
            )
        if workload == "chain":
            chain_checks(
                producer_pc,
                consumer_pcs[0],
                consumer_pcs[1],
                c,
                accepted,
                acc_line,
                acc_pc,
                val_line,
                val_pc,
                redirects,
                pred_outcome,
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
        iterations = ITERATIONS[workload]
        total = sum(inv["loads"].values())
        check(
            total == n_loads * iterations,
            "A trace: exactly %d committed loads (found %d)"
            % (n_loads * iterations, total),
        )
        check(
            len(inv["loads"]) == n_loads,
            "A trace: exactly %d distinct load PCs (found %d)"
            % (n_loads, len(inv["loads"])),
        )
        check(
            inv["loads"]
            == {pc: iterations for pc in [producer_pc] + consumer_pcs},
            "A trace: the committed loads are exactly the producer 0x%x "
            "and the consumer(s) %s of the assembly check, %d times each"
            % (
                producer_pc,
                ", ".join("0x%x" % pc for pc in consumer_pcs),
                iterations,
            ),
        )
        if workload == "fault":
            fault_trace_checks(inv, consumer_pcs)
        elif workload == "chain":
            chain_trace_checks(inv, producer_pc, consumer_pcs)
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
            n_consumers * ITERATIONS[workload],
            100.0
            * c["specLoadsCommitted"]
            / (n_consumers * ITERATIONS[workload]),
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
