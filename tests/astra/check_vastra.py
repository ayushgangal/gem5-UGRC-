#!/usr/bin/env python3
"""Checks the outputs of run_vastra.sh.

Usage:
  check_vastra.py asm MODE VA_S SYMBOLS PCS_OUT
  check_vastra.py MODE OUT

MODE is classify, store, discard, fault, noninterference or
verify-detect.  OUT holds, for
each run NAME, the gem5 output directory OUT/NAME, and OUT/NAME.out and
OUT/NAME.rc with its output and exit status.  D has the diagnostic V-ASTRA
probe (src/cpu/minor/vastra.hh) off and S the same run with it on; ASTRA and
the shadow V$ are on in both.  V is S with strict verification
(enableVAstraVerify): a candidate that passed every check and is a byte-valid
V$ hit must equal the bytes the real load returned or the run stops.  V must
differ from S in nothing but vastra.verifiedHits, and every byte-valid hit in
it must have been verified (verifiedHits = candidateCorrect, and
candidateWrong = 0).

The probe must change nothing, and what it reports must be what ASTRA, the
V$ and the real load say.  For every mode:

  - non-interference: D and S have the same checksum, committed instruction
    count, committed trace and every statistic except vastra.*, cycles
    included (so also every ASTRA, V$ and LVP statistic);
  - the log is replayed on a model: ASTRA's own log gives the predicted and
    the real base of each load, the V$ log (the retiring loads and stores)
    gives the state of the V$ at the moment of each decision, and the
    disassembly gives the immediate.  Each decision, the predicted address
    (base + immediate), the older stores still in flight (stores whose
    retirement is logged after the decision), the copy of the V$ entry and
    the terminal outcome (against the raw bytes the load really read) must
    agree with the probe's lines, and every vastra.* statistic with the
    replay's count;
  - the identities of vastra.hh hold.

Each mode adds assertions about its own program (the sites named in
vastra.c).  Like the other ASTRA and V$ checkers, the model is written from
the specification (vastra.hh) by the same author as the code: a pass shows
that the code does what the specification says.
"""

import bisect
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ca = _load("check_astra")
cv = _load("check_vcache")

fails = []
MASK64 = (1 << 64) - 1
MAX_LOAD_BYTES = 8  # vastra.hh: the size of a load is not known at decision


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


# ---------------------------------------------------------------- assembly

EXPECTED_SITES = {
    "classify": ["va_hit", "va_bw", "va_tm", "va_bm", "va_x"],
    "store": ["va_st", "va_ct"],
    "discard": ["va_disc"],
    "fault": ["va_f"],
}
LOAD_OPERAND = re.compile(r"^\w+,(-?\d+)\((\w+)\)$")
INSN_LINE = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)$")


def asm_main(mode, disasm, symbols, pcs_out):
    syms = {}
    with open(symbols) as f:
        for line in f:
            t = line.split()
            if len(t) >= 3:
                syms[t[-1]] = int(t[0], 16)
    insns = {}
    for line in cv.read_text(disasm).splitlines():
        m = INSN_LINE.match(line)
        if m:
            insns[int(m.group(1), 16)] = (
                m.group(2),
                m.group(3).split("#")[0].strip(),
            )
    problems = []
    for name in EXPECTED_SITES[mode]:
        if name not in syms:
            problems.append("symbol %s not found" % name)
            continue
        mnem, op = insns.get(syms[name], ("?", ""))
        if mnem != "ld" or not LOAD_OPERAND.match(op):
            problems.append(
                "%s is not a load with a base and an immediate" % name
            )
    if any(m.startswith("c.") for m, _ in insns.values()):
        problems.append("compressed instructions")
    for p in problems:
        print("FAIL: " + p)
    if problems:
        return 1
    with open(pcs_out, "w") as f:
        for n in EXPECTED_SITES[mode]:
            f.write("%s 0x%x\n" % (n, syms[n]))
    print("PASS: the %s program has the probed loads" % mode)
    return 0


def read_pcs(directory):
    pcs = {}
    with open(os.path.join(directory, "pcs.txt")) as f:
        for line in f:
            n, v = line.split()
            pcs[n] = int(v, 16)
    return pcs


def immediates(disasm_path):
    """{pc: immediate} of the loads of a disassembly"""
    imm = {}
    for line in cv.read_text(disasm_path).splitlines():
        m = INSN_LINE.match(line)
        if not m:
            continue
        o = LOAD_OPERAND.match(m.group(3).split("#")[0].strip())
        if o and (m.group(2).startswith("l") or m.group(2).startswith("fl")):
            imm[int(m.group(1), 16)] = int(o.group(1))
    return imm


