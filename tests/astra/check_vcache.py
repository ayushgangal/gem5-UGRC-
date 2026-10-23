#!/usr/bin/env python3
"""Checks the outputs of run_vcache.sh.

Usage:
  check_vcache.py asm MODE VC_S SYMBOLS PCS_OUT
  check_vcache.py MODE OUT

MODE is ops, squash, fault or noninterference.  OUT holds, for each run NAME,
the gem5 output directory OUT/NAME, and OUT/NAME.out and OUT/NAME.rc with its
output and exit status.  D is a run with the shadow V$ off and S the same run
with it on.

The shadow V$ (src/cpu/minor/vcache.hh) is instrumentation: it must change
nothing, and its state is a function of the retired loads and stores.  The
checks are:

  - non-interference: D and S have the same checksum, committed instruction
    count, committed instruction trace and every statistic except vcache.*,
    cycles included;
  - every retired memory operation, and only those, reaches the V$: the
    VCache log lines are, in order, the committed loads, stores and atomics
    of the trace (pc, address, size), and an execution that took a fault
    (printed without a result) has no line;
  - the raw bytes of a load are what its register result is made from (sign
    or zero extended by its opcode), checked against the trace;
  - the whole log is replayed on an independent model of the V$ (VModel
    below) and every classification, every invalidation count and every
    vcache.* statistic must agree;
  - the expected outcome of each step of the scripted cases (SCRIPT), written
    from the design of the V$ and not from a run.

VModel and SCRIPT are written from the specification at the top of vcache.hh,
by the same author as the implementation: a pass shows that the code does what
that specification says, not that the specification is the paper's (the paper
does not specify per byte valid bits, the invalidation policy or the cold
state).
"""

import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "check_astra", os.path.join(HERE, "check_astra.py")
)
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)

fails = []

LINE = 16
ENTRIES = 256
FAULT_ADDR = 0x20000000
FAULT_PAGE = 4096
VBUF_BYTES = 16384
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


# ----------------------------------------------------------------- the model


def index_of(vaddr):
    return (vaddr >> 4) & (ENTRIES - 1)


def tag_of(vaddr):
    return vaddr >> 12


def fits(vaddr, size):
    return size >= 1 and (vaddr & (LINE - 1)) + size <= LINE


STAT_NAMES = [
    "retiredOps",
    "lookups",
    "tagHits",
    "tagMisses",
    "byteValidHits",
    "byteInvalidMisses",
    "valueCorrect",
    "valueWrong",
    "loadAllocations",
    "loadReplacements",
    "loadUpdates",
    "storeUpdates",
    "storeMisses",
    "uncacheableLoads",
    "uncacheableStores",
    "crossLineLoads",
    "crossLineStores",
    "specialOps",
    "invalidationOps",
    "bytesInvalidated",
]


class VModel:
    """256 entries of 16 bytes with one valid bit per byte, indexed by
    (vaddr >> 4) & 255, tagged with vaddr >> 12"""

    def __init__(self):
        self.occ = [False] * ENTRIES
        self.tag = [0] * ENTRIES
        self.data = [bytearray(LINE) for _ in range(ENTRIES)]
        self.valid = [0] * ENTRIES
        self.n = {k: 0 for k in STAT_NAMES}

    def load(self, vaddr, data):
        i, t, off, size = index_of(vaddr), tag_of(vaddr), vaddr & 15, len(data)
        want = ((1 << size) - 1) << off
        self.n["retiredOps"] += 1
        self.n["lookups"] += 1
        if not self.occ[i] or self.tag[i] != t:
            self.n["tagMisses"] += 1
            lookup = "tagmiss"
        else:
            self.n["tagHits"] += 1
            if self.valid[i] & want != want:
                self.n["byteInvalidMisses"] += 1
                lookup = "bytemiss"
            else:
                self.n["byteValidHits"] += 1
                if bytes(self.data[i][off : off + size]) == data:
                    self.n["valueCorrect"] += 1
                    lookup = "correct"
                else:
                    self.n["valueWrong"] += 1
                    lookup = "wrong"
        if not self.occ[i]:
            self.n["loadAllocations"] += 1
            update = "allocate"
            self.occ[i], self.tag[i], self.valid[i] = True, t, 0
        elif self.tag[i] != t:
            self.n["loadReplacements"] += 1
            update = "replace"
            self.tag[i], self.valid[i] = t, 0
        else:
            self.n["loadUpdates"] += 1
            update = "update"
        self.data[i][off : off + size] = data
        self.valid[i] |= want
        return lookup, update

    def store(self, vaddr, data):
        i, t, off, size = index_of(vaddr), tag_of(vaddr), vaddr & 15, len(data)
        self.n["retiredOps"] += 1
        if self.occ[i] and self.tag[i] == t:
            self.n["storeUpdates"] += 1
            self.data[i][off : off + size] = data
            self.valid[i] |= ((1 << size) - 1) << off
            return "update"
        self.n["storeMisses"] += 1
        return "miss"

    def other(self, kind, vaddr, size):
        """kind is as logged; returns the valid bytes cleared"""
        is_load = kind.endswith("_load")
        self.n["retiredOps"] += 1
        if kind == "special":
            self.n["specialOps"] += 1
        elif kind.startswith("uncacheable"):
            self.n["uncacheableLoads" if is_load else "uncacheableStores"] += 1
        else:
            self.n["crossLineLoads" if is_load else "crossLineStores"] += 1
        if kind == "crossline_load":
            return 0
        self.n["invalidationOps"] += 1
        cleared = 0
        for line in range(vaddr >> 4, ((vaddr + size - 1) >> 4) + 1):
            i = line & (ENTRIES - 1)
            if not self.occ[i] or self.tag[i] != tag_of(line << 4):
                continue
            lo = max(vaddr, line << 4)
            hi = min(vaddr + size, (line + 1) << 4)
            bits = ((1 << (hi - lo)) - 1) << (lo - (line << 4))
            cleared += bin(self.valid[i] & bits).count("1")
            self.valid[i] &= ~bits
        self.n["bytesInvalidated"] += cleared
        return cleared


