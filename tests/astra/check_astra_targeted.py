#!/usr/bin/env python3
"""Checks the outputs of run_astra_targeted.sh.

Usage:
  check_astra_targeted.py asm MODE ASTRA_S SYMBOLS PCS_OUT
  check_astra_targeted.py MODE OUT

MODE is cold, alias, conf, squash, fault, invalid-config or lvp-coexist (see
run_astra_targeted.sh and the "Targeted tests" section of README.md).  OUT is
the directory the runner wrote for the mode: for each run NAME, the gem5
output directory OUT/NAME, and OUT/NAME.out and OUT/NAME.rc with its output and
exit status.

The ASTRA golden model, the trace reader and the replay are those of
check_astra.py (imported).  They are a re-implementation of the same
inferences as src/cpu/minor/astra.hh, so a replay that agrees shows that the
implementation does what astra.hh documents, NOT that the documented choices
are the paper's: the cold start, the stride sign, the SS index and the 16 bit
truncation are inferences (see astra.hh and README.md).  The tests below add
what check_astra.py does not cover; every expectation is derived from the
program's structure and from the model, never from a count taken from a run.
"""

import importlib.util
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "check_astra", os.path.join(HERE, "check_astra.py")
)
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)

ENTRIES = 1024
fails = []

# astra_targeted.c parameters
ALIAS_TRAIN = 8
CONF_N = 48
SQ_BLOCKS = 4
FAULT_PAGES = 2
FAULT_ADDR = 0x20000000
FAULT_PAGE = 4096
PHASE_CHECKSUM = (7 + 11) * (16384 // 2)  # tests/lvp/check_lvp_stageb.py


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def report(msg):
    print("INFO  " + msg)


def section(title):
    print()
    print("== " + title)


def finish():
    print()
    print(
        "%d correctness check(s) failed" % len(fails)
        if fails
        else "all correctness checks passed"
    )
    return 1 if fails else 0


def lat_index(pc, entries=ENTRIES):
    """LAT index of a load at pc: the formula of AstraPredictor::latIndex"""
    return (pc >> 1) & (entries - 1)


def ss_index(stride, entries=ENTRIES):
    return (stride & 0xFFFF) & (entries - 1)


def buf_a(k):
    return 7 * k + 3  # value at long index k of bufA


def buf_b(k):
    return 11 * k + 5


# ---------------------------------------------------------------- assembly

EXPECTED_LOADS = {
    "cold": ["cold_a", "cold_b"],
    "alias": ["ld_a", "ld_b"],
    "conf": ["ld_x", "ld_y"],
    "squash": ["sq_wrong%d" % i for i in range(SQ_BLOCKS)]
    + ["sq_real%d" % i for i in range(SQ_BLOCKS)],
    "fault": ["ft_load"],
}
LOAD_INSN = re.compile(
    r"^(l[bhwd]u?|fl[wd]|lr\.[wd]\S*|amo\w+\.[wd]\S*|sc\.\S+)$"
)
INSN_LINE = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)$")


def asm_main(mode, disasm, symbols, pcs_out):
    """Checks that the program has exactly the loads the test is about, at
    the symbol addresses, and the PC index relations the test needs"""
    syms = {}
    with open(symbols) as f:
        for line in f:
            t = line.split()
            if len(t) == 4:
                syms[t[3]] = int(t[0], 16)
            elif len(t) == 3:
                syms[t[2]] = int(t[0], 16)
    loads = []
    count = 0
    with open(disasm) as f:
        for line in f:
            m = INSN_LINE.match(line)
            if not m:
                continue
            count += 1
            op = m.group(3).split("#")[0].strip()
            if m.group(2).startswith("c."):
                loads.append(("compressed", int(m.group(1), 16), op))
            if LOAD_INSN.match(m.group(2)):
                loads.append((m.group(2), int(m.group(1), 16), op))
    print("instructions parsed: %d" % count)
    for mnem, pc, op in loads:
        print("load %x %s %s" % (pc, mnem, op))

    problems = []
    want = {}
    for name in EXPECTED_LOADS[mode]:
        if name not in syms:
            problems.append("symbol %s not found" % name)
        else:
            want[name] = syms[name]
    got = sorted(pc for mnem, pc, op in loads if mnem != "compressed")
    if got != sorted(want.values()):
        problems.append(
            "the loads of the program are at %s, expected exactly %s"
            % (
                ["%x" % p for p in got],
                ["%x" % p for p in sorted(want.values())],
            )
        )
    for mnem, pc, op in loads:
        if mnem == "compressed":
            problems.append("compressed instruction at %x" % pc)
        elif mnem != "ld" or not re.match(r"^\w+,0\((\w+)\)$", op):
            problems.append("unexpected load %x %s %s" % (pc, mnem, op))

    idx = {n: lat_index(pc) for n, pc in want.items()}
    if mode == "cold":
        if idx.get("cold_a") == idx.get("cold_b"):
            problems.append("cold_a and cold_b share a LAT index")
    elif mode == "conf":
        if idx.get("ld_x") == idx.get("ld_y"):
            problems.append("ld_x and ld_y share a LAT index")
    elif mode == "squash":
        if len(set(idx.values())) != len(idx):
            problems.append("two loads of the squash program share an index")
    elif mode == "alias" and not problems:
        a, b = want["ld_a"], want["ld_b"]
        if a == b or (b - a) % 2048:
            problems.append("ld_b is not a multiple of 2048 bytes after ld_a")
        if lat_index(a) != lat_index(b):
            problems.append(
                "ld_a and ld_b do not share a LAT index with 1024 entries"
            )
        if lat_index(a, 2048) == lat_index(b, 2048):
            problems.append(
                "ld_a and ld_b share a LAT index with 2048 entries"
            )
        print(
            "ld_a %#x ld_b %#x: index (pc >> 1) & 1023 = %d and %d; "
            "& 2047 = %d and %d"
            % (
                a,
                b,
                lat_index(a),
                lat_index(b),
                lat_index(a, 2048),
                lat_index(b, 2048),
            )
        )
    # the buffers the program loads from (the compiler drops the others)
    buffers = {
        "cold": ["bufA", "bufB"],
        "alias": ["bufA", "bufB"],
        "conf": ["bufA", "bufB"],
        "squash": ["bufA"],
        "fault": [],
    }[mode]
    for need in buffers:
        if need not in syms:
            problems.append("symbol %s not found" % need)
    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 1
    with open(pcs_out, "w") as f:
        for n, pc in want.items():
            f.write("%s 0x%x\n" % (n, pc))
        for need in buffers:
            f.write("%s 0x%x\n" % (need, syms[need]))
    print("PASS: the loads of the %s program are as intended" % mode)
    return 0