# ----------------------------------------------------------------- reading

DECIDE_RE = re.compile(
    r"VAstra decide exec: (\d+) pc: 0x([0-9a-f]+) result: (\w+) pred_base: "
    r"0x([0-9a-f]+) actual_base: 0x([0-9a-f]+) pred_ea: 0x([0-9a-f]+)\s*$"
)
OUTCOME_RE = re.compile(
    r"VAstra outcome exec: (\d+) pc: 0x([0-9a-f]+) outcome: (\w+)"
    r"(?: vaddr: 0x([0-9a-f]+) size: (\d+) actual: (\S+) candidate: (\S+))?"
    r"\s*$"
)


def read_trace(path):
    """The log in order: ('vc', event) for a V$ line, ('astra', event) for an
    ASTRA line, ('dec', decision) and ('out', outcome) for the probe's, and
    the committed instruction lines.  None if it cannot be read."""
    seq, insns, problems = [], [], []
    text = cv.read_text(path)
    if not text:
        return None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if "VAstra " in line:
            m = DECIDE_RE.search(line)
            if m:
                g = m.groups()
                seq.append(
                    (
                        "dec",
                        {
                            "exec": int(g[0]),
                            "pc": int(g[1], 16),
                            "result": g[2],
                            "pred_base": int(g[3], 16),
                            "actual_base": int(g[4], 16),
                            "pred_ea": int(g[5], 16),
                        },
                    )
                )
                continue
            m = OUTCOME_RE.search(line)
            if m:
                g = m.groups()
                o = {"exec": int(g[0]), "pc": int(g[1], 16), "outcome": g[2]}
                if g[3] is not None:
                    o.update(
                        vaddr=int(g[3], 16),
                        size=int(g[4]),
                        actual=g[5],
                        candidate=g[6],
                    )
                seq.append(("out", o))
                continue
            problems.append("line %d: malformed VAstra line" % n)
        elif "VCache " in line:
            ev = None
            m = cv.LOAD_LINE.search(line)
            if m:
                g = m.groups()
                ev = {
                    "kind": "load",
                    "exec": int(g[0]),
                    "pc": int(g[1], 16),
                    "vaddr": int(g[2], 16),
                    "size": int(g[3]),
                    "data": bytes.fromhex(g[4]),
                }
            m = m or cv.STORE_LINE.search(line)
            if ev is None and m:
                g = m.groups()
                ev = {
                    "kind": "store",
                    "exec": int(g[0]),
                    "pc": int(g[1], 16),
                    "vaddr": int(g[2], 16),
                    "size": int(g[3]),
                    "data": bytes.fromhex(g[4]),
                }
            if ev is None:
                m = cv.OTHER_LINE.search(line)
                if m:
                    g = m.groups()
                    ev = {
                        "kind": "other",
                        "exec": int(g[0]),
                        "pc": int(g[1], 16),
                        "vaddr": int(g[2], 16),
                        "size": int(g[3]),
                        "what": g[4],
                        "invalidated": int(g[5]),
                    }
            if ev is None:
                problems.append("line %d: malformed VCache line" % n)
            else:
                seq.append(("vc", ev))
        elif ca.ASTRA_EVENT.search(line):
            ev = ca.parse_event(line)
            if ev is None:
                problems.append("line %d: malformed ASTRA line" % n)
            else:
                seq.append(("astra", ev))
        elif ca.INSN_RE.search(line):
            insns.append(line)
        else:
            problems.append("line %d: not a known line" % n)
    return {"seq": seq, "insns": insns, "problems": problems}


class Run:
    pass


