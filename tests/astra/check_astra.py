#!/usr/bin/env python3
"""Checks the outputs of run_astra.sh: the ASTRA shadow predictor.
Usage: check_astra.py OUT

OUT holds, for each run name D (ASTRA disabled), S (ASTRA in shadow mode) and
SL (S again, with the ASTRA log in the trace), the directory OUT/<name> that
gem5 wrote and OUT/<name>.out and OUT/<name>.rc with its output and exit
status, and, from the assembly check, OUT/load_pcs.txt and OUT/symbols.txt.
Every run has LVP disabled, so ASTRA is the only predictor.

The checks, in groups:
  - preflight: every run completed and wrote its outputs; nothing is
    compared until all do;
  - architecture: ASTRA changes neither the checksum, the committed
    instruction trace nor any non-ASTRA statistic, and a disabled ASTRA
    logs and counts nothing;
  - the load inventory: the committed loads are exactly the ones that
    astra.c is written to execute, at the addresses an independent model of
    the program says;
  - the ASTRA log, replayed against a golden model of the algorithm: every
    lookup (prediction and the table state it read) and every retirement
    update agrees with the model, updates are in retirement order and only
    for retired loads, and the statistics agree with the log;
  - the recovered base address: for every retired load, the base the
    predictor was trained with is the load's effective address minus its
    sign extended immediate, both read from the committed trace;
  - what each section of the program demonstrates, from the same model run
    in program order with no pipeline lag.

What is and is not from the paper (HPCA 2025, Ravenel et al.).  Paper: the
structure (LAT indexed by the load PC, holding the last address and a
stride that indexes the untagged SS table, which holds a 16 bit stride), the
prediction LAT address + SS[stride], the update at retirement, no tags and
no confidence, Table II's "16+n" bits of stride per entry.  NOT specified by
the paper, so inferences made by this prototype and mirrored by AstraModel
below: the PC to LAT index ((pc >> 1) & mask), the stride to SS index (the
low n bits of the 16 bit stride), which SS entry the update writes (the one
the load's previous stride index points to), the cold start (LAT and SS all
zero, so the first prediction is 0), wrap to 16 bits of a stride that does
not fit, and prediction of the base register's value rather than of the
effective address.  Any expectation below that depends on one of these is a
property of this model, not a guarantee of the paper.
"""

import math
import os
import re
import sys

MASK64 = (1 << 64) - 1
ENTRIES = 1024  # astraTableEntries used by run_astra.sh

NAMES = ["D", "S", "SL"]

# astra.c: iterations per section, and the sections' parameters
N = 64
STRIDE = 64
STRIDE_BIG = 192
CHG_RUN = 8
PAIR_SWITCH = 24
CONFLICT_Y_STRIDE = 1088
BUF_BYTES = 131072

SECTIONS = ["const", "pos", "neg", "chg", "imm", "pair", "conf"]

fails = []


def check(cond, msg, failures=None):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        (fails if failures is None else failures).append(msg)


def report(msg):
    print("INFO  " + msg)


def section(title):
    print()
    print("== " + title)


# ---------------------------------------------------------------- the model


def s16(x):
    """The signed value of the low 16 bits of x"""
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


class AstraModel:
    """ASTRA, as documented in src/cpu/minor/astra.hh"""

    def __init__(self, entries):
        self.mask = entries - 1
        self.lat_base = [0] * entries
        self.lat_idx = [0] * entries
        self.ss = [0] * entries

    def lat_index(self, pc):
        return (pc >> 1) & self.mask

    def lookup(self, pc):
        i = self.lat_index(pc)
        idx = self.lat_idx[i]
        stride = self.ss[idx]
        return {
            "index": i,
            "lat_base": self.lat_base[i],
            "lat_idx": idx,
            "ss": stride,
            "pred": (self.lat_base[i] + stride) & MASK64,
        }

    def retire(self, pc, base, pred):
        i = self.lat_index(pc)
        prev_base = self.lat_base[i]
        prev_idx = self.lat_idx[i]
        stride = s16((base - prev_base) & MASK64)
        new_idx = (stride & 0xFFFF) & self.mask
        self.ss[prev_idx] = stride
        self.lat_base[i] = base
        self.lat_idx[i] = new_idx
        return {
            "index": i,
            "prev_base": prev_base,
            "prev_idx": prev_idx,
            "stride": stride,
            "new_idx": new_idx,
            "correct": pred == base,
        }