# ------------------------------------------------------------- run readers


def read_pcs(directory):
    pcs = {}
    with open(os.path.join(directory, "pcs.txt")) as f:
        for line in f:
            n, v = line.split()
            pcs[n] = int(v, 16)
    return pcs


def cpu_params(out, name):
    params = {}
    section_name = None
    try:
        with open(os.path.join(out, name, "config.ini")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section_name = line
                elif section_name == "[system.cpu]" and "=" in line:
                    k, v = line.split("=", 1)
                    params[k] = v
    except OSError:
        return None
    return params


def program_output(out, name):
    try:
        with open(os.path.join(out, name + ".out"), errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def exit_status(out, name):
    try:
        with open(os.path.join(out, name + ".rc")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def checksum_of(out, name):
    m = re.search(r"^checksum (\d+)\s*$", program_output(out, name), re.M)
    return int(m.group(1)) if m else None


def astra_counters(stats):
    return {
        k.split("cpu.astra.", 1)[1]: int(float(v))
        for k, v in stats.items()
        if "cpu.astra." in k
    }


class Run:
    pass


def load_run(out, name, want_astra, entries=ENTRIES, expect_rc=0, trace=True):
    """Reads a run; checks its exit status, files, statistics and ASTRA
    configuration.  Returns a Run, or None if it cannot be used"""
    r = Run()
    r.name = name
    r.rc = exit_status(out, name)
    check(
        r.rc == expect_rc,
        "%s: gem5 exit status %s (got %s)" % (name, expect_rc, r.rc),
    )
    try:
        r.stats = ca.read_stats(out, name)
    except OSError:
        r.stats = None
    ok = r.rc == expect_rc and r.stats is not None
    check(r.stats is not None, "%s: stats.txt can be read" % name)
    r.params = cpu_params(out, name)
    check(r.params is not None, "%s: [system.cpu] parameters found" % name)
    if r.params is None or r.stats is None:
        return None
    check(
        r.params.get("enableAstra") == ("true" if want_astra else "false"),
        "%s: enableAstra is %s" % (name, want_astra),
    )
    if want_astra:
        check(
            r.params.get("astraTableEntries") == str(entries),
            "%s: astraTableEntries is %d" % (name, entries),
        )
    check(
        r.params.get("threadPolicy") == "SingleThreaded"
        and r.params.get("executeMaxAccessesInMemory") == "16"
        and r.params.get("executeLSQTransfersQueueSize") == "16",
        "%s: research baseline settings (SingleThreaded, 16 and 16)" % name,
    )
    has_astra = any("cpu.astra." in k for k in r.stats)
    check(
        has_astra == want_astra,
        "%s: astra.* statistics %s"
        % (name, "present" if want_astra else "absent"),
    )
    r.astra = astra_counters(r.stats)
    r.insts = int(float(r.stats["simInsts"]))
    r.cycles = int(float(r.stats["system.cpu.numCycles"]))
    r.checksum = checksum_of(out, name)
    if not trace:
        r.tr = None
        return r if ok else None
    r.tr = ca.read_trace(os.path.join(out, name, "trace.txt"))
    check(r.tr is not None, "%s: trace can be read" % name)
    if r.tr is None:
        return None
    check(
        not r.tr["problems"],
        "%s: the trace has only instruction and ASTRA lines (%d others)"
        % (name, len(r.tr["problems"])),
    )
    return r if ok else None


def lifecycle(run):
    s = run.astra
    check(
        s["lookups"] + s["carryIn"]
        == s["trainings"]
        + s["squashedLoads"]
        + s["faultedLoads"]
        + s["baseUnavailableLoads"]
        + s["inFlight"],
        "%s: lookups + carry-in = trainings + squashed + faulted + "
        "unavailable + in flight" % run.name,
    )
    check(
        s["inFlight"] == 0 and s["carryIn"] == 0,
        "%s: no load in flight at the end, none carried in" % run.name,
    )


def events_of(run, kind):
    return [e for e in run.tr["events"] if e["kind"] == kind]


def common_pair(d, s, want_checksum, label):
    """Checks shared by a D (ASTRA off) and S (ASTRA on) pair"""
    check(
        d.checksum == want_checksum and s.checksum == want_checksum,
        "%s: checksum is %s in D and S (got %s and %s)"
        % (label, want_checksum, d.checksum, s.checksum),
    )
    check(
        d.insts == s.insts,
        "%s: same committed instruction count (%d and %d)"
        % (label, d.insts, s.insts),
    )
    check(
        d.tr["insns"] == s.tr["insns"],
        "%s: identical committed instruction trace in D and S" % label,
    )
    nd = {k: v for k, v in d.stats.items() if "cpu.astra." not in k}
    ns = {k: v for k, v in s.stats.items() if "cpu.astra." not in k}
    check(nd == ns, "%s: identical statistics apart from astra.*" % label)
    check(
        not events_of(d, "lookup"),
        "%s: D logs no ASTRA event" % label,
    )


def replay_ok(run, entries=ENTRIES):
    problems, sm = ca.replay(run.tr["events"], entries)
    check(
        not problems,
        "%s: every lookup and update agrees with the model (%d problems)%s"
        % (run.name, len(problems), (": " + problems[0]) if problems else ""),
    )
    return sm


def committed_loads(run):
    return [(l["pc"], l["ea"] - l["imm"]) for l in run.tr["loads"]]


# ------------------------------------------------------------------- cold


def cold_main(out):
    pcs = read_pcs(out)
    a, b = pcs["cold_a"], pcs["cold_b"]
    ba, bb = pcs["bufA"], pcs["bufB"]
    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    if d is None or s is None:
        return finish()
    common_pair(d, s, buf_a(8) + buf_b(8) + buf_a(24) + buf_b(24), "cold")
    lifecycle(s)

    section("The program executes the loads it is written to")
    want = [(a, ba + 64), (b, bb + 64), (a, ba + 192), (b, bb + 192)]
    check(
        committed_loads(s) == want,
        "committed loads: cold_a, cold_b, cold_a, cold_b at the buffer "
        "addresses of the program (%s)"
        % [(hex(p), hex(x)) for p, x in committed_loads(s)],
    )
    sm = replay_ok(s)
    lk = events_of(s, "lookup")
    up = events_of(s, "update")
    check(len(lk) == 4 and len(up) == 4, "4 lookups and 4 updates, no squash")
    if len(lk) != 4 or len(up) != 4:
        return finish()

    section("First use: the policy of astra.hh (an implementation choice)")
    check(
        (lk[0]["pc"], lk[0]["lat_base"], lk[0]["lat_idx"], lk[0]["ss"])
        == (a, 0, 0, 0)
        and lk[0]["pred"] == 0,
        "the first load of the program reads LAT base 0, stride index 0 and "
        "SS 0 and predicts 0 (read %#x %d %d, predicted %#x)"
        % (lk[0]["lat_base"], lk[0]["lat_idx"], lk[0]["ss"], lk[0]["pred"]),
    )
    stride_a = ca.s16(ba + 64)
    check(stride_a != 0, "premise: the first stride is not 0 (%d)" % stride_a)
    check(
        (up[0]["prev_base"], up[0]["prev_idx"]) == (0, 0)
        and up[0]["stride"] == stride_a
        and up[0]["new_idx"] == ss_index(stride_a)
        and not up[0]["correct"],
        "its update takes the stride from base 0: the 16 bit truncation of "
        "%#x is %d, the new stride index %d, and the prediction 0 was wrong"
        % (ba + 64, up[0]["stride"], up[0]["new_idx"]),
    )
    check(
        (lk[1]["pc"], lk[1]["lat_base"], lk[1]["lat_idx"]) == (b, 0, 0),
        "the first use of cold_b, whose LAT entry was never written, reads "
        "LAT base 0 and stride index 0 (its own entry is cold)",
    )
    check(
        lk[1]["ss"] == stride_a and lk[1]["pred"] == stride_a,
        "but SS[0], written by the first update (a cold load's stride index "
        "is 0), is what it reads: SS %d, predicted %#x"
        % (lk[1]["ss"], lk[1]["pred"]),
    )
    check(
        lat_index(a) != lat_index(b),
        "premise: cold_a and cold_b have different LAT indexes (%d, %d)"
        % (lat_index(a), lat_index(b)),
    )
    check(
        (lk[2]["lat_base"], lk[2]["lat_idx"]) == (ba + 64, ss_index(stride_a)),
        "the second use of cold_a reads what the first update wrote: LAT base "
        "%#x, stride index %d" % (lk[2]["lat_base"], lk[2]["lat_idx"]),
    )
    report(
        "this cold start (LAT {0, 0} and SS all 0, no valid bit) is the "
        "implementation's choice: the paper does not specify one"
    )
    return finish()


# ------------------------------------------------------------------ alias


def alias_main(out):
    pcs = read_pcs(out)
    a, b = pcs["ld_a"], pcs["ld_b"]
    ba, bb = pcs["bufA"], pcs["bufB"]
    section("The two loads alias (from the formula, on the assembled PCs)")
    check(a != b, "ld_a %#x and ld_b %#x are different loads" % (a, b))
    check(
        lat_index(a) == lat_index(b),
        "(pc >> 1) & 1023 is %d for ld_a and %d for ld_b"
        % (lat_index(a), lat_index(b)),
    )
    check(
        lat_index(a, 2048) != lat_index(b, 2048),
        "(pc >> 1) & 2047 is %d and %d: not aliased with 2048 entries"
        % (lat_index(a, 2048), lat_index(b, 2048)),
    )

    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    s2 = load_run(out, "S2K", True, 2048)
    if d is None or s is None or s2 is None:
        return finish()
    total = (
        sum(buf_a(8 * i) for i in range(ALIAS_TRAIN))
        + buf_b(0)
        + buf_a(8 * ALIAS_TRAIN)
        + buf_b(64)
    )
    common_pair(d, s, total, "alias")
    check(
        s2.checksum == total and s2.tr["insns"] == d.tr["insns"],
        "S2K: same checksum and committed trace",
    )
    lifecycle(s)
    lifecycle(s2)

    section("The program executes the loads it is written to")
    want = [(a, ba + 64 * i) for i in range(ALIAS_TRAIN)] + [
        (b, bb),
        (a, ba + 64 * ALIAS_TRAIN),
        (b, bb + 512),
    ]
    check(committed_loads(s) == want, "committed loads and bases as written")
    replay_ok(s)
    replay_ok(s2, 2048)
    lk = events_of(s, "lookup")
    up = events_of(s, "update")
    lk2 = events_of(s2, "lookup")
    check(
        len(lk) == len(want) and len(up) == len(want),
        "%d lookups and updates in S" % len(want),
    )
    if len(lk) != len(want) or len(up) != len(want) or len(lk2) != len(want):
        return finish()

    section("1024 entries: ld_b reads the LAT state that ld_a trained")
    n = ALIAS_TRAIN
    last_a = ba + 64 * (n - 1)
    check(
        up[n - 1]["new_idx"] == ss_index(64)
        and lk[n]["ss"] == 64
        and (lk[n]["pc"], lk[n]["lat_base"], lk[n]["lat_idx"])
        == (b, last_a, ss_index(64)),
        "ld_b's first lookup reads ld_a's last base %#x and stride index %d "
        "(and SS %d), never written by ld_b (read %#x %d %d)"
        % (
            last_a,
            ss_index(64),
            64,
            lk[n]["lat_base"],
            lk[n]["lat_idx"],
            lk[n]["ss"],
        ),
    )
    check(
        lk[n]["pred"] == last_a + 64,
        "so it predicts ld_a's base plus the trained stride: %#x (read %#x)"
        % (last_a + 64, lk[n]["pred"]),
    )
    stride_b = ca.s16(bb - last_a)
    check(
        (up[n]["prev_base"], up[n]["prev_idx"], up[n]["stride"])
        == (last_a, ss_index(64), stride_b),
        "ld_b's update computes its stride against ld_a's base: prev base "
        "%#x, stride %d" % (up[n]["prev_base"], up[n]["stride"]),
    )
    check(
        (lk[n + 1]["pc"], lk[n + 1]["lat_base"], lk[n + 1]["lat_idx"])
        == (a, bb, ss_index(stride_b)),
        "ld_a's next lookup reads ld_b's base %#x and stride index %d "
        "(read %#x %d)"
        % (
            bb,
            ss_index(stride_b),
            lk[n + 1]["lat_base"],
            lk[n + 1]["lat_idx"],
        ),
    )

    section("2048 entries (same program): no shared state")
    check(
        (lk2[n]["pc"], lk2[n]["lat_base"], lk2[n]["lat_idx"]) == (b, 0, 0),
        "ld_b's first lookup reads its own cold entry: LAT base 0, stride "
        "index 0 (read %#x %d)" % (lk2[n]["lat_base"], lk2[n]["lat_idx"]),
    )
    check(
        lk2[n + 1]["lat_base"] == last_a,
        "ld_a's next lookup still reads its own last base %#x (read %#x)"
        % (last_a, lk2[n + 1]["lat_base"]),
    )
    check(
        lk[n]["lat_base"] != lk2[n]["lat_base"],
        "the same lookup differs between the table sizes: the sharing is "
        "the table index, not the program",
    )
    return finish()


# ------------------------------------------------------------------- conf


def conf_run(out, tag, ystride, label):
    pcs = read_pcs(os.path.join(out, "bin_" + tag))
    x, y = pcs["ld_x"], pcs["ld_y"]
    ba, bb = pcs["bufA"], pcs["bufB"]
    d = load_run(out, tag + "D", False)
    s = load_run(out, tag + "S", True)
    if d is None or s is None:
        return None
    total = sum(buf_a(8 * i) + buf_b(ystride // 8 * i) for i in range(CONF_N))
    common_pair(d, s, total, label)
    lifecycle(s)
    want = []
    for i in range(CONF_N):
        want.append((x, ba + 64 * i))
        want.append((y, bb + ystride * i))
    check(committed_loads(s) == want, "%s: committed loads as written" % label)
    replay_ok(s)
    lk = events_of(s, "lookup")
    up = events_of(s, "update")
    check(
        len(lk) == len(want) and len(up) == len(want),
        "%s: %d lookups and updates, no squashed load" % (label, len(want)),
    )
    if len(lk) != len(want) or len(up) != len(want):
        return None
    # the pipeline lag is removed: every lookup comes after the update of
    # the previous load in the log
    pos = {}
    for n, e in enumerate(s.tr["events"]):
        if e["kind"] in ("lookup", "update"):
            pos[(e["kind"], e["key"])] = n
    lagfree = all(
        pos[("lookup", lk[i]["key"])] > pos[("update", up[i - 1]["key"])]
        for i in range(1, len(want))
    )
    check(
        lagfree,
        "%s: premise: every load is looked up after the previous load "
        "retired (no pipeline lag)" % label,
    )
    seq = [(("p", i), pc, base) for i, (pc, base) in enumerate(want)]
    ideal = [ok for _, ok in ca.ideal_run(ENTRIES, seq)]
    real = [u["correct"] for u in up]
    if lagfree:
        check(
            real == ideal,
            "%s: each load is predicted correctly exactly when the "
            "lag-free model says so" % label,
        )
    res = {
        "x": sum(1 for i in range(0, len(want), 2) if real[i]),
        "y": sum(1 for i in range(1, len(want), 2) if real[i]),
        "idx": (up[2]["new_idx"], up[3]["new_idx"]),
        "lagfree": lagfree,
    }
    report(
        "%s: ld_x predicted correctly %d of %d, ld_y %d of %d, stride "
        "indexes %s" % (label, res["x"], CONF_N, res["y"], CONF_N, res["idx"])
    )
    return res


def conf_main(out):
    section("Destructive SS aliasing (strides 64 and 1088)")
    al = conf_run(out, "alias", 1088, "alias")
    section("Control (strides 64 and 128: different SS entries)")
    ct = conf_run(out, "ctrl", 128, "ctrl")
    if al is None or ct is None:
        return finish()
    section("The difference is the shared SS entry")
    check(
        al["idx"] == (64, 64) and ct["idx"] == (64, 128),
        "stride indexes: 64 and 64 (1088 mod 1024 = 64) against 64 and 128",
    )
    check(
        al["x"] <= 2 and al["y"] <= 2,
        "with the shared SS entry and no lag, at most 2 of %d predictions "
        "of each load are correct (%d and %d): each retirement overwrites "
        "the stride the other load will read" % (CONF_N, al["x"], al["y"]),
    )
    check(
        ct["x"] >= CONF_N - 4 and ct["y"] >= CONF_N - 4,
        "without it, nearly every prediction is correct (%d and %d of %d)"
        % (ct["x"], ct["y"], CONF_N),
    )
    return finish()


# ----------------------------------------------------------------- squash


def squash_main(out):
    pcs = read_pcs(out)
    wrong = [pcs["sq_wrong%d" % i] for i in range(SQ_BLOCKS)]
    real = [pcs["sq_real%d" % i] for i in range(SQ_BLOCKS)]
    ba = pcs["bufA"]
    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    if d is None or s is None:
        return finish()
    total = sum(buf_a(8 + 8 * k) for k in range(SQ_BLOCKS))
    common_pair(d, s, total, "squash")
    lifecycle(s)
    replay_ok(s)

    section("The wrong-path loads never execute")
    want = [(real[k], ba + 64 + 64 * k) for k in range(SQ_BLOCKS)]
    check(
        committed_loads(s) == want,
        "the committed loads are exactly the %d real ones at the bases "
        "the program computes" % SQ_BLOCKS,
    )
    check(
        not any(l["pc"] in wrong for l in s.tr["loads"]),
        "no sq_wrong load is in the committed trace",
    )

    section("They are looked up, squashed, and do not train")
    ev = s.tr["events"]
    lookups = {e["key"]: e for e in ev if e["kind"] == "lookup"}
    outcomes = {e["key"]: e for e in ev if e["kind"] == "outcome"}
    updated = {e["key"] for e in ev if e["kind"] == "update"}
    squashed = [k for k, e in outcomes.items() if e["outcome"] == "squashed"]
    check(
        len(squashed) >= 1, "at least one squashed load (%d)" % len(squashed)
    )
    check(
        s.astra["squashedLoads"] == len(squashed),
        "squashedLoads (%d) = squashed outcome lines (%d)"
        % (s.astra["squashedLoads"], len(squashed)),
    )
    check(
        all(k in lookups for k in squashed),
        "every squashed load was looked up first",
    )
    check(
        not any(k in updated for k in squashed),
        "no squashed load has an update (none trained)",
    )
    sq_wrong = [k for k in squashed if outcomes[k]["pc"] in wrong]
    check(
        len(sq_wrong) >= 1,
        "a load that never executes architecturally was looked up and "
        "squashed (%d of the squashed loads are at sq_wrong PCs)"
        % len(sq_wrong),
    )
    for i, pc in enumerate(wrong):
        report(
            "sq_wrong%d (%#x): squashed %d time(s)"
            % (i, pc, sum(1 for k in squashed if outcomes[k]["pc"] == pc))
        )
    check(
        all(lookups[k]["pc"] not in wrong for k in updated),
        "no sq_wrong load updated the tables",
    )
    check(
        len(updated) == SQ_BLOCKS
        and {lookups[k]["pc"] for k in updated} == set(real),
        "the only updates are the %d real loads" % SQ_BLOCKS,
    )
    check(
        s.astra["trainings"] == SQ_BLOCKS,
        "trainings is %d (got %d)" % (SQ_BLOCKS, s.astra["trainings"]),
    )
    return finish()


# ------------------------------------------------------------------ fault


def fault_main(out):
    pcs = read_pcs(out)
    ft = pcs["ft_load"]
    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    if d is None or s is None:
        return finish()
    common_pair(d, s, 1, "fault")
    lifecycle(s)
    replay_ok(s)
    report(
        "gem5 exits with status 0: the faulting page is in a mapping "
        "(mmap), so the SE page fault handler allocates it and the load is "
        "executed again.  A fault it cannot resolve is a panic with no "
        "statistics; that is not tested"
    )

    section("The faulting loads reach the fault path and do not train")
    ev = s.tr["events"]
    lookups = {e["key"]: e for e in ev if e["kind"] == "lookup"}
    bases = {e["key"]: e["base"] for e in ev if e["kind"] == "base"}
    outcomes = {e["key"]: e for e in ev if e["kind"] == "outcome"}
    updates = [e for e in ev if e["kind"] == "update"]
    faulted = [k for k, e in outcomes.items() if e["outcome"] == "faulted"]
    check(
        len(faulted) == FAULT_PAGES and s.astra["faultedLoads"] == FAULT_PAGES,
        "%d faulted loads in the log and in faultedLoads (%d, %d)"
        % (FAULT_PAGES, len(faulted), s.astra["faultedLoads"]),
    )
    check(
        sorted(bases[k] for k in faulted)
        == [FAULT_ADDR, FAULT_ADDR + FAULT_PAGE],
        "they are the first loads from each page (bases %s)"
        % [hex(bases[k]) for k in sorted(faulted, key=lambda k: bases[k])],
    )
    check(
        all(lookups[k]["pc"] == ft for k in faulted),
        "at the PC of ft_load",
    )
    check(
        not any(u["key"] in faulted for u in updates),
        "no faulted load has an update (none trained)",
    )
    # in the raw trace: the outcome line is followed by the faulting
    # instruction, printed with its address and without a result
    lines = []
    with open(os.path.join(out, "S", "trace.txt"), errors="replace") as f:
        lines = f.read().splitlines()
    good = 0
    for n, line in enumerate(lines):
        m = ca.OUTCOME_RE.search(line)
        if not m or m.group(4) != "faulted":
            continue
        nxt = next((l for l in lines[n + 1 :] if "ASTRA" not in l), "")
        if ("0x%x" % ft) in nxt and " A=" in nxt and " D=" not in nxt:
            good += 1
    check(
        good == FAULT_PAGES,
        "each faulted outcome is followed by the faulting ld, printed with "
        "its address and no result (%d of %d)" % (good, FAULT_PAGES),
    )

    section("Each faulting load is then executed again and trains")
    check(
        s.astra["trainings"] == 3 and len(updates) == 3,
        "three loads completed and trained (the two re-executions and the "
        "load that did not fault): trainings %d" % s.astra["trainings"],
    )
    check(
        [u["base"] for u in updates]
        == [FAULT_ADDR, FAULT_ADDR + FAULT_PAGE, FAULT_ADDR + 8],
        "with bases %s" % [hex(u["base"]) for u in updates],
    )
    check(
        s.astra["lookups"] == 5
        and s.astra["squashedLoads"] == 0
        and s.astra["baseUnavailableLoads"] == 0,
        "5 lookups (2 faulted + 3 completed), nothing squashed or "
        "unavailable",
    )
    check(
        updates[0]["stride"] == 0 and FAULT_ADDR & 0xFFFF == 0,
        "first update: the stride 0x%x - 0 truncated to 16 bits is %d "
        "(an overflow of the 16 bit stride, as astra.hh says it is "
        "truncated; recorded, not judged)"
        % (FAULT_ADDR, updates[0]["stride"]),
    )
    return finish()


# --------------------------------------------------------- invalid-config


def expect_text(out, name, text):
    return text in program_output(out, name)


def never_started(out, name):
    """gem5 stopped before simulating: no "Beginning simulation!" and no
    program output (it leaves an empty stats.txt behind)"""
    text = program_output(out, name)
    return "Beginning simulation!" not in text and "checksum" not in text


def invalid_main(out):
    bad_cases = [
        ("cli_0", 2, "argument --astra-entries: not a power of two: '0'"),
        ("cli_1", 2, "--astra-entries must be in [2, 65536]"),
        ("cli_3", 2, "argument --astra-entries: not a power of two: '3'"),
        (
            "cli_65537",
            2,
            "argument --astra-entries: not a power of two: '65537'",
        ),
        (
            "cli_disabled_3",
            2,
            "argument --astra-entries: not a power of two: '3'",
        ),
        ("cli_disabled_2", 2, "--astra-entries needs --astra"),
    ]
    section("The baseline's command line rejects invalid sizes")
    for name, rc, text in bad_cases:
        got = exit_status(out, name)
        check(
            got == rc,
            "%s: exit status %d (got %s)" % (name, rc, got),
        )
        check(
            expect_text(out, name, "error:") and expect_text(out, name, text),
            "%s: the error says %r" % (name, text),
        )
        check(
            never_started(out, name),
            "%s: gem5 stopped before running the program" % name,
        )

    section("Valid sizes are accepted (command line)")
    for name, entries in (
        ("cli_ok_2", 2),
        ("cli_ok_1024", 1024),
        ("cli_ok_65536", 65536),
        ("cli_default", 1024),
    ):
        r = load_run(out, name, True, entries, trace=False)
        if r is not None:
            check(
                r.checksum == 592 and r.astra["trainings"] == 4,
                "%s: checksum 592 and 4 trainings (%s, %s)"
                % (name, r.checksum, r.astra["trainings"]),
            )

    section(
        "The C++ check of the AstraPredictor constructor (parameters set directly)"
    )
    for n in (0, 1, 3, 65537):
        name = "cpp_%d" % n
        got = exit_status(out, name)
        check(got == 1, "%s: exit status 1 (got %s)" % (name, got))
        text = (
            "astraTableEntries must be a power of two in [2, 65536] (got %d)"
            % n
        )
        check(
            expect_text(out, name, text)
            and len(re.findall(r"\bfatal\b", program_output(out, name))) >= 1,
            "%s: fatal: %s" % (name, text),
        )
        check(
            never_started(out, name),
            "%s: gem5 stopped before running the program" % name,
        )
    r = load_run(out, "cpp_ok_2", True, 2, trace=False)
    if r is not None:
        check(r.checksum == 592, "cpp_ok_2: checksum 592")

    section("ASTRA disabled with an invalid size")
    name = "cpp_disabled_3"
    got = exit_status(out, name)
    check(got == 0, "%s: exit status 0 (got %s)" % (name, got))
    params = cpu_params(out, name)
    check(
        params is not None
        and params.get("enableAstra") == "false"
        and params.get("astraTableEntries") == "3",
        "%s: astraTableEntries=3 is set but enableAstra is false" % name,
    )
    try:
        st = ca.read_stats(out, name)
    except OSError:
        st = {}
    check(
        st and not any("cpu.astra." in k for k in st),
        "%s: the size is ignored: no predictor, no astra.* statistics" % name,
    )
    check(
        checksum_of(out, name) == 592,
        "%s: the program ran and printed its checksum" % name,
    )
    report(
        "policy (as for LVP's lvpTableEntries, checked only with enableLVP): "
        "astraTableEntries is checked only when enableAstra is true; with it "
        "false any value is accepted and ignored.  The baseline's command "
        "line is stricter: it checks --astra-entries even without --astra"
    )
    return finish()


# ------------------------------------------------------------ lvp-coexist


def lvp_counters(stats):
    return {
        k.split("cpu.lvp.", 1)[1]: int(float(v))
        for k, v in stats.items()
        if "cpu.lvp." in k
    }


def coexist_main(out):
    pcs = {}
    with open(os.path.join(out, "load_pcs.txt")) as f:
        for line in f:
            t = line.split()
            if len(t) == 2:
                pcs[t[0]] = int(t[1], 16)
    producer, consumer = pcs["producer"], pcs["consumer"]
    section("Preflight and configuration")
    runs = {}
    for name, astra in (("L", False), ("LA", True), ("LAL", True)):
        runs[name] = load_run(out, name, astra)
    if any(r is None for r in runs.values()):
        return finish()
    for name, r in runs.items():
        check(
            r.params.get("enableLVP") == "true"
            and r.params.get("lvpSpecAddr") == "true",
            "%s: LVP address speculation is on" % name,
        )
        check(
            r.checksum == PHASE_CHECKSUM,
            "%s: checksum %d (got %s)" % (name, PHASE_CHECKSUM, r.checksum),
        )
    L, LA, LAL = runs["L"], runs["LA"], runs["LAL"]

    section("ASTRA does not change the execution")
    check(
        L.insts == LA.insts == LAL.insts,
        "same committed instruction count (%d, %d, %d)"
        % (L.insts, LA.insts, LAL.insts),
    )
    check(
        L.tr["insns"] == LA.tr["insns"] == LAL.tr["insns"],
        "identical committed instruction traces",
    )
    nl = {k: v for k, v in L.stats.items() if "cpu.astra." not in k}
    nla = {k: v for k, v in LA.stats.items() if "cpu.astra." not in k}
    nlal = {k: v for k, v in LAL.stats.items() if "cpu.astra." not in k}
    check(
        nl == nla == nlal,
        "identical statistics apart from astra.*, cycles included (%d, %d, "
        "%d)" % (L.cycles, LA.cycles, LAL.cycles),
    )
    check(
        lvp_counters(L.stats)
        == lvp_counters(LA.stats)
        == lvp_counters(LAL.stats),
        "identical LVP counters",
    )
    lv = lvp_counters(L.stats)
    check(
        lv["specLoadsCommitted"] > 0 and lv["mispredictSquashes"] > 0,
        "the LVP path was exercised: %d speculative loads committed, %d "
        "recovery redirects (lookups %d, confident %d, correct %d, "
        "incorrect %d)"
        % (
            lv["specLoadsCommitted"],
            lv["mispredictSquashes"],
            lv["lookups"],
            lv["confidentPredictions"],
            lv["correct"],
            lv["incorrect"],
        ),
    )
    check(
        L.astra == {} and LA.astra == LAL.astra,
        "astra.* statistics only with ASTRA, and equal in LA and LAL",
    )

    section("ASTRA with the LVP speculation")
    lifecycle(LAL)
    replay_ok(LAL)
    a = LAL.astra
    ev = LAL.tr["events"]
    outcomes = [e for e in ev if e["kind"] == "outcome"]
    unavailable = [e for e in outcomes if e["outcome"] == "unavailable"]
    check(
        a["baseUnavailableLoads"] == len(unavailable),
        "baseUnavailableLoads (%d) = unavailable outcome lines (%d)"
        % (a["baseUnavailableLoads"], len(unavailable)),
    )
    check(
        all(e["pc"] == consumer for e in unavailable),
        "every unavailable load is the LVP consumer (pc %#x): its base "
        "register held a predicted value when its address was computed, "
        "so ASTRA could not read the real base (astraCaptureBase skips "
        "loads with lvpSpecActive)" % consumer,
    )
    check(
        a["baseUnavailableLoads"] == lv["specLoadsCommitted"],
        "unavailable (%d) = speculative loads that committed (%d)"
        % (a["baseUnavailableLoads"], lv["specLoadsCommitted"]),
    )
    squashed = [e for e in outcomes if e["outcome"] == "squashed"]
    sq_pcs = {e["pc"] for e in squashed}
    report(
        "ASTRA: lookups %d, trainings %d, squashed %d (at pcs %s), "
        "unavailable %d, correct %d, incorrect %d; LVP: specLoadsSquashed %d"
        % (
            a["lookups"],
            a["trainings"],
            a["squashedLoads"],
            sorted(hex(p) for p in sq_pcs),
            a["baseUnavailableLoads"],
            a["baseCorrect"],
            a["baseIncorrect"],
            lv["specLoadsSquashed"],
        )
    )
    check(
        a["trainings"] + a["baseUnavailableLoads"] + a["squashedLoads"]
        == a["lookups"],
        "each lookup is a training, an unavailable load or a squashed load",
    )
    return finish()


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "asm":
        if sys.argv[2] not in EXPECTED_LOADS:
            print(__doc__)
            return 2
        return asm_main(*sys.argv[2:])
    modes = {
        "cold": cold_main,
        "alias": alias_main,
        "conf": conf_main,
        "squash": squash_main,
        "fault": fault_main,
        "invalid-config": invalid_main,
        "lvp-coexist": coexist_main,
    }
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        print(__doc__)
        return 2
    return modes[sys.argv[1]](sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