def load_run(out, name, diag, verify=False):
    r = Run()
    r.name = name
    try:
        r.rc = int(cv.read_text(os.path.join(out, name + ".rc")).strip())
    except ValueError:
        r.rc = None
    check(r.rc == 0, "%s: gem5 exit status 0 (got %s)" % (name, r.rc))
    try:
        r.stats = ca.read_stats(out, name)
    except OSError:
        r.stats = None
    r.params = cv.cpu_params(out, name)
    r.tr = read_trace(os.path.join(out, name, "trace.txt"))
    ok = r.rc == 0 and r.stats is not None and r.params and r.tr
    check(
        bool(ok), "%s: statistics, configuration and trace can be read" % name
    )
    if not ok:
        return None
    check(
        r.params.get("enableAstra") == "true"
        and r.params.get("enableVCache") == "true"
        and r.params.get("enableVAstraDiag") == ("true" if diag else "false")
        and r.params.get("enableVAstraVerify")
        == ("true" if verify else "false"),
        "%s: ASTRA and the V$ are on, the probe is %s, strict verification "
        "is %s" % (name, "on" if diag else "off", "on" if verify else "off"),
    )
    check(
        r.params.get("threadPolicy") == "SingleThreaded"
        and r.params.get("executeMaxAccessesInMemory") == "16"
        and r.params.get("executeLSQTransfersQueueSize") == "16",
        "%s: research baseline settings" % name,
    )
    has = any("cpu.vastra." in k for k in r.stats)
    check(
        has == diag,
        "%s: vastra.* statistics %s" % (name, "present" if diag else "absent"),
    )
    check(
        not r.tr["problems"],
        "%s: the trace has only known lines (%d others)"
        % (name, len(r.tr["problems"])),
    )
    m = re.search(
        r"^checksum (-?\d+)\s*$",
        cv.read_text(os.path.join(out, name + ".out")),
        re.M,
    )
    r.checksum = int(m.group(1)) if m else None
    r.insts = int(float(r.stats["simInsts"]))
    r.cycles = int(float(r.stats["system.cpu.numCycles"]))
    r.vs = cv.counters(r.stats, "vastra")
    return r


def verification(s, v, label):
    """V is S with strict verification: nothing may differ, and every
    byte-valid hit was verified"""
    check(
        s.checksum == v.checksum
        and s.insts == v.insts
        and s.tr["insns"] == v.tr["insns"],
        "%s: strict verification: same checksum, instruction count and "
        "committed trace" % label,
    )
    ns = {k: x for k, x in s.stats.items() if "vastra.verifiedHits" not in k}
    nv = {k: x for k, x in v.stats.items() if "vastra.verifiedHits" not in k}
    check(
        ns == nv,
        "%s: strict verification: every statistic is identical, cycles "
        "included (%d, %d), except vastra.verifiedHits"
        % (label, s.cycles, v.cycles),
    )
    check(
        s.vs["verifiedHits"] == 0
        and v.vs["verifiedHits"] == v.vs["candidateCorrect"]
        and v.vs["candidateWrong"] == 0,
        "%s: strict verification checked every byte-valid hit: %d "
        "verified = %d correct, %d wrong"
        % (
            label,
            v.vs["verifiedHits"],
            v.vs["candidateCorrect"],
            v.vs["candidateWrong"],
        ),
    )


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
    nd = {k: v for k, v in d.stats.items() if "cpu.vastra." not in k}
    ns = {k: v for k, v in s.stats.items() if "cpu.vastra." not in k}
    check(
        nd == ns,
        "%s: identical statistics apart from vastra.*, cycles included "
        "(%d, %d)" % (label, d.cycles, s.cycles),
    )
    check(
        not any(k == "dec" or k == "out" for k, _ in d.tr["seq"]),
        "%s: D logs no probe line" % label,
    )


# ------------------------------------------------------------------ replay

COUNT_NAMES = [
    "predAvailable",
    "metadataMissing",
    "baseWrong",
    "baseCorrect",
    "rejStoreConflict",
    "rejStoreUnknown",
    "candidates",
    "candidatesDiscarded",
    "candidatesFaulted",
    "rejUnsupported",
    "eaMismatch",
    "lookups",
    "tagHits",
    "tagMisses",
    "byteValidHits",
    "byteInvalidMisses",
    "candidateCorrect",
    "candidateWrong",
]


def snapshot(vm, ea):
    i = cv.index_of(ea)
    if vm.occ[i] and vm.tag[i] == cv.tag_of(ea):
        return True, vm.valid[i], bytes(vm.data[i])
    return False, 0, bytes(16)


def overlaps(store, ea):
    return (
        store["vaddr"] < ea + MAX_LOAD_BYTES
        and ea < store["vaddr"] + store["size"]
    )