# ------------------------------------------------------------- the readers

LOAD_LINE = re.compile(
    r"VCache load exec: (\d+) pc: 0x([0-9a-f]+) vaddr: 0x([0-9a-f]+) "
    r"size: (\d+) data: ([0-9a-f]+) lookup: (\w+) update: (\w+)\s*$"
)
STORE_LINE = re.compile(
    r"VCache store exec: (\d+) pc: 0x([0-9a-f]+) vaddr: 0x([0-9a-f]+) "
    r"size: (\d+) data: ([0-9a-f]+) update: (\w+)\s*$"
)
OTHER_LINE = re.compile(
    r"VCache other exec: (\d+) pc: 0x([0-9a-f]+) vaddr: 0x([0-9a-f]+) "
    r"size: (\d+) kind: (\w+) invalidated: (\d+)\s*$"
)
MEM_SIZES = {
    "lb": 1, "lbu": 1, "sb": 1, "lh": 2, "lhu": 2, "sh": 2,
    "lw": 4, "lwu": 4, "sw": 4, "flw": 4, "fsw": 4,
    "ld": 8, "sd": 8, "fld": 8, "fsd": 8,
}  # fmt: skip
ATOMIC = re.compile(r"^(amo\w+|lr|sc)\.([wd])$")
RESULT_RE = re.compile(r"\bD=0x([0-9a-f]+)")


def mem_size(mnemonic):
    if mnemonic in MEM_SIZES:
        return MEM_SIZES[mnemonic]
    m = ATOMIC.match(mnemonic)
    if m:
        return 4 if m.group(2) == "w" else 8
    return None


def read_run_trace(path):
    """The VCache events, the committed memory operations and the committed
    instruction lines of a trace, in order.  Operations printed without a
    result (a load that took a fault) are returned separately.  None if the
    trace cannot be read."""
    tr = {
        "events": [],
        "memops": [],
        "faulting": [],
        "insns": [],
        "astra": [],
        "problems": [],
    }
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if "VCache " in line:
            m = LOAD_LINE.search(line)
            if m:
                g = m.groups()
                tr["events"].append(
                    {
                        "kind": "load",
                        "exec": int(g[0]),
                        "pc": int(g[1], 16),
                        "vaddr": int(g[2], 16),
                        "size": int(g[3]),
                        "data": bytes.fromhex(g[4]),
                        "lookup": g[5],
                        "update": g[6],
                    }
                )
                continue
            m = STORE_LINE.search(line)
            if m:
                g = m.groups()
                tr["events"].append(
                    {
                        "kind": "store",
                        "exec": int(g[0]),
                        "pc": int(g[1], 16),
                        "vaddr": int(g[2], 16),
                        "size": int(g[3]),
                        "data": bytes.fromhex(g[4]),
                        "update": g[5],
                    }
                )
                continue
            m = OTHER_LINE.search(line)
            if m:
                g = m.groups()
                tr["events"].append(
                    {
                        "kind": "other",
                        "exec": int(g[0]),
                        "pc": int(g[1], 16),
                        "vaddr": int(g[2], 16),
                        "size": int(g[3]),
                        "what": g[4],
                        "invalidated": int(g[5]),
                    }
                )
                continue
            tr["problems"].append("line %d: malformed VCache line" % n)
            continue
        if ca.ASTRA_EVENT.search(line):
            ev = ca.parse_event(line)
            if ev is None:
                tr["problems"].append("line %d: malformed ASTRA line" % n)
            else:
                tr["astra"].append(ev)
            continue
        m = ca.INSN_RE.search(line)
        if not m:
            tr["problems"].append("line %d: not an instruction" % n)
            continue
        tr["insns"].append(line)
        mnem = m.group(2)
        size = mem_size(mnem)
        if size is None:
            continue
        ea = ca.EA_RE.search(line)
        if not ea:
            tr["problems"].append("line %d: memory op without A=" % n)
            continue
        res = RESULT_RE.search(line)
        op = {
            "pc": int(m.group(1), 16),
            "mnemonic": mnem,
            "vaddr": int(ea.group(1), 16),
            "size": size,
            "result": int(res.group(1), 16) if res else None,
        }
        is_load = mnem.startswith("l") or mnem.startswith("fl")
        if is_load and res is None and not mnem.startswith("lr"):
            tr["faulting"].append(op)
        else:
            tr["memops"].append(op)
    return tr


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