# ------------------------------------------- the program, independently


def emulate(buf_a, buf_b):
    """astra.c executed by hand: the loads in program order as (section,
    slot, base address, immediate), and the checksum.  buf_a and buf_b are
    the buffers' addresses."""
    loads = []
    total = 0
    bufs = {"A": buf_a, "B": buf_b}

    def load(sec, slot, buf, off, imm):
        nonlocal total
        loads.append((sec, slot, bufs[buf] + off, imm))
        k = (off + imm) // 8
        assert 0 <= off + imm < BUF_BYTES and (off + imm) % 8 == 0
        total += k * 7 + 3 if buf == "A" else k * 11 + 5

    off = 4096
    for _ in range(N):
        load("const", 0, "A", off, 0)

    off = 0
    for _ in range(N):
        load("pos", 0, "A", off, 0)
        off += STRIDE

    off = STRIDE * (N - 1)
    for _ in range(N):
        load("neg", 0, "A", off, 0)
        off -= STRIDE

    off = 0
    step = STRIDE
    for i in range(N):
        load("chg", 0, "A", off, 0)
        off += step
        if (i + 1) % CHG_RUN == 0:
            step = STRIDE_BIG if step == STRIDE else STRIDE

    off = 64
    for _ in range(N):
        load("imm", 0, "A", off, 24)
        load("imm", 1, "A", off, -16)
        off += STRIDE

    a = b = 0
    for i in range(N):
        step = STRIDE if i < PAIR_SWITCH else STRIDE_BIG
        load("pair", 0, "A", a, 0)
        load("pair", 1, "B", b, 0)
        a += step
        b += step

    x = y = 0
    for _ in range(N):
        load("conf", 0, "A", x, 0)
        load("conf", 1, "B", y, 0)
        x += STRIDE
        y += CONFLICT_Y_STRIDE

    return loads, total


def ideal_run(entries, seq):
    """The model run in program order with every load retiring before the
    next is looked up (no pipeline lag).  seq is a list of (key, pc, base).
    Returns [(key, predicted correctly)]."""
    model = AstraModel(entries)
    out = []
    for key, pc, base in seq:
        pred = model.lookup(pc)["pred"]
        model.retire(pc, base, pred)
        out.append((key, pred == base))
    return out


# ----------------------------------------------------------- reading outputs


def file_problem(path):
    """Why path can't be used as a text input, or None"""
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


def number_problems(stats, keys):
    bad = []
    for k in keys:
        try:
            v = float(stats[k])
            if not math.isfinite(v) or not v.is_integer():
                raise ValueError(k)
        except (KeyError, ValueError):
            bad.append("%s = %s" % (k, stats.get(k)))
    return bad


def read_cpu_config(out, name):
    """The parameters of [system.cpu] in OUT/<name>/config.ini, or None"""
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


def checksum(out, name):
    try:
        with open(os.path.join(out, name + ".out")) as f:
            for line in f:
                m = re.match(r"checksum (\d+)\s*$", line)
                if m:
                    return int(m.group(1))
    except (OSError, UnicodeDecodeError):
        pass
    return None


def preflight(out):
    """Check that every run completed and wrote what is compared.  Returns
    (statistics of each run, []) or (None, the problems found)"""
    problems = []
    stats = {}

    def need(cond, msg):
        check(cond, msg, problems)

    for n in NAMES:
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
            "config.ini": os.path.join(out, n, "config.ini"),
        }
        usable = True
        for what, path in files.items():
            why = file_problem(path)
            need(
                why is None,
                "%s: %s is a non-empty text file%s"
                % (n, what, "" if why is None else " (%s: %s)" % (path, why)),
            )
            usable = usable and why is None
        if not usable:
            continue
        try:
            stats[n] = read_stats(out, n)
        except (OSError, ValueError) as e:
            need(False, "%s: stats.txt can be parsed (%s)" % (n, e))
            continue
        bad = number_problems(
            stats[n], ["simInsts", "system.cpu.numCycles"]
        ) + number_problems(stats[n], [k for k in stats[n] if ".astra." in k])
        need(
            not bad,
            "%s: statistics are integers%s"
            % (n, ", not: " + "; ".join(bad) if bad else ""),
        )

    for name in ("load_pcs.txt", "symbols.txt"):
        why = file_problem(os.path.join(out, name))
        need(
            why is None,
            "%s is a non-empty text file%s"
            % (name, "" if why is None else " (%s)" % why),
        )

    if problems or set(NAMES) - set(stats):
        return None, problems or ["incomplete statistics"]
    return stats, []