def analyze(r, imm, label):
    """Replays the log of a run with the probe on.  Returns the records of
    the decisions: {exec: dict}, in order"""
    seq = r.tr["seq"]
    vc_pos, vc_by_exec = {}, {}
    for pos, (k, e) in enumerate(seq):
        if k == "vc":
            vc_pos[e["exec"]] = pos
            vc_by_exec[e["exec"]] = e
    astra_pred, astra_base, astra_out = {}, {}, {}
    store_pos = [
        vc_pos[x["exec"]]
        for x in sorted(vc_by_exec.values(), key=lambda x: vc_pos[x["exec"]])
        if x["kind"] == "store"
        or (x["kind"] == "other" and not x["what"].endswith("_load"))
    ]
    store_ev = [
        vc_by_exec[ex]
        for ex in sorted(
            (
                x["exec"]
                for x in vc_by_exec.values()
                if x["kind"] == "store"
                or (x["kind"] == "other" and not x["what"].endswith("_load"))
            ),
            key=lambda ex: vc_pos[ex],
        )
    ]
    vm = cv.VModel()
    n = {k: 0 for k in COUNT_NAMES}
    recs, cands = {}, {}
    bad = []

    def is_store(e):
        return e["kind"] == "store" or (
            e["kind"] == "other" and not e["what"].endswith("_load")
        )

    for pos, (k, e) in enumerate(seq):
        if k == "vc":
            if e["kind"] == "load":
                vm.load(e["vaddr"], e["data"])
            elif e["kind"] == "store":
                vm.store(e["vaddr"], e["data"])
            else:
                vm.other(e["what"], e["vaddr"], e["size"])
        elif k == "astra":
            if e["kind"] == "lookup":
                astra_pred[e["key"][1]] = e["pred"]
            elif e["kind"] == "base":
                astra_base[e["key"][1]] = e["base"]
            elif e["kind"] == "outcome":
                astra_out[e["key"][1]] = e["outcome"]
        elif k == "dec":
            ex = e["exec"]
            n["predAvailable"] += 1
            rec = {
                "pc": e["pc"],
                "result": e["result"],
                "exec": ex,
                "outcome": None,
                "note": {},
            }
            recs[ex] = rec
            pred, base = astra_pred.get(ex), astra_base.get(ex)
            if e["pred_base"] != pred:
                bad.append("exec %d: predicted base differs from ASTRA's" % ex)
            off = imm.get(e["pc"])
            if e["result"] == "metadata":
                n["metadataMissing"] += 1
                if base is not None:
                    bad.append("exec %d: metadata for a load with a base" % ex)
                continue
            if base is None or e["actual_base"] != base:
                bad.append("exec %d: actual base differs from ASTRA's" % ex)
                continue
            if off is None:
                bad.append("exec %d: no immediate in the disassembly" % ex)
                continue
            retire = vc_by_exec.get(ex)
            if pred != base:
                n["baseWrong"] += 1
                if e["result"] != "basewrong":
                    bad.append(
                        "exec %d: base wrong but %s" % (ex, e["result"])
                    )
                # what a V$ lookup would have found: at the predicted
                # address, and at the real one
                if retire is not None and retire["kind"] == "load":
                    size = retire["size"]
                    for tag, a in (
                        ("pred", (pred + off) & MASK64),
                        ("real", (base + off) & MASK64),
                    ):
                        hit, valid, data = snapshot(vm, a)
                        o = a & 15
                        want = ((1 << size) - 1) << o
                        full = (
                            hit and cv.fits(a, size) and valid & want == want
                        )
                        rec["note"][tag] = (
                            full,
                            full and data[o : o + size] == retire["data"],
                        )
                continue
            n["baseCorrect"] += 1
            ea = (base + off) & MASK64
            if e["pred_ea"] != ea:
                bad.append(
                    "exec %d: predicted address %#x, expected %#x"
                    % (ex, e["pred_ea"], ea)
                )
            # the older stores whose retirement is logged after this
            # decision: still in flight (retirement is in order, so they
            # follow this position in the log in order of age)
            stores = []
            for x in store_ev[bisect.bisect_right(store_pos, pos) :]:
                if x["exec"] >= ex:
                    break
                stores.append(x)
            rec["inflight_stores"] = stores
            conflict = any(overlaps(x, ea) for x in stores)
            want_res = "storeconflict" if conflict else "candidate"
            if e["result"] == "storeunknown":
                n["rejStoreUnknown"] += 1
                continue
            if e["result"] != want_res:
                bad.append(
                    "exec %d: result %s, the log of the older stores "
                    "says %s" % (ex, e["result"], want_res)
                )
            if conflict:
                n["rejStoreConflict"] += 1
                continue
            n["candidates"] += 1
            hit, valid, data = snapshot(vm, ea)
            cands[ex] = {"ea": ea, "hit": hit, "valid": valid, "data": data}
        else:  # outcome
            ex = e["exec"]
            rec = recs.get(ex)
            c = cands.pop(ex, None)
            if rec is None or c is None:
                bad.append("exec %d: an outcome without a candidate" % ex)
                continue
            rec["outcome"] = e["outcome"]
            if e["outcome"] in ("discarded", "faulted"):
                n["candidates" + e["outcome"].capitalize()] += 1
                want_astra = (
                    "squashed" if e["outcome"] == "discarded" else "faulted"
                )
                if astra_out.get(ex) != want_astra:
                    bad.append(
                        "exec %d: %s but ASTRA says %s"
                        % (ex, e["outcome"], astra_out.get(ex))
                    )
                if ex in vc_by_exec:
                    bad.append(
                        "exec %d: %s but it retired" % (ex, e["outcome"])
                    )
                continue
            ev = vc_by_exec.get(ex)
            if ev is None:
                bad.append("exec %d: an outcome but no retirement" % ex)
                continue
            if ev["kind"] != "load":
                want, want_cand = "unsupported", "-"
                n["rejUnsupported"] += 1
            elif c["ea"] != ev["vaddr"]:
                want, want_cand = "eamismatch", "-"
                n["eaMismatch"] += 1
            else:
                o = ev["vaddr"] & 15
                size = ev["size"]
                need = ((1 << size) - 1) << o
                n["lookups"] += 1
                want_cand = c["data"][o : o + size].hex() if c["hit"] else "-"
                if not c["hit"]:
                    want = "tagmiss"
                    n["tagMisses"] += 1
                else:
                    n["tagHits"] += 1
                    if c["valid"] & need != need:
                        want = "bytemiss"
                        n["byteInvalidMisses"] += 1
                    else:
                        n["byteValidHits"] += 1
                        if c["data"][o : o + size] == ev["data"]:
                            want = "correct"
                            n["candidateCorrect"] += 1
                        else:
                            want = "wrong"
                            n["candidateWrong"] += 1
            if e["outcome"] != want:
                bad.append(
                    "exec %d: outcome %s, expected %s"
                    % (ex, e["outcome"], want)
                )
            if ev["kind"] == "load" and (
                e["actual"] != ev["data"].hex() or e["candidate"] != want_cand
            ):
                bad.append(
                    "exec %d: bytes %s/%s, expected %s/%s"
                    % (
                        ex,
                        e["actual"],
                        e["candidate"],
                        ev["data"].hex(),
                        want_cand,
                    )
                )
    check(
        not cands,
        "%s: every candidate has its one terminal outcome (%d "
        "without)" % (label, len(cands)),
    )
    check(
        not bad,
        "%s: every decision, address, in-flight store, V$ copy "
        "and outcome agrees with the replay (%d problems)%s"
        % (label, len(bad), (": " + bad[0]) if bad else ""),
    )
    differ = {
        k: (n[k], r.vs.get(k)) for k in COUNT_NAMES if n[k] != r.vs.get(k)
    }
    check(
        not differ,
        "%s: every vastra.* statistic equals the replay's "
        "count%s" % (label, (": %s" % differ) if differ else ""),
    )
    return recs, n


