#!/usr/bin/env python3
"""Checks of the V-ASTRA bypass tests (run_bypass.sh).  Every function reads
the outputs of runs made by run_bypass.sh; see README.md, "V-ASTRA bypass
tests"."""

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


def windows(path):
    """Every statistics window of stats.txt"""
    blocks, cur = [], None
    with open(path) as f:
        for line in f:
            if line.startswith("---------- Begin"):
                cur = {}
                blocks.append(cur)
            elif cur is not None and not line.startswith("-"):
                m = re.match(r"(\S+)\s+(\S+)", line)
                if m:
                    try:
                        cur[m.group(1)] = float(m.group(2))
                    except ValueError:
                        pass
    return blocks


def stats(path):
    """The statistics of the last window in stats.txt"""
    values = {}
    with open(path) as f:
        for line in f:
            if line.startswith("---------- Begin"):
                values = {}
                continue
            m = re.match(r"(\S+)\s+(\S+)", line)
            if m and not line.startswith("-"):
                try:
                    values[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return values


COMMIT = re.compile(r"^system\.cpu: 0x[0-9a-f]+ +:")


def commits(path):
    with open(path, errors="replace") as f:
        return [line for line in f if COMMIT.match(line)]


def program_lines(path):
    with open(path, errors="replace") as f:
        return [l.strip() for l in f if l.startswith(("checksum", "errors"))]


def program_values(path):
    """checksum and errors values, tolerant of the program's output being
    interleaved with the wrapper's and gem5's own lines"""
    text = open(path, errors="replace").read()
    text = "\n".join(
        l
        for l in text.splitlines()
        if not re.search(
            r"reset statistics|: info:|: warn:|^Exiting|^Entering", l
        )
        or l.startswith(("checksum", "errors"))
    )
    return re.findall(r"(?:checksum|errors)\s+(-?\d+)", text) or re.findall(
        r"^(-?\d{4,})$", text, re.M
    )


class Run:
    def __init__(self, d, name):
        self.d, self.name = d, name
        self.out = os.path.join(d, name + ".out")
        self.rc = int(open(os.path.join(d, name + ".rc")).read())
        sp = os.path.join(d, name, "stats.txt")
        self.s = stats(sp) if os.path.exists(sp) else {}
        self.trace = os.path.join(d, name, "trace.txt")

    def stat(self, name):
        return self.s["system.cpu." + name]

    def bypass_pcs(self):
        pcs = []
        with open(self.trace, errors="replace") as f:
            for line in f:
                m = re.search(r"VBypass exec: (\d+) pc: (0x[0-9a-f]+) ", line)
                if m:
                    pcs.append((int(m.group(1)), int(m.group(2), 16)))
        return pcs


def identity(r, label):
    g = r.stat
    lhs = g("vastra.candidates") + g("vastra.carryIn")
    rhs = sum(
        g("vastra." + n)
        for n in (
            "candidatesDiscarded",
            "candidatesFaulted",
            "rejUnsupported",
            "eaMismatch",
            "lookups",
            "candidatesBypassedRetired",
            "inFlight",
        )
    )
    check(
        lhs == rhs,
        "%s: candidates + carryIn = outcomes (%d = %d)" % (label, lhs, rhs),
    )


def pair(d, label, pcs=None, ref_accesses=True):
    """P (bypass off) against B (bypass on) of one workload"""
    p, b = Run(d, "P"), Run(d, "B")
    check(p.rc == 0 and b.rc == 0, "%s: both runs exit 0" % label)
    check(
        program_lines(p.out) == program_lines(b.out),
        "%s: program output identical (%s)" % (label, program_lines(b.out)),
    )
    errs = [l for l in program_lines(b.out) if l.startswith("errors")]
    check(
        all(l == "errors 0" for l in errs), "%s: no stale value seen" % label
    )
    ct_p, ct_b = commits(p.trace), commits(b.trace)
    check(
        len(ct_p) > 0 and ct_p == ct_b,
        "%s: committed instruction trace identical (%d lines)"
        % (label, len(ct_p)),
    )
    for n in ("simInsts", "simOps"):
        if "%s" % n in p.s:
            check(
                p.s[n] == b.s[n], "%s: %s identical (%d)" % (label, n, p.s[n])
            )
    sr = b.stat("vastra.syntheticResponses")
    check(
        sr
        == b.stat("vastra.eligibleBypass")
        == b.stat("vastra.memRequestsSuppressed"),
        "%s: synthetic responses = eligible = requests suppressed (%d)"
        % (label, sr),
    )
    check(
        all(
            p.stat("vastra." + n) == 0
            for n in (
                "syntheticResponses",
                "eligibleBypass",
                "memRequestsSuppressed",
                "candidatesBypassedRetired",
                "bypassAbandoned_",
            )
        ),
        "%s: bypass disabled: no bypass counter moves" % label,
    )
    check(
        b.stat("vastra.candidatesBypassedRetired") <= sr,
        "%s: bypassed loads retired (%d) <= synthetic responses (%d)"
        % (label, b.stat("vastra.candidatesBypassedRetired"), sr),
    )
    identity(p, label + " P")
    identity(b, label + " B")
    br = b.stat("vastra.candidatesBypassedRetired")
    check(
        p.stat("vcache.lookups") - b.stat("vcache.lookups") == br
        and p.stat("vcache.retiredOps") - b.stat("vcache.retiredOps") == br,
        "%s: the V$ did not observe the %d bypassed loads (lookups %d -> "
        "%d, retiredOps %d -> %d)"
        % (
            label,
            br,
            p.stat("vcache.lookups"),
            b.stat("vcache.lookups"),
            p.stat("vcache.retiredOps"),
            b.stat("vcache.retiredOps"),
        ),
    )
    if ref_accesses:
        pa = p.s["system.cpu.dcache.ReadReq.accesses::total"]
        ba = b.s["system.cpu.dcache.ReadReq.accesses::total"]
        check(
            pa - ba == sr,
            "%s: data cache read accesses %d -> %d, "
            "fewer by the %d suppressed" % (label, pa, ba, sr),
        )
    pts = b.bypass_pcs()
    check(
        len(pts) == sr, "%s: one VBypass line per synthetic response" % label
    )
    info(
        "%s: cycles %d -> %d, bypassed %d, fallback: rejected-at-decision "
        "%d tag %d byte %d crossline %d unsupported %d eamismatch %d "
        "olderstore %d abandoned %d"
        % (
            label,
            p.stat("numCycles"),
            b.stat("numCycles"),
            sr,
            b.stat("vastra.fbDecisionRejected"),
            b.stat("vastra.fbTagMiss"),
            b.stat("vastra.fbByteMiss"),
            b.stat("vastra.fbCrossLine"),
            b.stat("vastra.fbUnsupported"),
            b.stat("vastra.fbEaMismatch"),
            b.stat("vastra.fbOlderStore"),
            b.stat("vastra.bypassAbandoned_"),
        )
    )
    return p, b


def read_pcs(path):
    pcs = {}
    for line in open(path):
        k, v = line.split()
        pcs[k] = int(v, 16)
    return pcs


def evidence(b, label, need=True):
    """Each bypassed load: no request to memory, no response from memory,
    and one LSQ lifecycle (deleted once, at most one memory response)"""
    ids = {}
    key = re.compile(r"/(\d+)\.(\d+) pc:")
    kinds = (
        ("Trying to send request", "send"),
        ("Received response packet inst", "recv"),
        ("Memory response inst", "resp"),
        ("Deleting load request", "del"),
        ("Found matching memory response", "found"),
    )
    with open(b.trace, errors="replace") as f:
        for line in f:
            for text, k in kinds:
                if text in line:
                    m = key.search(line)
                    if m:
                        ids.setdefault(int(m.group(2)), {}).setdefault(k, 0)
                        ids[int(m.group(2))][k] += 1
    execs = [e for e, _ in b.bypass_pcs()]
    bad = []
    for e in execs:
        c = ids.get(e, {})
        if (
            c.get("send", 0)
            or c.get("recv", 0)
            or c.get("resp", 0) > 1
            or c.get("del", 0) != 1
            or (c.get("found", 0) > 1 and c.get("resp", 0))
        ):
            bad.append((e, c))
    check(
        (len(execs) > 0 or not need) and not bad,
        "%s: %d bypassed loads: no request sent, no memory response, "
        "deleted once, one completion unless discarded (the discard path looks "
        "the response up twice) (%d violations)"
        % (label, len(execs), len(bad)),
    )
    for e, c in bad[:5]:
        print("      exec %d: %s" % (e, c))
    return execs


def mode_classify(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "classify")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    check(
        at.get(pcs["va_hit"], 0) > 0,
        "classify: correct base + valid bytes bypass (va_hit %d)"
        % at.get(pcs["va_hit"], 0),
    )
    for n in ("va_tm", "va_bm", "va_x"):
        check(
            at.get(pcs[n], 0) == 0,
            "classify: %s never bypasses (tag miss / byte miss / cross "
            "line)" % n,
        )
    check(b.stat("vastra.fbTagMiss") > 0, "classify: tag-miss fallback seen")
    check(
        b.stat("vastra.fbByteMiss") > 0, "classify: byte-invalid fallback seen"
    )
    check(
        b.stat("vastra.fbCrossLine") > 0, "classify: cross-line fallback seen"
    )
    check(
        b.stat("vastra.fbDecisionRejected") > 0,
        "classify: base-mismatch (rejected at decision) fallback seen",
    )
    evidence(b, "classify")


def mode_store(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "store")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    check(
        b.stat("vastra.rejStoreConflict") > 0,
        "store: conflicting older store rejected at the decision (%d)"
        % b.stat("vastra.rejStoreConflict"),
    )
    check(
        at.get(pcs["va_st"], 0) == 0,
        "store: the load behind a conflicting store never bypasses",
    )
    info(
        "store: control load bypassed %d times, unknown-address rejections "
        "%d, LSQ rescan fallbacks %d"
        % (
            at.get(pcs["va_ct"], 0),
            b.stat("vastra.rejStoreUnknown"),
            b.stat("vastra.fbOlderStore"),
        )
    )
    evidence(b, "store")


def mode_discard(d):
    p, b = pair(d, "discard")
    execs = evidence(b, "discard")
    with open(b.trace, errors="replace") as f:
        text = f.read()
    retired = set(
        int(x) for x in re.findall(r"VBypass retired exec: (\d+)", text)
    )
    lost = [e for e in execs if e not in retired]
    info(
        "discard: %d bypassed loads were discarded before retiring" % len(lost)
    )
    check(
        len(lost)
        == b.stat("vastra.syntheticResponses")
        - b.stat("vastra.candidatesBypassedRetired")
        and len(lost)
        <= b.stat("vastra.candidatesDiscarded")
        + b.stat("vastra.candidatesFaulted"),
        "discard: bypassed loads that did not retire (%d) = synthetic "
        "responses - bypassed retired, and are counted as discarded or "
        "faulted, never as retired" % len(lost),
    )
    info(
        "discard: candidatesDiscarded P %d B %d"
        % (
            p.stat("vastra.candidatesDiscarded"),
            b.stat("vastra.candidatesDiscarded"),
        )
    )


def mode_fault(d):
    p, b = pair(d, "fault")
    check(
        b.stat("vastra.candidatesFaulted")
        == p.stat("vastra.candidatesFaulted"),
        "fault: candidates that faulted are the same (%d)"
        % b.stat("vastra.candidatesFaulted"),
    )
    evidence(b, "fault", need=False)


def mode_syscall(d):
    p, b = pair(d, "syscall")
    check(
        b.stat("vcache.syscallFlushes") > 0
        and b.stat("vcache.syscallFlushes") == p.stat("vcache.syscallFlushes"),
        "syscall: V$ flushed at committed system calls (%d)"
        % b.stat("vcache.syscallFlushes"),
    )
    ecalls = sum(1 for l in commits(p.trace) if " ecall" in l)
    check(
        ecalls > 0 and b.stat("vcache.syscallFlushes") == ecalls,
        "syscall: one flush per committed ecall (%d ecalls in the "
        "trace, %d flushes)" % (ecalls, b.stat("vcache.syscallFlushes")),
    )
    check(
        b.stat("vcache.syscallFlushBytes") > 0,
        "syscall: valid bytes were dropped (%d bytes, %d entries)"
        % (
            b.stat("vcache.syscallFlushBytes"),
            b.stat("vcache.syscallFlushEntries"),
        ),
    )
    check(
        b.stat("vastra.syntheticResponses") > 0,
        "syscall: the V$ supplied values before the system call (%d)"
        % b.stat("vastra.syntheticResponses"),
    )
    # The read system call is the one that follows the first probe pass:
    # no bypass of a probe load may follow it until the V$ is refilled
    with open(b.trace, errors="replace") as f:
        lines = f.readlines()
    flushes = [i for i, l in enumerate(lines) if "VBypass syscall flush" in l]
    byp = [i for i, l in enumerate(lines) if "VBypass exec:" in l]
    check(
        len(flushes) >= 2,
        "syscall: flush lines in the trace (%d)" % len(flushes),
    )
    # the probe pass after the read: the last 24 bypass-eligible probe loads
    # find a flushed V$.  Between the flush of the read and the first load
    # that refills an entry, no VBypass exec of the probe PC with an old
    # value may appear: the checksum equality above is the decisive check
    # (a stale bypass returns an old value, "errors" counts it)
    errs = program_lines(b.out)
    check(
        "errors 0" in errs,
        "syscall: after the read system call every " "load saw the new value",
    )
    evidence(b, "syscall")


M64 = (1 << 64) - 1


def old_val(i):
    return (i * 0x0101010101010101 + 0x1111) & M64


def new_val(i):
    return (i * 0x0F0F0F0F0F0F0F0F + 0x7777) & M64


def le_hex(v):
    return "".join("%02x" % ((v >> (8 * k)) & 0xFF) for k in range(8))


def mode_syscall_negative(d):
    p, b, n = Run(d, "P"), Run(d, "B"), Run(d, "N")
    x_rc = int(open(os.path.join(d, "X.rc")).read())
    # 1. controls: bypass off, and bypass on with the flush
    check(
        p.rc == 0 and program_lines(p.out)[0] == "errors 0",
        "negative: P (bypass off) is correct",
    )
    check(
        b.rc == 0 and program_lines(b.out) == program_lines(p.out),
        "negative: B (bypass on, flush) is correct and identical to P",
    )
    check(
        b.stat("vcache.syscallFlushes") > 0
        and b.stat("vcache.syscallFlushesSkipped") == 0
        and b.stat("vastra.syntheticResponses") > 0,
        "negative: B flushed (%d) and bypassed (%d)"
        % (
            b.stat("vcache.syscallFlushes"),
            b.stat("vastra.syntheticResponses"),
        ),
    )
    # 2. N: flush suppressed, bypass on: stale bytes are consumed
    check(n.rc == 0, "negative: N runs to completion")
    check(
        n.stat("vcache.syscallFlushes") == 0
        and n.stat("vcache.syscallFlushesSkipped")
        == b.stat("vcache.syscallFlushes"),
        "negative: N suppressed every flush (%d skipped, 0 flushed)"
        % n.stat("vcache.syscallFlushesSkipped"),
    )
    lines = program_lines(n.out)
    errs = [int(l.split()[1]) for l in lines if l.startswith("errors")]
    check(
        errs and errs[0] > 0 and errs[0] < 1000,
        "negative: N's own check counts stale loads (errors %s)" % errs,
    )
    check(
        lines != program_lines(p.out),
        "negative: N's output differs from P's (%s vs %s)"
        % (lines, program_lines(p.out)),
    )
    # 3. the specific mechanism: bypass lines after the read system call
    # whose data is the OLD value of a doubleword the read overwrote
    with open(n.trace, errors="replace") as f:
        tl = f.readlines()
    skipped = [i for i, l in enumerate(tl) if "flush SKIPPED" in l]
    read_at = skipped[2] if len(skipped) >= 3 else len(tl)
    stale, bases = [], set()
    olds = {le_hex(old_val(i)): i for i in range(24)}
    news = {le_hex(new_val(i)): i for i in range(24)}
    after_new = 0
    for ln in tl[read_at:]:
        m = re.search(
            r"VBypass exec: \d+ pc: 0x[0-9a-f]+ vaddr: (0x[0-9a-f]+)"
            r" size: 8 data: ([0-9a-f]{16}) ",
            ln,
        )
        if not m:
            continue
        va, data = int(m.group(1), 16), m.group(2)
        if data in olds:
            stale.append((va, olds[data]))
            bases.add(va - 8 * olds[data])
        elif data in news:
            after_new += 1
    check(
        len(stale) == errs[0] and len(bases) == 1,
        "negative: %d bypassed loads after the read returned an OLD "
        "value of the overwritten buffer (one consistent base %s), "
        "equal to the program's error count %d"
        % (len(stale), [hex(x) for x in bases], errs[0]),
    )
    check(after_new == 0, "negative: no bypassed load returned a NEW value")
    # the committed traces first differ at a probe load: P read NEW(i), N
    # committed OLD(i) for the same i
    cp, cn = commits(p.trace), commits(n.trace)
    first = next(
        (k for k in range(min(len(cp), len(cn))) if cp[k] != cn[k]), None
    )
    ok = False
    if first is not None:
        mp = re.search(r"D=0x([0-9a-f]{16})", cp[first])
        mn = re.search(r"D=0x([0-9a-f]{16})", cn[first])
        if mp and mn and " ld " in cp[first]:
            vp, vn = int(mp.group(1), 16), int(mn.group(1), 16)
            ok = any(vp == new_val(i) and vn == old_val(i) for i in range(24))
    check(
        ok,
        "negative: the committed traces first differ at a load "
        "(line %s): P committed NEW(i), N committed OLD(i)" % first,
    )
    # 4. X: strict verification detects the same hazard
    xo = open(os.path.join(d, "X.out"), errors="replace").read()
    m = re.search(
        r"candidate differs from the load.*candidate: "
        r"([0-9a-f]{16}) actual: ([0-9a-f]{16})",
        xo,
    )
    ok = (
        bool(m)
        and x_rc != 0
        and m.group(1) in olds
        and m.group(2) in news
        and olds[m.group(1)] == news[m.group(2)]
    )
    check(
        ok,
        "negative: X (strict verification, flush suppressed) stops "
        "with candidate=OLD(i), actual=NEW(i) for the same i "
        "(rc %d, %s)" % (x_rc, m.groups() if m else None),
    )


K = 0x9E3779B97F4A7C15


def commit_loads(trace, pc):
    """(effective address, result) of every committed load at pc"""
    out = []
    for line in commits(trace):
        m = re.match(
            r"system\.cpu: 0x([0-9a-f]+)\s+:.*D=0x([0-9a-f]+)"
            r"\s+A=0x([0-9a-f]+)",
            line,
        )
        if m and int(m.group(1), 16) == pc:
            out.append((int(m.group(3), 16), int(m.group(2), 16)))
    return out


def trace_events(path, pattern):
    ev = []
    with open(path, errors="replace") as f:
        for line in f:
            m = re.search(pattern, line)
            if m:
                ev.append(m.groups())
    return ev


def mode_atomic(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "atomic")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    conf = b.stat("vastra.rejStoreConflict")
    check(
        conf >= 10,
        "atomic: the older in-flight amoadd is a store conflict "
        "at the decision (rejStoreConflict %d)" % conf,
    )
    check(
        at.get(pcs["bp_acl"], 0) == 0,
        "atomic: the load behind the overlapping amoadd is never "
        "bypassed (%d)" % at.get(pcs["bp_acl"], 0),
    )
    check(
        at.get(pcs["bp_acc"], 0) > 0,
        "atomic: the control (amoadd on a neighbouring doubleword) does "
        "bypass (%d)" % at.get(pcs["bp_acc"], 0),
    )
    # the architectural result: the amoadd's effect is seen by the load
    loads = commit_loads(b.trace, pcs["bp_acl"])
    ok = len(loads) == 16
    for a, v in loads:
        off = a - (a & ~0xFFF)
        ok &= v == (((off * K) + 3 + 1) & M64)
    check(
        ok,
        "atomic: each of %d loads after the amoadd returns the "
        "initial doubleword + 1 (the atomic's result)" % len(loads),
    )
    evidence(b, "atomic")


def mode_abandon(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "abandon")
    ab = b.stat("vastra.bypassAbandoned_")
    check(
        ab >= 15,
        "abandon: candidates whose V$ entry was replaced between "
        "the decision and the send were abandoned (%d)" % ab,
    )
    # each abandoned load went to memory: a request sent, one memory
    # response, never a synthetic response
    execs = [
        int(x[0])
        for x in trace_events(b.trace, r"VBypass abandoned exec: (\d+) ")
    ]
    bypassed = set(e for e, _ in b.bypass_pcs())
    ids = {}
    key = re.compile(r"/(\d+)\.(\d+) pc:")
    kinds = (
        ("Trying to send request", "send"),
        ("Received response packet inst", "recv"),
        ("Memory response inst", "resp"),
        ("Deleting load request", "del"),
    )
    with open(b.trace, errors="replace") as f:
        for line in f:
            for text, k in kinds:
                if text in line:
                    m = key.search(line)
                    if m:
                        c = ids.setdefault(int(m.group(2)), {})
                        c[k] = c.get(k, 0) + 1
    bad = [
        e
        for e in execs
        if e in bypassed
        or not (
            ids.get(e, {}).get("send", 0) >= 1
            and ids.get(e, {}).get("recv", 0) == 1
            and ids.get(e, {}).get("resp", 0) == 1
            and ids.get(e, {}).get("del", 0) == 1
        )
    ]
    check(
        len(execs) == ab and not bad,
        "abandon: all %d abandoned loads sent a real memory request, got "
        "one response and completed once (%d violations)"
        % (len(execs), len(bad)),
    )
    loads = commit_loads(b.trace, pcs["bp_abc"])
    ok = len(loads) == 24
    for a, v in loads:
        off = a - (a & ~0xFFF)
        ok &= v == (((off * K) + 7) & M64)
    check(
        ok,
        "abandon: the %d candidate loads returned the memory values "
        "(abandoned or not)" % len(loads),
    )


def mode_order(d):
    """Source-grounded invariant (execute.cc, vastraScanStores comment): a
    load is decided only after every older memory reference has built its
    request, so an older store always has its address."""
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "order")
    check(
        b.stat("vastra.rejStoreUnknown") == 0
        and b.stat("vastra.fbOlderStore") == 0,
        "order: no candidate met an older store without an address "
        "(rejStoreUnknown %d, fbOlderStore %d)"
        % (b.stat("vastra.rejStoreUnknown"), b.stat("vastra.fbOlderStore")),
    )
    # order in which the requests were built (MinorMem trace): for every
    # candidate load the older store pushed its request first
    seq = []
    pat = re.compile(
        r"Setting state from NotIssued to InTranslation for "
        r"request: \S+/(\d+)\.(\d+) pc: (0x[0-9a-f]+) \((\w+)\)"
    )
    with open(b.trace, errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if m:
                seq.append((int(m.group(2)), int(m.group(3), 16), m.group(4)))
    st_pc, ld_pc = pcs["bp_ordst"], pcs["bp_ordc"]
    stores = [i for i, e in enumerate(seq) if e[1] == st_pc]
    loads = [i for i, e in enumerate(seq) if e[1] == ld_pc]
    ok = len(stores) == len(loads) == 24 and all(
        s < l and seq[s][0] < seq[l][0] for s, l in zip(stores, loads)
    )
    check(
        ok,
        "order: for each of %d iterations the older store (late "
        "data) built its request before the younger candidate load did"
        % len(loads),
    )


def mode_sub(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "sub")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    check(
        "errors 0" in program_lines(b.out),
        "sub: every extended value equals the expected one",
    )
    for n in ("bp_lb", "bp_lbu", "bp_lh", "bp_lhu", "bp_lw", "bp_lwu"):
        check(
            at.get(pcs[n], 0) > 0,
            "sub: %s candidates bypass (%d)" % (n, at.get(pcs[n], 0)),
        )
    evidence(b, "sub")


def mode_split(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "split")
    at = [pc for _, pc in b.bypass_pcs() if pc == pcs["bp_split"]]
    cand = b.stat("vastra.candidates")
    fb = b.stat("vastra.fbUnsupported")
    check(
        not at,
        "split: a load that crosses a 64 byte line is never " "bypassed",
    )
    check(
        fb > 0,
        "split: candidates fall back as unsupported (split "
        "request): fbUnsupported %d of %d candidates" % (fb, cand),
    )
    evidence(b, "split", need=False)


def mode_fp(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "fp")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    check(
        at.get(pcs["bp_fldp"], 0) > 0,
        "fp: fld candidates bypass (%d)" % at.get(pcs["bp_fldp"], 0),
    )
    check(
        at.get(pcs["bp_flwp"], 0) > 0,
        "fp: flw candidates bypass (%d)" % at.get(pcs["bp_flwp"], 0),
    )
    check(
        "errors 0" in program_lines(b.out),
        "fp: every loaded bit pattern (incl. NaN and a denormal; flw "
        "NaN-boxed, read back with fmv.x.w) equals the expected one",
    )
    # bypassed loads after the FP stores return the stored patterns
    d1 = {
        le_hex((0xC008000000000000 ^ (i * 0x0000000100010001)) & M64): i
        for i in range(24)
    }
    data = [
        x[1]
        for x in trace_events(
            b.trace,
            r"VBypass exec: \d+ pc: 0x[0-9a-f]+ vaddr: (0x[0-9a-f]+) "
            r"size: 8 data: ([0-9a-f]{16})",
        )
    ]
    check(
        any(x in d1 for x in data),
        "fp: bypassed fld loads after fsd stores return the stored "
        "doublewords (%d)" % sum(1 for x in data if x in d1),
    )
    fw = [
        x[0]
        for x in trace_events(
            b.trace,
            r"VBypass exec: \d+ pc: 0x[0-9a-f]+ vaddr: 0x[0-9a-f]+ "
            r"size: 4 data: ([0-9a-f]{8}) ",
        )
    ]
    f1 = {
        "%02x%02x%02x%02x"
        % tuple(
            ((0x3F000000 ^ ((i * 0x00010203) & 0xFFFFFFFF)) >> (8 * k)) & 0xFF
            for k in range(4)
        ): i
        for i in range(24)
    }
    check(
        any(x in f1 for x in fw),
        "fp: bypassed flw loads after fsw stores return the stored words "
        "(%d)" % sum(1 for x in fw if x in f1),
    )
    evidence(b, "fp")


def mode_synonym(d):
    s = os.path.join(d, "syn")
    p, b = pair(s, "synonym")
    vals = dict(
        re.findall(
            r"^(\w+) (\d+)$",
            open(os.path.join(s, "B.out"), errors="replace").read(),
            re.M,
        )
    )
    shared, old, vis = (
        int(vals.get(k, -1)) for k in ("shared", "old", "storedvisible")
    )
    check(
        vis == 24,
        "synonym: the stores through the second mapping took "
        "effect there (%d of 24)" % vis,
    )
    if shared == 0 and old == 24:
        info(
            "synonym: the two MAP_SHARED mappings are independent copies "
            "in SE mode (a store through one is not seen through the "
            "other: shared 0, old 24): no virtual synonym can be created "
            "this way, so the hazard is NOT reproducible and NOT tested "
            "here"
        )
        check(
            True,
            "synonym: mapping the same file twice yields two "
            "independent physical copies (documented SE limitation, "
            "mmapFunc)",
        )
    else:
        check(
            shared == 24 and old == 0,
            "synonym: mappings are shared: "
            "stale V$ values would be a failure (shared %d old %d)"
            % (shared, old),
        )
    for name, call in (
        ("shm", "shmget (#194)"),
        ("memfd", "memfd_create (#279)"),
    ):
        o = open(os.path.join(d, name, "B.out"), errors="replace").read()
        rc = int(open(os.path.join(d, name, "B.rc")).read())
        check(
            rc != 0 and "syscall %s unimplemented" % call in o,
            "synonym: %s is unimplemented in SE mode: gem5 stops with "
            "'syscall %s unimplemented' (rc %d)" % (name, call, rc),
        )
    info(
        "synonym: other ways to alias a page in SE mode: mremap moves the "
        "translation and erases the old one (EmulationPageTable::remap); "
        "clone/fork share the page table at the same virtual address"
    )


def mode_special(d):
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    p, b = pair(d, "special")
    at = {}
    for _, pc in b.bypass_pcs():
        at[pc] = at.get(pc, 0) + 1
    # nm gives the symbol address; the lr/amo is at +0 (lr) and +4 (amo,
    # after the li): the bypass PCs seen must be neither
    check(
        all(pc not in (pcs["bp_lr"], pcs["bp_amo"] + 4) for pc in at),
        "special: no bypass at the load-reserved or the atomic",
    )
    # the atomic add wrote the lines: it invalidated their V$ bytes, so the
    # ordinary loads that follow find them byte-invalid and go to memory
    check(
        b.stat("vastra.fbByteMiss") > 0,
        "special: ordinary loads after the atomics fall back, byte "
        "invalid (%d)" % b.stat("vastra.fbByteMiss"),
    )
    check(at.get(pcs["bp_probe0"], 0) == 0, "special: and are not bypassed")
    evidence(b, "special", need=False)


def walk_spans(path, pc):
    """Cycles (1000 ticks) from the first to the last bp_step load of each
    walk of the ring (the walks are 32, 32, 32 + lap nodes, 3 laps)"""
    t = []
    for line in open(path, errors="replace"):
        m = re.match(r"\s*(\d+): system.cpu: 0x([0-9a-f]+)\s+:\s+ld a0", line)
        if m and int(m.group(2), 16) == pc:
            t.append(int(m.group(1)))
    sizes = [32, 32, 32, 32, 32, 33, 32, 32, 34]
    assert len(t) == sum(sizes), (len(t), sum(sizes))
    spans, i = [], 0
    for s in sizes:
        spans.append((t[i + s - 1] - t[i]) // 1000)
        i += s
    return spans


def mode_lat(d):
    p, b = pair(d, "latency", ref_accesses=True)
    pcs = read_pcs(os.path.join(d, "pcs.txt"))
    pm = p.s["system.cpu.dcache.ReadReq.misses::total"]
    bm = b.s["system.cpu.dcache.ReadReq.misses::total"]
    check(bm < pm, "latency: data cache read misses %d -> %d" % (pm, bm))
    check(
        b.stat("vastra.syntheticResponses") > 0,
        "latency: loads were bypassed (%d)"
        % b.stat("vastra.syntheticResponses"),
    )
    # Whole-run numCycles is dominated by 3 MiB of stores whose timing moves
    # by up to 3% with the binary path and the run's layout, more than the
    # effect: it is reported, not asserted.  The walk spans are the
    # measurement: the walks after the eviction (1, 4, 7) must be faster,
    # the others must not change.
    sp = walk_spans(os.path.join(d, "PT", "trace.txt"), pcs["bp_step"])
    sb = walk_spans(os.path.join(d, "BT", "trace.txt"), pcs["bp_step"])
    saved = [x - y for x, y in zip(sp, sb)]
    info(
        "latency: numCycles %d -> %d (layout dependent, not asserted)"
        % (p.stat("numCycles"), b.stat("numCycles"))
    )
    info("latency: walk spans P %s" % sp)
    info("latency: walk spans B %s" % sb)
    check(
        all(saved[i] > 100 for i in (1, 4, 7)),
        "latency: each walk after the eviction is faster with bypass "
        "(saved %s cycles)" % [saved[i] for i in (1, 4, 7)],
    )
    check(
        all(abs(saved[i]) <= 5 for i in (0, 2, 3, 5, 6, 8)),
        "latency: the walks that hit in L1 do not change (%s)"
        % [saved[i] for i in (0, 2, 3, 5, 6, 8)],
    )
    check(
        sum(saved) > 0,
        "latency: %d cycles saved in all walks (%.2f%% "
        "of the walks' time)" % (sum(saved), 100.0 * sum(saved) / sum(sp)),
    )


def mode_lvp(d):
    p, b = Run(d, "P"), Run(d, "B")
    check(p.rc == 0 and b.rc == 0, "lvp: both runs exit 0")
    check(
        program_lines(p.out) == program_lines(b.out)
        and len(program_lines(b.out)) > 0,
        "lvp: checksum identical (%s)" % program_lines(b.out),
    )
    check(
        commits(p.trace) == commits(b.trace) and len(commits(p.trace)),
        "lvp: committed trace identical",
    )
    check(
        p.s["simInsts"] == b.s["simInsts"],
        "lvp: committed instructions " "identical (%d)" % b.s["simInsts"],
    )
    check(
        b.stat("lvp.incorrect") > 0,
        "lvp: wrong predictions and recovery still happen (%d)"
        % b.stat("lvp.incorrect"),
    )
    check(
        b.stat("lvp.specLoadsIssued") > 0,
        "lvp: speculative loads were issued (%d)"
        % b.stat("lvp.specLoadsIssued"),
    )
    check(
        b.stat("vastra.syntheticResponses") > 0,
        "lvp: V-ASTRA bypass ran in the same program (%d)"
        % b.stat("vastra.syntheticResponses"),
    )
    identity(b, "lvp B")
    info(
        "lvp: cycles %d -> %d; lvp correct/incorrect P %d/%d B %d/%d"
        % (
            p.stat("numCycles"),
            b.stat("numCycles"),
            p.stat("lvp.correct"),
            p.stat("lvp.incorrect"),
            b.stat("lvp.correct"),
            b.stat("lvp.incorrect"),
        )
    )


OUTCOMES = (
    "candidatesDiscarded",
    "candidatesFaulted",
    "rejUnsupported",
    "eaMismatch",
    "lookups",
    "candidatesBypassedRetired",
)


def mode_drain(d, how):
    """The contract of the candidate identity: in every statistics window
    candidates + carryIn = outcomes + inFlight, where carryIn is inFlight at
    the previous reset, so the windows chain: carryIn(k) = inFlight(k-1),
    carryIn(0) = 0, and over all windows sum(candidates) = sum(outcomes) +
    inFlight(last).  After a drain nothing is in flight (the run panics
    otherwise), so every window is closed by itself.  No value is ever
    negative."""
    p, b = Run(d, "P"), Run(d, "B")
    check(p.rc == 0 and b.rc == 0, "%s: both runs exit 0" % how)
    for r in (p, b):
        n = open(r.out, errors="replace").read().count("reset statistics")
        check(n >= 3, "%s: %s reset %d times" % (how, r.name, n))
    check(
        program_values(p.out) == program_values(b.out)
        and len(program_values(b.out)) > 0,
        "%s: program output identical " "(%s)" % (how, program_values(b.out)),
    )
    check(
        commits(p.trace) == commits(b.trace) and len(commits(p.trace)),
        "%s: committed trace identical" % how,
    )
    carried = 0
    for r in (p, b):
        ws = windows(os.path.join(r.d, r.name, "stats.txt"))
        pre = "system.cpu.vastra."
        ok_win = ok_chain = ok_pos = True
        tot_c = tot_o = 0
        prev_in = 0
        for w in ws:
            out = sum(w[pre + n] for n in OUTCOMES)
            ok_win &= (
                w[pre + "candidates"] + w[pre + "carryIn"]
                == out + w[pre + "inFlight"]
            )
            ok_chain &= w[pre + "carryIn"] == prev_in
            ok_pos &= all(
                v >= 0
                for k, v in w.items()
                if k.startswith(("system.cpu.vastra.", "system.cpu.vcache."))
            )
            tot_c += w[pre + "candidates"]
            tot_o += out
            prev_in = w[pre + "inFlight"]
            carried = max(carried, w[pre + "carryIn"])
        check(
            ok_win,
            "%s %s: identity holds in each of %d windows"
            % (how, r.name, len(ws)),
        )
        check(
            ok_pos, "%s %s: no vastra/vcache value is negative" % (how, r.name)
        )
        if how == "drain":
            check(
                all(w[pre + "inFlight"] == 0 for w in ws),
                "%s %s: nothing in flight at any window end" % (how, r.name),
            )
        else:
            check(
                ok_chain, "%s %s: carryIn(k) = inFlight(k-1)" % (how, r.name)
            )
        check(
            tot_c == tot_o + prev_in,
            "%s %s: over all windows candidates %d = outcomes %d + "
            "in flight at the end %d" % (how, r.name, tot_c, tot_o, prev_in),
        )
    check(
        len(b.bypass_pcs()) > 0,
        "%s: loads were bypassed in the run "
        "(%d in all windows)" % (how, len(b.bypass_pcs())),
    )
    if how == "reset":
        info("reset: largest carryIn seen in a window: %d" % carried)


def main():
    modes = {
        "classify": mode_classify,
        "store": mode_store,
        "discard": mode_discard,
        "fault": mode_fault,
        "syscall": mode_syscall,
        "latency": mode_lat,
        "lvp": mode_lvp,
        "atomic": mode_atomic,
        "abandon": mode_abandon,
        "order": mode_order,
        "sub": mode_sub,
        "split": mode_split,
        "fp": mode_fp,
        "synonym": mode_synonym,
        "syscall-negative": mode_syscall_negative,
        "special": mode_special,
        "drain": mode_drain,
    }
    modes[sys.argv[1]](*sys.argv[2:])
    if failures:
        print("%d correctness check(s) failed" % failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