def read_load_pcs(out):
    """{(section, slot): (pc, imm)} from load_pcs.txt, or (None, why)"""
    pcs = {}
    try:
        with open(os.path.join(out, "load_pcs.txt")) as f:
            for line in f:
                t = line.split()
                if not t:
                    continue
                if len(t) != 4:
                    return None, "bad line: %s" % line.strip()
                pcs[(t[0], int(t[1]))] = (int(t[2], 16), int(t[3]))
    except (OSError, ValueError) as e:
        return None, str(e)
    return pcs, "ok"


def read_symbols(out):
    syms = {}
    with open(os.path.join(out, "symbols.txt")) as f:
        for line in f:
            t = line.split()
            if len(t) == 4:
                syms[t[3]] = int(t[0], 16)
    return syms


# Instructions that read memory, as the commit trace names them
LOAD_MNEMONIC = re.compile(
    r"(l[bhwd]u?|fl[wd]|lr\.[wd]|amo\w+\.[wd]|sc\.[wd])$"
)
INSN_RE = re.compile(r"\b0x([0-9a-f]+)\s+:\s+(\S+)")
EA_RE = re.compile(r"\bA=0x([0-9a-f]+)")
DISASM_RE = re.compile(r":\s*\S+\s+\S+,\s*(-?(?:0x)?[0-9a-fA-F]+)\((\w+)\)")
ASTRA_EVENT = re.compile(r"\bASTRA (lookup|base|update|outcome)\b")

LOOKUP_RE = re.compile(
    r"ASTRA lookup tid: (\d+) exec: (\d+) pc: 0x([0-9a-f]+) index: (\d+) "
    r"lat_base: 0x([0-9a-f]+) lat_idx: (\d+) ss: (-?\d+) "
    r"pred: 0x([0-9a-f]+)\s*$"
)
BASE_RE = re.compile(
    r"ASTRA base tid: (\d+) exec: (\d+) pc: 0x([0-9a-f]+) "
    r"base: 0x([0-9a-f]+)\s*$"
)
UPDATE_RE = re.compile(
    r"ASTRA update tid: (\d+) exec: (\d+) pc: 0x([0-9a-f]+) "
    r"base: 0x([0-9a-f]+) pred: 0x([0-9a-f]+) correct: (\d) "
    r"index: (\d+) prev_base: 0x([0-9a-f]+) prev_idx: (\d+) "
    r"stride: (-?\d+) new_idx: (\d+)\s*$"
)
OUTCOME_RE = re.compile(
    r"ASTRA outcome tid: (\d+) exec: (\d+) pc: 0x([0-9a-f]+) "
    r"outcome: (\w+)\s*$"
)


def read_trace(path):
    """What a trace says: the committed instruction lines, the committed
    loads (pc, effective address, immediate as the trace disassembles it),
    the ASTRA events in order, and the lines that are neither (problems).
    None if it can't be read."""
    tr = {"insns": [], "loads": [], "events": [], "problems": []}
    try:
        with open(path) as f:
            for n, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if ASTRA_EVENT.search(line):
                    ev = parse_event(line)
                    if ev is None:
                        tr["problems"].append(
                            "line %d: malformed ASTRA line: %s"
                            % (n, line[:120])
                        )
                    else:
                        tr["events"].append(ev)
                    continue
                m = INSN_RE.search(line)
                if not m:
                    tr["problems"].append(
                        "line %d: not an instruction: %s" % (n, line[:120])
                    )
                    continue
                tr["insns"].append(line)
                if LOAD_MNEMONIC.match(m.group(2)):
                    ea = EA_RE.search(line)
                    d = DISASM_RE.search(line)
                    if not ea or not d:
                        tr["problems"].append(
                            "line %d: load without A= or offset(base): %s"
                            % (n, line[:120])
                        )
                        continue
                    tr["loads"].append(
                        {
                            "pc": int(m.group(1), 16),
                            "ea": int(ea.group(1), 16),
                            "imm": int(d.group(1), 0),
                        }
                    )
    except (OSError, UnicodeDecodeError):
        return None
    return tr