def identities(r, label):
    v = r.vs
    check(
        v["predAvailable"]
        == v["metadataMissing"] + v["baseWrong"] + v["baseCorrect"],
        "%s: predAvailable = metadata + base wrong + base correct" % label,
    )
    check(
        v["baseCorrect"]
        == v["rejStoreConflict"] + v["rejStoreUnknown"] + v["candidates"],
        "%s: base correct = store conflict + store unknown + candidates"
        % label,
    )
    check(
        v["candidates"] + v["carryIn"]
        == v["candidatesDiscarded"]
        + v["candidatesFaulted"]
        + v["rejUnsupported"]
        + v["eaMismatch"]
        + v["lookups"]
        + v["inFlight"],
        "%s: candidates + carry-in = discarded + faulted + unsupported + "
        "address mismatch + lookups + in flight" % label,
    )
    check(
        v["lookups"] == v["tagHits"] + v["tagMisses"]
        and v["tagHits"] == v["byteInvalidMisses"] + v["byteValidHits"]
        and v["byteValidHits"] == v["candidateCorrect"] + v["candidateWrong"],
        "%s: lookups = tag hits + misses; tag hits = byte misses + "
        "byte-valid hits; byte-valid hits = correct + wrong" % label,
    )
    check(
        v["inFlight"] == 0 and v["carryIn"] == 0,
        "%s: no candidate in flight at the end, none carried in" % label,
    )
    check(
        v["rejStoreUnknown"] == 0,
        "%s: no older store without an address was ever seen: Minor "
        "sends memory operations to the LSQ in order, so every older "
        "store has computed its address (and the unknown-address case "
        "cannot be produced; see README)" % label,
    )