def read_text(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def counters(stats, group):
    prefix = "system.cpu.%s." % group
    return {
        k[len(prefix) :]: int(float(v))
        for k, v in stats.items()
        if k.startswith(prefix)
    }


class Run:
    pass


def load_run(out, name, vcache, astra=None):
    r = Run()
    r.name = name
    try:
        r.rc = int(read_text(os.path.join(out, name + ".rc")).strip())
    except ValueError:
        r.rc = None
    check(r.rc == 0, "%s: gem5 exit status 0 (got %s)" % (name, r.rc))
    try:
        r.stats = ca.read_stats(out, name)
    except OSError:
        r.stats = None
    r.params = cpu_params(out, name)
    r.tr = read_run_trace(os.path.join(out, name, "trace.txt"))
    ok = r.rc == 0 and r.stats is not None and r.params and r.tr
    check(ok, "%s: statistics, configuration and trace can be read" % name)
    if not ok:
        return None
    check(
        r.params.get("enableVCache") == ("true" if vcache else "false"),
        "%s: enableVCache is %s" % (name, vcache),
    )
    if astra is not None:
        check(
            r.params.get("enableAstra") == ("true" if astra else "false"),
            "%s: enableAstra is %s" % (name, astra),
        )
    check(
        r.params.get("threadPolicy") == "SingleThreaded"
        and r.params.get("executeMaxAccessesInMemory") == "16"
        and r.params.get("executeLSQTransfersQueueSize") == "16",
        "%s: research baseline settings" % name,
    )
    check(
        any("cpu.vcache." in k for k in r.stats) == vcache,
        "%s: vcache.* statistics %s"
        % (name, "present" if vcache else "absent"),
    )
    check(
        not r.tr["problems"],
        "%s: the trace has only instruction, ASTRA and VCache lines (%d "
        "others)" % (name, len(r.tr["problems"])),
    )
    m = re.search(
        r"^checksum (-?\d+)\s*$",
        read_text(os.path.join(out, name + ".out")),
        re.M,
    )
    r.checksum = int(m.group(1)) if m else None
    r.insts = int(float(r.stats["simInsts"]))
    r.cycles = int(float(r.stats["system.cpu.numCycles"]))
    r.vc = counters(r.stats, "vcache")
    return r


def noninterference(d, s, label):
    check(
        d.checksum is not None and d.checksum == s.checksum,
        "%s: same checksum in D and S (%s, %s)"
        % (label, d.checksum, s.checksum),
    )
    check(
        d.insts == s.insts,
        "%s: same committed instruction count (%d, %d)"
        % (label, d.insts, s.insts),
    )
    check(
        d.tr["insns"] == s.tr["insns"],
        "%s: identical committed instruction trace" % label,
    )
    nd = {k: v for k, v in d.stats.items() if "cpu.vcache." not in k}
    ns = {k: v for k, v in s.stats.items() if "cpu.vcache." not in k}
    check(
        nd == ns,
        "%s: identical statistics apart from vcache.*, cycles included "
        "(%d, %d)" % (label, d.cycles, s.cycles),
    )
    check(not d.tr["events"], "%s: D logs no VCache event" % label)


def disasm_map(path):
    """{pc: mnemonic} of a disassembly (objdump -d --no-show-raw-insn)"""
    pcs = {}
    for line in read_text(path).splitlines():
        m = re.match(r"^\s*([0-9a-f]+):\s+(\S+)", line)
        if m:
            pcs[int(m.group(1), 16)] = m.group(2)
    return pcs


def one_to_one(s, label, dis=None):
    """The events are the committed loads, stores and atomics, in order.
    The execution trace has no line for an atomic operation (amo, lr, sc),
    so a "special" event at an atomic instruction of the disassembly dis is
    matched with that instruction instead.  Returns [(event, operation)]"""
    ev, ops = s.tr["events"], s.tr["memops"]
    aligned, j, bad, atomics = [], 0, 0, 0
    for e in ev:
        if (
            e["kind"] == "other"
            and e["what"] == "special"
            and dis is not None
            and ATOMIC.match(dis.get(e["pc"], ""))
        ):
            atomics += 1
            aligned.append(
                (
                    e,
                    {
                        "pc": e["pc"],
                        "mnemonic": dis[e["pc"]],
                        "vaddr": e["vaddr"],
                        "size": e["size"],
                        "result": None,
                    },
                )
            )
            continue
        o = ops[j] if j < len(ops) else None
        if o is None or (e["pc"], e["vaddr"], e["size"]) != (
            o["pc"],
            o["vaddr"],
            o["size"],
        ):
            bad += 1
        else:
            aligned.append((e, o))
        j += 1
    check(
        len(ev) == len(ops) + atomics and j == len(ops),
        "%s: %d VCache lines = %d committed memory operations + %d atomic "
        "operations (which the trace does not print)"
        % (label, len(ev), len(ops), atomics),
    )
    check(
        bad == 0,
        "%s: each line is the committed operation at the same position "
        "(pc, address, size): %d differ" % (label, bad),
    )
    return aligned


def extension_check(aligned, label):
    """A load's raw bytes, extended as its opcode says, are its result"""
    bad = 0
    n_checked = 0
    for e, o in aligned:
        m = o["mnemonic"]
        if e["kind"] != "load" or m not in (
            "lb",
            "lbu",
            "lh",
            "lhu",
            "lw",
            "lwu",
            "ld",
        ):
            continue
        raw = int.from_bytes(e["data"], "little")
        bits = 8 * e["size"]
        if m in ("lb", "lh", "lw") and raw >> (bits - 1):
            raw -= 1 << bits
        n_checked += 1
        if o["result"] != raw & ((1 << 64) - 1):
            bad += 1
    check(
        bad == 0 and n_checked > 0,
        "%s: the raw bytes of %d integer loads, extended by their opcode "
        "(lb lbu lh lhu lw lwu ld), are the traced results (%d differ)"
        % (label, n_checked, bad),
    )


def replay(s, label):
    """Replays the events on VModel; returns the model, or None"""
    m = VModel()
    bad = []
    for n, e in enumerate(s.tr["events"]):
        if e["kind"] == "load":
            if not fits(e["vaddr"], e["size"]):
                bad.append((n, "a load that crosses a line was looked up"))
                continue
            lookup, update = m.load(e["vaddr"], e["data"])
            if (lookup, update) != (e["lookup"], e["update"]):
                bad.append(
                    (
                        n,
                        "load: model %s/%s, log %s/%s"
                        % (lookup, update, e["lookup"], e["update"]),
                    )
                )
        elif e["kind"] == "store":
            if not fits(e["vaddr"], e["size"]):
                bad.append((n, "a store that crosses a line was applied"))
                continue
            update = m.store(e["vaddr"], e["data"])
            if update != e["update"]:
                bad.append(
                    (n, "store: model %s, log %s" % (update, e["update"]))
                )
        else:
            if e["what"].startswith("crossline") and fits(
                e["vaddr"], e["size"]
            ):
                bad.append((n, "crossline for an access that fits"))
            cleared = m.other(e["what"], e["vaddr"], e["size"])
            if cleared != e["invalidated"]:
                bad.append(
                    (
                        n,
                        "other %s: model cleared %d, log %d"
                        % (e["what"], cleared, e["invalidated"]),
                    )
                )
    check(
        not bad,
        "%s: every VCache line agrees with the model (%d disagreements)%s"
        % (label, len(bad), (": %s" % (bad[0],)) if bad else ""),
    )
    differ = {
        k: (m.n[k], s.vc.get(k)) for k in STAT_NAMES if m.n[k] != s.vc.get(k)
    }
    check(
        not differ,
        "%s: every vcache.* statistic equals the model's count%s"
        % (label, (": %s" % differ) if differ else ""),
    )
    return m


def identities(s, label):
    v = s.vc
    check(
        v["retiredOps"]
        == v["lookups"]
        + v["storeUpdates"]
        + v["storeMisses"]
        + v["uncacheableLoads"]
        + v["uncacheableStores"]
        + v["crossLineLoads"]
        + v["crossLineStores"]
        + v["specialOps"],
        "%s: retiredOps = lookups + store updates + store misses + "
        "uncacheable + cross-line + special" % label,
    )
    check(
        v["lookups"] == v["tagHits"] + v["tagMisses"]
        and v["tagHits"] == v["byteValidHits"] + v["byteInvalidMisses"]
        and v["byteValidHits"] == v["valueCorrect"] + v["valueWrong"],
        "%s: lookups = tag hits + misses, tag hits = byte-valid hits + "
        "byte-invalid misses, byte-valid hits = correct + wrong" % label,
    )
    check(
        v["lookups"]
        == v["loadAllocations"] + v["loadReplacements"] + v["loadUpdates"],
        "%s: every looked-up load allocates, replaces or updates" % label,
    )


# ---------------------------------------------------------------- assembly


def asm_main(mode, disasm, symbols, pcs_out):
    syms = {}
    with open(symbols) as f:
        for line in f:
            t = line.split()
            if len(t) >= 3:
                syms[t[-1]] = int(t[0], 16)
    text = read_text(disasm)
    problems = []
    if mode != "fault" and ("vbuf" not in syms or syms["vbuf"] % 8192):
        problems.append("vbuf is missing or not 8 KiB aligned")
    mnems = set(re.findall(r"^\s*[0-9a-f]+:\s+(\S+)", text, re.M))
    if mode == "ops":
        for need in (
            "lb",
            "lbu",
            "lh",
            "lhu",
            "lw",
            "lwu",
            "ld",
            "sb",
            "sh",
            "sw",
            "sd",
            "amoadd.d",
            "lr.d",
            "sc.d",
        ):
            if need not in mnems:
                problems.append("no %s instruction in the program" % need)
        if any(m.startswith("c.") for m in mnems):
            problems.append("compressed instructions")
    if mode == "squash":
        for need in ("vc_wrong", "vc_real"):
            if need not in syms:
                problems.append("symbol %s not found" % need)
        m = re.search(
            r"^\s*%x:\s+(\S+)\s+(\S+)" % syms.get("vc_wrong", -1), text, re.M
        )
        if not m or m.group(1) != "ld":
            problems.append("vc_wrong is not a load")
    for p in problems:
        print("FAIL: " + p)
    if problems:
        return 1
    with open(pcs_out, "w") as f:
        for n in ("vbuf", "vc_wrong", "vc_real"):
            if n in syms:
                f.write("%s 0x%x\n" % (n, syms[n]))
    print("PASS: the %s program is as intended" % mode)
    return 0


SCRIPT = [
    (1, "st", 0x000, 8, "miss"),
    (2, "ld", 0x000, 8, ("tagmiss", "allocate")),
    (3, "ld", 0x000, 8, ("correct", "update")),
    (4, "ld", 0x007, 1, ("correct", "update")),   # lb
    (5, "ld", 0x007, 1, ("correct", "update")),   # lbu
    (6, "ld", 0x006, 2, ("correct", "update")),   # lh
    (7, "ld", 0x006, 2, ("correct", "update")),   # lhu
    (8, "ld", 0x004, 4, ("correct", "update")),   # lw
    (9, "ld", 0x004, 4, ("correct", "update")),   # lwu
    (10, "st", 0x010, 4, "miss"),
    (11, "ld", 0x010, 4, ("tagmiss", "allocate")),
    (12, "ld", 0x013, 1, ("correct", "update")),
    (13, "ld", 0x013, 1, ("correct", "update")),
    (14, "ld", 0x020, 8, ("tagmiss", "allocate")),
    (15, "st", 0x022, 1, "update"),
    (16, "st", 0x024, 2, "update"),
    (17, "st", 0x027, 1, "update"),
    (18, "ld", 0x020, 8, ("correct", "update")),
    (19, "ld", 0x021, 1, ("correct", "update")),
    (20, "ld", 0x024, 2, ("correct", "update")),
    (21, "st", 0x030, 8, "miss"),
    (22, "ld", 0x030, 8, ("tagmiss", "allocate")),
    (23, "ld", 0x040, 8, ("tagmiss", "allocate")),
    (24, "ld", 0x048, 8, ("bytemiss", "update")),
    (25, "ld", 0x1040, 8, ("tagmiss", "replace")),
    (26, "ld", 0x1048, 8, ("bytemiss", "update")),
    (27, "ld", 0x040, 8, ("tagmiss", "replace")),
    (28, "ld", 0x1040, 8, ("tagmiss", "replace")),
    (29, "ld", 0x050, 4, ("tagmiss", "allocate")),
    (30, "ld", 0x054, 4, ("bytemiss", "update")),
    (31, "ld", 0x050, 8, ("correct", "update")),
    (32, "ld", 0x06c, 8, ("crossline_load", 0)),
    (33, "ld", 0x06c, 4, ("tagmiss", "allocate")),
    (34, "ld", 0x088, 8, ("tagmiss", "allocate")),
    (35, "ld", 0x090, 8, ("tagmiss", "allocate")),
    (36, "st", 0x08c, 8, ("crossline_store", 8)),
    (37, "ld", 0x088, 4, ("correct", "update")),
    (38, "ld", 0x08c, 4, ("bytemiss", "update")),
    (39, "ld", 0x090, 4, ("bytemiss", "update")),
    (40, "ld", 0x094, 4, ("correct", "update")),
    (41, "ld", 0x0a0, 8, ("tagmiss", "allocate")),
    (42, "amo", 0x0a0, 8, ("special", 8)),
    (43, "ld", 0x0a0, 8, ("bytemiss", "update")),
    (44, "ld", 0x0b0, 8, ("tagmiss", "allocate")),
    (45, "lr", 0x0b0, 8, ("special", 8)),
    (46, "ld", 0x0b0, 8, ("bytemiss", "update")),
    (47, "lr", 0x0b0, 8, ("special", 8)),
    (48, "sc", 0x0b0, 8, ("special", 0)),
    (49, "ld", 0x0b0, 8, ("bytemiss", "update")),
    (50, "ld", 0x0d0, 4, ("tagmiss", "allocate")),
    (51, "st", 0x0d4, 4, "update"),
    (52, "ld", 0x0d0, 8, ("correct", "update")),
]  # fmt: skip

WHY = {
    1: "a store to an absent line does not allocate",
    2: "the first load allocates",
    4: "a signed byte inside valid bytes hits",
    15: "a partial store updates a matching entry",
    18: "neighbouring bytes survived the partial stores",
    22: "the line was not allocated by the store",
    25: "a tag conflict replaces the entry",
    26: "the replaced entry's old valid bytes are cleared",
    30: "tag match but the bytes were not observed",
    31: "the neighbouring valid bytes were kept",
    32: "a load that crosses a 16 byte boundary is rejected",
    33: "and did not allocate",
    36: "a store that crosses a boundary invalidates the bytes it covers",
    37: "bytes it did not cover stayed valid",
    38: "bytes it covered are invalid",
    42: "an atomic operation invalidates its bytes",
    45: "so does a load-reserved",
    48: "and a store-conditional (nothing left to clear)",
    52: "a store made the bytes it wrote valid",
}


def script_events(aligned, vbuf):
    return [
        (e, o) for e, o in aligned if vbuf <= e["vaddr"] < vbuf + VBUF_BYTES
    ]


def ops_main(out):
    pcs = read_pcs(out)
    vbuf = pcs["vbuf"]
    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    if d is None or s is None:
        return finish()
    noninterference(d, s, "ops")

    section("Retirement: every committed memory operation, once, in order")
    aligned = one_to_one(s, "ops", disasm_map(os.path.join(out, "vc.S")))
    extension_check(aligned, "ops")
    check(
        not s.tr["faulting"],
        "no committed execution took a fault (%d)" % len(s.tr["faulting"]),
    )

    section("The scripted cases (expected outcomes from the design)")
    se = script_events(aligned, vbuf)
    check(
        len(se) == len(SCRIPT),
        "the script has %d memory operations on vbuf (got %d)"
        % (len(SCRIPT), len(se)),
    )
    if len(se) != len(SCRIPT):
        return finish()
    other_idx = {
        index_of(e["vaddr"])
        for e in s.tr["events"]
        if not (vbuf <= e["vaddr"] < vbuf + VBUF_BYTES)
    }
    script_idx = {index_of(vbuf + off) for _, _, off, _, _ in SCRIPT}
    check(
        not (other_idx & script_idx),
        "premise: no other access (stack) maps to an index of the script "
        "(%s)" % sorted(other_idx & script_idx),
    )
    bad = []
    for (e, o), (n, acc, off, size, want) in zip(se, SCRIPT):
        got_shape = (e["vaddr"] - vbuf, e["size"])
        mnem = o["mnemonic"]
        kind_ok = {
            "ld": e["kind"] in ("load", "other"),
            "st": e["kind"] in ("store", "other"),
            "amo": mnem.startswith("amo"),
            "lr": mnem.startswith("lr"),
            "sc": mnem.startswith("sc"),
        }[acc]
        if got_shape != (off, size) or not kind_ok:
            bad.append(
                "step %d: the program did %s %#x/%d" % (n, mnem, *got_shape)
            )
            continue
        if e["kind"] == "load":
            got = (e["lookup"], e["update"])
        elif e["kind"] == "store":
            got = e["update"]
        else:
            got = (e["what"], e["invalidated"])
        if got != want:
            bad.append(
                "step %d (%s %#x/%d): expected %s, got %s"
                % (n, mnem, off, size, want, got)
            )
    check(
        not bad,
        "all %d steps have the expected outcome%s"
        % (len(SCRIPT), (": " + bad[0]) if bad else ""),
    )
    for b in bad[1:6]:
        print("      " + b)
    for n, why in sorted(WHY.items()):
        report("step %d: %s" % (n, why))

    section("The model and the statistics")
    replay(s, "ops")
    identities(s, "ops")
    check(
        s.vc["valueWrong"] == 0,
        "no diagnostic hit had wrong data (%d)" % s.vc["valueWrong"],
    )
    check(
        s.vc["uncacheableLoads"] == 0 and s.vc["uncacheableStores"] == 0,
        "no uncacheable access: a user-mode SE program cannot make one, so "
        "that path is not exercised here",
    )
    report(
        "retired %d, lookups %d (tag hits %d, byte-valid hits %d, byte-"
        "invalid misses %d), load allocations %d, replacements %d, "
        "updates %d, store updates %d, store misses %d, cross-line %d+%d, "
        "special %d, bytes invalidated %d"
        % (
            s.vc["retiredOps"],
            s.vc["lookups"],
            s.vc["tagHits"],
            s.vc["byteValidHits"],
            s.vc["byteInvalidMisses"],
            s.vc["loadAllocations"],
            s.vc["loadReplacements"],
            s.vc["loadUpdates"],
            s.vc["storeUpdates"],
            s.vc["storeMisses"],
            s.vc["crossLineLoads"],
            s.vc["crossLineStores"],
            s.vc["specialOps"],
            s.vc["bytesInvalidated"],
        )
    )
    return finish()


# ------------------------------------------------------------ squash, fault


def astra_outcomes(s):
    return [e for e in s.tr["astra"] if e["kind"] == "outcome"]


def squash_main(out):
    pcs = read_pcs(out)
    vbuf, wrong = pcs["vbuf"], pcs["vc_wrong"]
    section("Preflight and non-interference")
    d = load_run(out, "D", False, astra=True)
    s = load_run(out, "S", True, astra=True)
    if d is None or s is None:
        return finish()
    noninterference(d, s, "squash")
    one_to_one(s, "squash", disasm_map(os.path.join(out, "vc.S")))

    section("The wrong-path load was issued and squashed")
    sq = [
        e
        for e in astra_outcomes(s)
        if e["outcome"] == "squashed" and e["pc"] == wrong
    ]
    check(
        len(sq) >= 1,
        "ASTRA looked up and squashed the load at vc_wrong (%#x): %d time(s)"
        % (wrong, len(sq)),
    )
    check(
        not any(o["pc"] == wrong for o in s.tr["memops"]),
        "it never retired: no committed load at vc_wrong",
    )

    section("It left no trace in the V$")
    check(
        not any(e["pc"] == wrong for e in s.tr["events"]),
        "no VCache line for the load at vc_wrong",
    )
    ev = [e for e in s.tr["events"] if vbuf <= e["vaddr"] < vbuf + VBUF_BYTES]
    shape = [(e["kind"], e["vaddr"] - vbuf, e["size"]) for e in ev]
    check(
        shape
        == [
            ("store", 0x240, 8),
            ("load", 0x240, 8),
            ("load", 0x200, 8),
            ("load", 0x240, 8),
        ],
        "the retired accesses to vbuf are: a store and the real load at "
        "+0x240, a load at +0x200 and a load at +0x240 (%s)"
        % [(k, hex(a), z) for k, a, z in shape],
    )
    if len(ev) == 4:
        check(
            ev[0]["update"] == "miss"
            and (ev[1]["lookup"], ev[1]["update"]) == ("tagmiss", "allocate"),
            "the store missed, and the real load allocated",
        )
        check(
            (ev[2]["lookup"], ev[2]["update"]) == ("tagmiss", "allocate"),
            "the first retired load from +0x200, the line the squashed "
            "load had addressed, is a tag miss that allocates: the "
            "squashed load did not allocate it",
        )
        check(
            (ev[3]["lookup"], ev[3]["update"]) == ("correct", "update"),
            "the later load from +0x240 hits with the right data",
        )
    section("The model and the statistics")
    replay(s, "squash")
    identities(s, "squash")
    return finish()


def fault_main(out):
    section("Preflight and non-interference")
    d = load_run(out, "D", False, astra=True)
    s = load_run(out, "S", True, astra=True)
    if d is None or s is None:
        return finish()
    noninterference(d, s, "fault")
    one_to_one(s, "fault", disasm_map(os.path.join(out, "vc.S")))

    section("The faulting executions do not reach the V$")
    faulted = [e for e in astra_outcomes(s) if e["outcome"] == "faulted"]
    check(
        len(faulted) == 2
        and ca.read_stats(out, "S")["system.cpu.astra." "faultedLoads"] == "2",
        "two loads took the translation fault (ASTRA outcome faulted: %d)"
        % len(faulted),
    )
    check(
        len(s.tr["faulting"]) == 2
        and all(
            o["vaddr"] in (FAULT_ADDR, FAULT_ADDR + FAULT_PAGE)
            for o in s.tr["faulting"]
        ),
        "the trace shows the two faulting loads (address, no result): %s"
        % [hex(o["vaddr"]) for o in s.tr["faulting"]],
    )
    region = [
        e
        for e in s.tr["events"]
        if FAULT_ADDR <= e["vaddr"] < FAULT_ADDR + 2 * FAULT_PAGE
    ]
    check(
        len(region) == 3,
        "3 VCache lines for the 3 loads that completed, none for the 2 "
        "that faulted (%d)" % len(region),
    )
    if len(region) == 3:
        got = [
            (e["vaddr"] - FAULT_ADDR, e["lookup"], e["update"]) for e in region
        ]
        check(
            got
            == [
                (0, "tagmiss", "allocate"),
                (0, "correct", "update"),
                (FAULT_PAGE, "tagmiss", "replace"),
            ],
            "they are: allocate, hit, and a conflicting page that replaces "
            "(%s)" % got,
        )
    section("The model and the statistics")
    replay(s, "fault")
    identities(s, "fault")
    return finish()


# --------------------------------------------------------- noninterference


def noninterference_main(out):
    section("ASTRA test program and LVP phase workload, V$ off and on")
    for tag, label, expect in (
        ("astra", "astra.c", None),
        ("lvp", "LVP phase", PHASE_CHECKSUM),
    ):
        section(label)
        d = load_run(out, tag + "D", False, astra=True)
        s = load_run(out, tag + "S", True, astra=True)
        if d is None or s is None:
            continue
        noninterference(d, s, label)
        if expect is not None:
            check(
                s.checksum == expect,
                "%s: checksum %d (got %s)" % (label, expect, s.checksum),
            )
        check(
            counters(d.stats, "astra") == counters(s.stats, "astra")
            and counters(d.stats, "astra"),
            "%s: ASTRA statistics present and identical" % label,
        )
        if tag == "lvp":
            check(
                counters(d.stats, "lvp") == counters(s.stats, "lvp")
                and counters(s.stats, "lvp")["specLoadsCommitted"] > 0
                and counters(s.stats, "lvp")["mispredictSquashes"] > 0,
                "%s: LVP statistics identical, with speculative loads and "
                "recovery in the run" % label,
            )
        aligned = one_to_one(s, label)
        extension_check(aligned, label)
        replay(s, label)
        identities(s, label)
        check(
            s.vc["valueWrong"] == 0,
            "%s: no diagnostic hit had wrong data (%d of %d byte-valid "
            "hits)" % (label, s.vc["valueWrong"], s.vc["byteValidHits"]),
        )
        report(
            "%s: retired %d, lookups %d, tag hits %d, byte-valid hits %d, "
            "special %d, cross-line %d+%d, store updates %d, store "
            "misses %d"
            % (
                label,
                s.vc["retiredOps"],
                s.vc["lookups"],
                s.vc["tagHits"],
                s.vc["byteValidHits"],
                s.vc["specialOps"],
                s.vc["crossLineLoads"],
                s.vc["crossLineStores"],
                s.vc["storeUpdates"],
                s.vc["storeMisses"],
            )
        )
    return finish()


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "asm":
        return asm_main(*sys.argv[2:])
    modes = {
        "ops": ops_main,
        "squash": squash_main,
        "fault": fault_main,
        "noninterference": noninterference_main,
    }
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        print(__doc__)
        return 2
    return modes[sys.argv[1]](sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