def parse_event(line):
    m = LOOKUP_RE.search(line)
    if m:
        t = m.groups()
        return {
            "kind": "lookup",
            "key": (int(t[0]), int(t[1])),
            "pc": int(t[2], 16),
            "index": int(t[3]),
            "lat_base": int(t[4], 16),
            "lat_idx": int(t[5]),
            "ss": int(t[6]),
            "pred": int(t[7], 16),
        }
    m = BASE_RE.search(line)
    if m:
        t = m.groups()
        return {
            "kind": "base",
            "key": (int(t[0]), int(t[1])),
            "pc": int(t[2], 16),
            "base": int(t[3], 16),
        }
    m = UPDATE_RE.search(line)
    if m:
        t = m.groups()
        return {
            "kind": "update",
            "key": (int(t[0]), int(t[1])),
            "pc": int(t[2], 16),
            "base": int(t[3], 16),
            "pred": int(t[4], 16),
            "correct": t[5] == "1",
            "index": int(t[6]),
            "prev_base": int(t[7], 16),
            "prev_idx": int(t[8]),
            "stride": int(t[9]),
            "new_idx": int(t[10]),
        }
    m = OUTCOME_RE.search(line)
    if m:
        t = m.groups()
        return {
            "kind": "outcome",
            "key": (int(t[0]), int(t[1])),
            "pc": int(t[2], 16),
            "outcome": t[3],
        }
    return None


def replay(events, entries):
    """Replay the ASTRA events, in log order, against the golden model.
    Returns (problems, summary).  A lookup's prediction and the table state
    it read, and a retirement update's stride, indexes and correctness, must
    all be what the model computes at that point of the log."""
    model = AstraModel(entries)
    problems = []
    looked = {}
    last_update_exec = {}
    sm = {
        "lookups": 0,
        "updates": [],
        "outcomes": {"squashed": 0, "faulted": 0, "unavailable": 0},
    }

    def bad(msg):
        problems.append(msg)

    for ev in events:
        key = ev["key"]
        rec = looked.get(key)
        kind = ev["kind"]
        where = "tid %d exec %d pc 0x%x" % (key[0], key[1], ev["pc"])
        if kind == "lookup":
            if rec is not None:
                bad("%s: looked up twice" % where)
                continue
            sm["lookups"] += 1
            want = model.lookup(ev["pc"])
            for f in ("index", "lat_base", "lat_idx", "ss", "pred"):
                if ev[f] != want[f]:
                    bad(
                        "%s: lookup %s is %#x, the model says %#x"
                        % (where, f, ev[f], want[f])
                    )
            looked[key] = {
                "pc": ev["pc"],
                "pred": ev["pred"],
                "base": None,
                "done": None,
            }
            continue
        if rec is None:
            bad("%s: %s without a lookup" % (where, kind))
            continue
        if rec["pc"] != ev["pc"]:
            bad("%s: %s for another pc (0x%x)" % (where, kind, rec["pc"]))
        if rec["done"] is not None:
            bad("%s: %s after %s" % (where, kind, rec["done"]))
            continue
        if kind == "base":
            rec["base"] = ev["base"]
        elif kind == "outcome":
            if ev["outcome"] not in sm["outcomes"]:
                bad("%s: unknown outcome %s" % (where, ev["outcome"]))
                continue
            sm["outcomes"][ev["outcome"]] += 1
            rec["done"] = ev["outcome"]
        else:
            if rec["base"] is None:
                bad(
                    "%s: update for a load whose base was not captured" % where
                )
            elif rec["base"] != ev["base"]:
                bad(
                    "%s: updated with base %#x, captured %#x"
                    % (where, ev["base"], rec["base"])
                )
            if ev["pred"] != rec["pred"]:
                bad("%s: update pred differs from the lookup's" % where)
            if ev["key"][1] <= last_update_exec.get(key[0], 0):
                bad("%s: update out of retirement order" % where)
            last_update_exec[key[0]] = ev["key"][1]
            want = model.retire(ev["pc"], ev["base"], rec["pred"])
            for f in (
                "index",
                "prev_base",
                "prev_idx",
                "stride",
                "new_idx",
                "correct",
            ):
                if ev[f] != want[f]:
                    bad(
                        "%s: update %s is %s, the model says %s"
                        % (where, f, ev[f], want[f])
                    )
            rec["done"] = "update"
            sm["updates"].append(ev)
    sm["live"] = sum(1 for r in looked.values() if r["done"] is None)
    return problems, sm