def at(recs, pc):
    return [r for _, r in sorted(recs.items()) if r["pc"] == pc]


def count(rs, field, value):
    return sum(1 for r in rs if r[field] == value)


# ------------------------------------------------------------------- modes


def program_mode(out, mode, expect):
    pcs = read_pcs(out)
    imm = immediates(os.path.join(out, "va.S"))
    section("Preflight and non-interference")
    d = load_run(out, "D", False)
    s = load_run(out, "S", True)
    v = load_run(out, "V", True, True)
    if d is None or s is None or v is None:
        return None
    noninterference(d, s, mode)
    verification(s, v, mode)
    section("Replay of the probe")
    recs, n = analyze(s, imm, mode)
    analyze(v, imm, mode + " (verified run)")
    identities(s, mode)
    expect(s, recs, pcs)
    report("probe: " + ", ".join("%s %d" % (k, s.vs[k]) for k in COUNT_NAMES))
    return finish()


def classify_expect(s, recs, pcs):
    section("The cases")
    hit, bw, tm, bm, x = (
        at(recs, pcs[n]) for n in ("va_hit", "va_bw", "va_tm", "va_bm", "va_x")
    )
    for name, rs, total in (
        ("hit", hit, 24),
        ("tagmiss", tm, 24),
        ("bytemiss", bm, 24),
        ("cross", x, 24),
        ("basewrong", bw, 14),
    ):
        check(
            len(rs) == total,
            "%s: %d probed loads reached the decision "
            "(got %d)" % (name, total, len(rs)),
        )
    slack = 4  # ASTRA needs a few iterations to learn the stride
    check(
        count(hit, "outcome", "correct") >= 24 - slack
        and count(hit, "outcome", "wrong") == 0,
        "hit: correct base, tag and bytes valid, the candidate bytes equal "
        "the load's: %d of 24 (none wrong)" % count(hit, "outcome", "correct"),
    )
    check(
        count(tm, "outcome", "tagmiss") >= 24 - slack
        and count(tm, "outcome", "correct") == 0,
        "tagmiss: a correct base finds no tag: %d of 24"
        % count(tm, "outcome", "tagmiss"),
    )
    check(
        count(bm, "outcome", "bytemiss") >= 24 - slack
        and count(bm, "outcome", "correct") == 0,
        "bytemiss: the tag matches, the requested bytes are not valid: "
        "%d of 24" % count(bm, "outcome", "bytemiss"),
    )
    check(
        count(x, "outcome", "unsupported") >= 24 - slack
        and all(r["outcome"] != "correct" for r in x),
        "cross: a load that crosses a 16 byte boundary is not looked up "
        "(unsupported): %d of 24" % count(x, "outcome", "unsupported"),
    )
    check(
        count(bw, "result", "basewrong") >= 14 - slack
        and count(bw, "outcome", "correct") == 0,
        "basewrong: the predicted base is wrong: %d of 14, no candidate "
        "was accepted as correct" % count(bw, "result", "basewrong"),
    )
    wrong_hit = [
        r
        for r in bw
        if r["result"] == "basewrong"
        and r["note"].get("pred") == (True, False)
    ]
    real_hit = [
        r
        for r in bw
        if r["result"] == "basewrong" and r["note"].get("real") == (True, True)
    ]
    check(
        len(wrong_hit) >= 1,
        "a tag hit alone is not enough: for %d loads with a wrong base "
        "the V$ held valid bytes at the wrongly predicted address, and "
        "they were not the load's bytes (they would have been a wrong "
        "value)" % len(wrong_hit),
    )
    check(
        len(real_hit) >= 1,
        "and for %d loads with a wrong base the V$ held the right bytes "
        "at the real address: the probe rejected them because the base "
        "was wrong, not because the V$ lacked the data" % len(real_hit),
    )


def store_expect(s, recs, pcs):
    section("The cases")
    cf, ct = at(recs, pcs["va_st"]), at(recs, pcs["va_ct"])
    check(
        len(cf) == 16 and len(ct) == 16,
        "16 probed loads in each section (%d, %d)" % (len(cf), len(ct)),
    )
    check(
        count(cf, "result", "storeconflict") >= 10
        and count(cf, "result", "candidate") <= 2,
        "conflict: the older store writes the bytes the load reads and is "
        "still in flight: %d of 16 rejected, %d accepted"
        % (
            count(cf, "result", "storeconflict"),
            count(cf, "result", "candidate"),
        ),
    )
    selective = [
        r
        for r in ct
        if r["result"] == "candidate" and r.get("inflight_stores")
    ]
    check(
        count(ct, "result", "storeconflict") == 0 and len(selective) >= 10,
        "control: the store writes the next line, so with an older store "
        "in flight the load is still a candidate (%d of 16): the scan "
        "rejects only overlapping stores" % len(selective),
    )


def discard_expect(s, recs, pcs):
    section("The case")
    rs = at(recs, pcs["va_disc"])
    d = [r for r in rs if r["outcome"] == "discarded"]
    check(
        len(d) >= 1
        and s.vs["candidatesDiscarded"] == len(d)
        and all(r["result"] == "candidate" for r in d),
        "a candidate decided on the wrong path was discarded before it "
        "retired (%d); ASTRA logged the load as squashed and the V$ has "
        "no line for it" % len(d),
    )
    check(
        count(rs, "outcome", "correct") >= 1,
        "the loads that retired were validated (%d correct)"
        % count(rs, "outcome", "correct"),
    )


def fault_expect(s, recs, pcs):
    section("The case")
    rs = at(recs, pcs["va_f"])
    f = [r for r in rs if r["outcome"] == "faulted"]
    check(
        len(f) >= 1 and s.vs["candidatesFaulted"] == len(f),
        "candidates whose load took a translation fault (%d) have the "
        "terminal outcome faulted; ASTRA logged them as faulted and the "
        "V$ has no line for them" % len(f),
    )
    check(
        all(r["result"] == "candidate" for r in f),
        "each was a candidate (base correct, no store in flight)",
    )
    check(
        count(rs, "outcome", "tagmiss") >= 1,
        "the executions that completed were looked up (%d tag misses)"
        % count(rs, "outcome", "tagmiss"),
    )


def program_main(mode, expect):
    def main(out):
        return program_mode(out, mode, expect)

    return main


def noninterference_main(out):
    section("ASTRA program and LVP phase workload, probe off and on")
    consumer = None
    pcs_file = os.path.join(out, "load_pcs.txt")
    if os.path.exists(pcs_file):
        for line in cv.read_text(pcs_file).splitlines():
            t = line.split()
            if len(t) == 2 and t[0] == "consumer":
                consumer = int(t[1], 16)
    for tag, label, dis in (
        ("astra", "astra.c", os.path.join(out, "astra_build", "astra.S")),
        (
            "lvp",
            "LVP phase",
            os.path.join(out, "lvp_build", "phase", "ldld.S"),
        ),
    ):
        section(label)
        d = load_run(out, tag + "D", False)
        s = load_run(out, tag + "S", True)
        v = load_run(out, tag + "V", True, True)
        if d is None or s is None or v is None:
            continue
        noninterference(d, s, label)
        verification(s, v, label)
        analyze(v, immediates(dis), label + " (verified run)")
        if tag == "lvp":
            check(
                s.checksum == cv.PHASE_CHECKSUM,
                "%s: checksum %d" % (label, cv.PHASE_CHECKSUM),
            )
        recs, n = analyze(s, immediates(dis), label)
        identities(s, label)
        check(
            s.vs["candidateWrong"] == 0,
            "%s: no candidate was a byte-valid hit with wrong bytes (%d "
            "of %d byte-valid hits): the scan of the older stores left no "
            "stale value"
            % (label, s.vs["candidateWrong"], s.vs["byteValidHits"]),
        )
        if tag == "lvp":
            meta = [r for r in recs.values() if r["result"] == "metadata"]
            ab = cv.counters(s.stats, "astra")["baseUnavailableLoads"]
            check(
                len(meta) >= ab > 0 and all(r["pc"] == consumer for r in meta),
                "%s: the %d loads with missing metadata are all the LVP "
                "consumer (pc %#x), whose base register held a predicted "
                "value; ASTRA's baseUnavailableLoads is %d, the ones that "
                "retired" % (label, len(meta), consumer or 0, ab),
            )
        report(
            "%s: %s"
            % (label, ", ".join("%s %d" % (k, s.vs[k]) for k in COUNT_NAMES))
        )
    return finish()