# ---------------------------------------------------------------------- main


STAT_PREFIX = "system.cpu.astra."
ASTRA_STATS = [
    "lookups",
    "trainings",
    "baseCorrect",
    "baseIncorrect",
    "zeroStrides",
    "squashedLoads",
    "faultedLoads",
    "baseUnavailableLoads",
    "inFlight",
    "carryIn",
]


def astra_stats(stats):
    return {
        k: int(float(stats.get(STAT_PREFIX + k, "nan")))
        for k in ASTRA_STATS
        if STAT_PREFIX + k in stats
    }


def non_astra(stats):
    return {k: v for k, v in stats.items() if ".astra." not in k}


def section_properties(ideal):
    """What each section of astra.c demonstrates, from the model run in
    program order with no pipeline lag.  ideal maps (section, slot) to the
    list of "predicted correctly" flags of that load, in order.  These are
    properties of this model (see the module docstring), checked so that the
    sections can't silently stop exercising what they are written to."""

    def wrong(key):
        return [i for i, ok in enumerate(ideal[key]) if not ok]

    for sec in ("const", "pos", "neg"):
        w = wrong((sec, 0))
        check(
            all(i < 6 for i in w),
            "%s: after the cold start every prediction is correct (wrong at"
            " occurrences %s of %d)" % (sec, w, N),
        )
    for slot in (0, 1):
        w = wrong(("imm", slot))
        check(
            all(i < 6 for i in w),
            "imm slot %d (immediate %s): after the cold start every "
            "prediction is correct (wrong at %s)"
            % (slot, "+24" if slot == 0 else "-16", w),
        )

    w = wrong(("chg", 0))
    check(
        len([i for i in w if i >= 8]) >= 2 * (N // CHG_RUN - 2)
        and len(w) <= N // 2,
        "chg: each change of stride costs mispredictions, and the stride "
        "is predicted between changes (wrong %d of %d)" % (len(w), N),
    )

    w0, w1 = wrong(("pair", 0)), wrong(("pair", 1))
    check(
        len(w1) < len(w0) and len(w1) <= 1,
        "pair: the second PC benefits from the SS entry the first one "
        "updated when the stride changes (constructive aliasing): wrong "
        "%d vs %d of %d" % (len(w1), len(w0), N),
    )

    c0, c1 = ideal[("conf", 0)], ideal[("conf", 1)]
    p_wrong = len(wrong(("pos", 0)))
    check(
        sum(c0) <= N // 10
        and sum(c1) <= N // 10
        and N - sum(c0) > p_wrong + N // 2,
        "conf: strides 64 and 1088 share SS index 64 and overwrite each "
        "other's entry (destructive aliasing): correct %d and %d of %d, "
        "against %d wrong for the same stride 64 alone (pos)"
        % (sum(c0), sum(c1), N, p_wrong),
    )


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = sys.argv[1]

    section("Preflight: every run completed and wrote its outputs")
    stats, problems = preflight(out)
    if stats is None:
        print()
        print(
            "%d preflight check(s) failed: nothing was compared"
            % len(problems)
        )
        return 1

    pcs, why = read_load_pcs(out)
    if pcs is None:
        check(False, "load PCs from the assembly check: " + why)
        print()
        print("the load PCs are unusable: nothing was compared")
        return 1
    syms = read_symbols(out)
    if "bufA" not in syms or "bufB" not in syms:
        check(False, "symbols bufA and bufB in symbols.txt")
        return 1
    loads_expected, expected_sum = emulate(syms["bufA"], syms["bufB"])

    section("Run configuration (LVP is off in every run)")
    for n in NAMES:
        params = read_cpu_config(out, n)
        check(params is not None, "%s: [system.cpu] found in config.ini" % n)
        if params is None:
            continue
        want = {
            "enableLVP": "false",
            "enableAstra": "false" if n == "D" else "true",
        }
        if n != "D":
            want["astraTableEntries"] = str(ENTRIES)
        for key, value in want.items():
            check(
                params.get(key) == value,
                "%s: %s is %s (config.ini says %s)"
                % (n, key, value, params.get(key)),
            )

    section("Architecture: ASTRA must not change anything but its own state")
    for n in NAMES:
        check(
            checksum(out, n) == expected_sum,
            "%s: checksum is %d (computed from the program's structure)"
            % (n, expected_sum),
        )
    traces = {n: read_trace(os.path.join(out, n, "trace.txt")) for n in NAMES}
    for n in NAMES:
        ok = traces[n] is not None
        check(ok, "%s: trace.txt can be read" % n)
        if ok:
            check(
                not traces[n]["problems"],
                "%s: every trace line is an instruction or an ASTRA event "
                "(%d problems%s)"
                % (
                    n,
                    len(traces[n]["problems"]),
                    ""
                    if not traces[n]["problems"]
                    else ": " + traces[n]["problems"][0],
                ),
            )
    if any(traces[n] is None for n in NAMES):
        return 1
    for n in NAMES:
        check(
            len(traces[n]["insns"]) == int(float(stats[n]["simInsts"])),
            "%s: one trace line per committed instruction (%d lines, "
            "simInsts %d)"
            % (n, len(traces[n]["insns"]), int(float(stats[n]["simInsts"]))),
        )
    for n in ("S", "SL"):
        check(
            traces[n]["insns"] == traces["D"]["insns"],
            "D and %s: identical committed instruction trace "
            "(%d instructions)" % (n, len(traces["D"]["insns"])),
        )
        check(
            non_astra(stats["D"]) == non_astra(stats[n]),
            "D and %s: identical statistics apart from astra.*" % n,
        )
    check(
        astra_stats(stats["S"]) == astra_stats(stats["SL"]),
        "S and SL: identical astra.* statistics (the log changes nothing)",
    )
    check(
        not astra_stats(stats["D"]) and not traces["D"]["events"],
        "D: ASTRA disabled has no astra.* statistics and logs no ASTRA event",
    )
    check(
        len(astra_stats(stats["S"])) == len(ASTRA_STATS),
        "S: all %d astra.* statistics are registered" % len(ASTRA_STATS),
    )

    section("The loads that were committed (astra.c's load inventory)")
    pc_of = {k: v[0] for k, v in pcs.items()}
    imm_of_pc = {v[0]: v[1] for v in pcs.values()}
    loads = traces["D"]["loads"]
    want_seq = [
        (pc_of[(sec, slot)], base + imm)
        for sec, slot, base, imm in loads_expected
    ]
    got_seq = [(l["pc"], l["ea"]) for l in loads]
    check(
        len(loads) == len(loads_expected),
        "D: %d committed loads (expected %d)"
        % (len(loads), len(loads_expected)),
    )
    diff = [i for i, (a, b) in enumerate(zip(got_seq, want_seq)) if a != b]
    check(
        got_seq == want_seq,
        "D: the committed loads, in order, are at the PCs and effective "
        "addresses of the independent model of astra.c%s"
        % (
            ""
            if not diff
            else " (first difference at load %d: got pc 0x%x ea 0x%x, "
            "expected pc 0x%x ea 0x%x)"
            % ((diff[0],) + got_seq[diff[0]] + want_seq[diff[0]])
        ),
    )
    check(
        all(l["imm"] == imm_of_pc.get(l["pc"]) for l in loads),
        "D: the immediate in every committed load's disassembly is the "
        "one the assembly check found",
    )
    if got_seq != want_seq:
        print()
        print("the load inventory is wrong: the ASTRA checks are not valid")
        return 1

    section("ASTRA log replayed against the golden model (run SL)")
    problems, sm = replay(traces["SL"]["events"], ENTRIES)
    check(
        not problems,
        "every lookup and update agrees with the model (%d problems%s)"
        % (len(problems), "" if not problems else ": " + problems[0]),
    )
    for p in problems[1:5]:
        report(p)
    ups = sm["updates"]
    sl_loads = traces["SL"]["loads"]
    check(
        [(u["pc"], u["base"]) for u in ups]
        == [(l["pc"], (l["ea"] - l["imm"]) & MASK64) for l in sl_loads],
        "the %d updates are for exactly the committed loads, in retirement "
        "order, trained with the base address = effective address minus the "
        "sign extended immediate of the trace (%d loads with a negative "
        "immediate)" % (len(ups), sum(1 for l in sl_loads if l["imm"] < 0)),
    )
    check(
        sm["lookups"] >= len(ups) and len(ups) == len(loads_expected),
        "%d lookups for %d retired loads (the rest were squashed or are "
        "in flight)" % (sm["lookups"], len(ups)),
    )

    section("Statistics against the log")
    st = astra_stats(stats["SL"])
    terminals = len(ups) + sum(sm["outcomes"].values())
    expect = {
        "lookups": sm["lookups"],
        "trainings": len(ups),
        "baseCorrect": sum(1 for u in ups if u["correct"]),
        "baseIncorrect": sum(1 for u in ups if not u["correct"]),
        "zeroStrides": sum(1 for u in ups if u["stride"] == 0),
        "squashedLoads": sm["outcomes"]["squashed"],
        "faultedLoads": sm["outcomes"]["faulted"],
        "baseUnavailableLoads": sm["outcomes"]["unavailable"],
        "inFlight": sm["lookups"] - terminals,
        "carryIn": 0,
    }
    for k, v in expect.items():
        check(
            st.get(k) == v, "astra.%s is %s (log says %d)" % (k, st.get(k), v)
        )
    check(
        st["lookups"] + st["carryIn"]
        == st["trainings"]
        + st["squashedLoads"]
        + st["faultedLoads"]
        + st["baseUnavailableLoads"]
        + st["inFlight"],
        "every looked up load has exactly one outcome: lookups + carryIn == "
        "trainings + squashed + faulted + unavailable + inFlight",
    )
    check(
        st["baseCorrect"] + st["baseIncorrect"] == st["trainings"],
        "baseCorrect + baseIncorrect == trainings",
    )
    check(
        st["faultedLoads"] == 0 and st["baseUnavailableLoads"] == 0,
        "program-specific: no load faults and none lacks its base address "
        "(no faults in astra.c, and LVP is off)",
    )

    section("What each section demonstrates (model, program order, no lag)")
    seq = [
        ((sec, slot), pc_of[(sec, slot)], base)
        for sec, slot, base, imm in loads_expected
    ]
    ideal = {}
    for key, ok in ideal_run(ENTRIES, seq):
        ideal.setdefault(key, []).append(ok)
    section_properties(ideal)
    for sec in SECTIONS:
        for slot in (0, 1):
            if (sec, slot) in ideal:
                v = ideal[(sec, slot)]
                report(
                    "ideal model, %s slot %d: %d of %d predicted"
                    % (sec, slot, sum(v), len(v))
                )
    by_pc = {}
    for u in ups:
        by_pc.setdefault(u["pc"], []).append(u["correct"])
    for key, pc in sorted(pc_of.items()):
        v = by_pc.get(pc, [])
        report(
            "pipeline run, %s slot %d: %d of %d predicted (pipeline lag "
            "changes this; it is replayed exactly above)"
            % (key[0], key[1], sum(v), len(v))
        )
    report(
        "base predictions correct in the run: %d of %d, zero strides %d"
        % (st["baseCorrect"], st["trainings"], st["zeroStrides"])
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