REPORT_RE = re.compile(
    r"V-ASTRA verify: candidate differs from the load: exec: (\d+) pc: "
    r"0x([0-9a-f]+) pred_base: 0x([0-9a-f]+) actual_base: 0x([0-9a-f]+) ea: "
    r"0x([0-9a-f]+) size: (\d+) candidate: ([0-9a-f]+) actual: ([0-9a-f]+)"
)


def verify_detect_main(out):
    pcs = read_pcs(out)
    imm = immediates(os.path.join(out, "va.S"))
    section("Normal scan, strict verification (X0)")
    x0 = load_run(out, "X0", True, True)
    if x0 is None:
        return finish()
    check(
        x0.vs["rejStoreConflict"] >= 10
        and x0.vs["candidateWrong"] == 0
        and x0.vs["verifiedHits"] == x0.vs["candidateCorrect"],
        "the conflicting loads are rejected (%d), so no stale candidate "
        "exists: %d wrong, %d verified"
        % (
            x0.vs["rejStoreConflict"],
            x0.vs["candidateWrong"],
            x0.vs["verifiedHits"],
        ),
    )

    section("Scan of the older stores switched off (X1, test only)")
    x1 = load_run(out, "X1", True)
    if x1 is None:
        return finish()
    seq = x1.tr["seq"]
    vc_pos = {e["exec"]: p for p, (k, e) in enumerate(seq) if k == "vc"}
    vc = {e["exec"]: e for k, e in seq if k == "vc"}
    dec = {e["exec"]: (p, e) for p, (k, e) in enumerate(seq) if k == "dec"}
    wrong = [e for k, e in seq if k == "out" and e["outcome"] == "wrong"]
    check(
        x1.vs["rejStoreConflict"] == 0
        and len(wrong) >= 1
        and x1.vs["candidateWrong"] == len(wrong),
        "no store is rejected and %d candidates are byte-valid hits with "
        "stale bytes (candidateWrong %d)"
        % (len(wrong), x1.vs["candidateWrong"]),
    )
    explained = 0
    for e in wrong:
        pos, d = dec[e["exec"]]
        ea = e["vaddr"]
        if any(
            x["kind"] == "store"
            and x["exec"] < e["exec"]
            and vc_pos[x["exec"]] > pos
            and x["vaddr"] < ea + MAX_LOAD_BYTES
            and ea < x["vaddr"] + x["size"]
            for x in vc.values()
        ):
            explained += 1
    check(
        explained == len(wrong),
        "each stale candidate is a load with an older overlapping store "
        "still in flight at its decision, the case the scan rejects "
        "(%d of %d)" % (explained, len(wrong)),
    )
    check(
        all(e["pc"] == pcs["va_st"] for e in wrong),
        "all of them are at the conflict site (va_st)",
    )

    section("The same with strict verification (X2)")
    rc = cv_rc(out, "X2")
    text = cv.read_text(os.path.join(out, "X2.out"))
    m = REPORT_RE.search(text)
    check(
        rc not in (None, 0) and m is not None,
        "the run stops with a nonzero exit status (%s) and a report" % rc,
    )
    if m and wrong:
        g = m.groups()
        first = wrong[0]
        pos, d = dec[first["exec"]]
        got = (
            int(g[0]),
            int(g[1], 16),
            int(g[2], 16),
            int(g[3], 16),
            int(g[4], 16),
            int(g[5]),
            g[6],
            g[7],
        )
        want = (
            first["exec"],
            first["pc"],
            d["pred_base"],
            d["actual_base"],
            first["vaddr"],
            first["size"],
            first["candidate"],
            first["actual"],
        )
        check(
            got == want,
            "the report names the first stale load of X1: pc %#x, "
            "predicted base %#x, actual base %#x, address %#x, size %d, "
            "candidate %s, actual %s"
            % (want[1], want[2], want[3], want[4], want[5], want[6], want[7]),
        )
    check("panic" in text, "it is a panic")
    return finish()


def cv_rc(out, name):
    try:
        return int(cv.read_text(os.path.join(out, name + ".rc")).strip())
    except ValueError:
        return None


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "asm":
        if sys.argv[2] not in EXPECTED_SITES:
            print(__doc__)
            return 2
        return asm_main(*sys.argv[2:])
    modes = {
        "classify": program_main("classify", classify_expect),
        "store": program_main("store", store_expect),
        "discard": program_main("discard", discard_expect),
        "fault": program_main("fault", fault_expect),
        "noninterference": noninterference_main,
        "verify-detect": verify_detect_main,
    }
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        print(__doc__)
        return 2
    return modes[sys.argv[1]](sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
