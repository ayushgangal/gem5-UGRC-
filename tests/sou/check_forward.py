#!/usr/bin/env python3
"""Checks of Design B, V-ASTRA forwarding to dependent loads (run_forward.sh).

  directed OUT   forward.c in one configuration: feature off (P) and on (R)
  lifecycle OUT  other programs, P and R: equivalence and lifecycle only

Everything is read from traces: issue, request and commit ticks of each
instruction (MinorExecute, MinorMem), the record events of the producers and
the use by consumers (VBypass: create, publish, fail, blocked, admit, erase,
outcome), the bypass successes, the V-ASTRA decisions and the scoreboard's own
mark/release/clear events.  A statistic is never the only evidence.  Whole-run
numCycles is information only; per-instruction ticks are the measurement."""

import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_release as cr  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failures += 1


def info(msg):
    print("INFO  " + msg)


ID = r"\S+/(\d+)\.\d+ pc: 0x([0-9a-f]+)"
R_ISSUE = re.compile(r"Issuing inst: " + ID)
R_INIT = re.compile(r"Initiating memRef inst: " + ID)
R_SEND = re.compile(r"Trying to send request: " + ID)
R_RESP = re.compile(r"Memory response inst: " + ID)
R_COMMIT = re.compile(r"Committing inst: " + ID)
R_DEL = re.compile(r"Deleting load request: " + ID)
R_PUSH = re.compile(r"Pushing request \(load\) addr: 0x([0-9a-f]+)")
R_FWD = re.compile(
    r"VBypass fwd (create|publish|fail|blocked|admit|erase|outcome) "
    r"exec: (\d+) pc: 0x([0-9a-f]+)(.*)"
)
R_BYP = re.compile(r"VBypass exec: (\d+) pc: 0x([0-9a-f]+)")
R_DEC = re.compile(
    r"VAstra decide exec: (\d+) pc: 0x([0-9a-f]+) result: (\w+)"
)
R_UNP = re.compile(
    r"Unpredictable (mark|release|clear) inst: "
    + ID
    + r".* regIndex: (\d+) count: (\d+)"
)
R_REL = re.compile(r"VBypass release exec: (\d+)")
LINE = re.compile(r"^\s*(\d+): (\S+): (.*)$")
EXEC = re.compile(r"^\s*\d+: system\.cpu: 0x([0-9a-f]+)\s+:(.*)$")
EXECR = re.compile(
    r"^\s*\d+: system\.cpu: 0x([0-9a-f]+)\s+:.*?:\s+D=0x([0-9a-f]+)"
    r"(?:\s+A=0x([0-9a-f]+))?"
)
KEY = (
    "Issuing inst",
    "Initiating memRef",
    "Trying to send",
    "Memory response",
    "Committing inst",
    "Deleting load",
    "Pushing request (load)",
    "VBypass",
    "VAstra decide",
    "Unpredictable ",
)


class T:
    def __init__(self, path):
        self.inst = {}  # seq -> dict
        self.fwd = {}  # seq -> list of (event, tick, rest)
        self.bypass = {}  # seq -> tick
        self.release = set()
        self.decide = {}
        self.events = []  # scoreboard events (tick, kind, seq, reg, count)
        self.stream = []
        self.results = {}  # pc -> list of (D, A)
        last_init = None
        with open(path, errors="replace") as f:
            for line in f:
                m = EXEC.match(line)
                if m:
                    self.stream.append(m.group(1) + m.group(2))
                    r = EXECR.match(line)
                    if r:
                        self.results.setdefault(
                            int(r.group(1), 16), []
                        ).append(
                            (
                                int(r.group(2), 16),
                                int(r.group(3), 16) if r.group(3) else None,
                            )
                        )
                    continue
                if not any(k in line for k in KEY):
                    continue
                m = LINE.match(line)
                if not m:
                    continue
                tick, msg = int(m.group(1)), m.group(3)
                g = R_PUSH.search(msg)
                if g and last_init is not None:
                    self.inst[last_init]["vaddr"] = int(g.group(1), 16)
                    last_init = None
                    continue
                for name, rx in (
                    ("issue", R_ISSUE),
                    ("init", R_INIT),
                    ("send", R_SEND),
                    ("resp", R_RESP),
                    ("commit", R_COMMIT),
                    ("del", R_DEL),
                ):
                    g = rx.search(msg)
                    if not g:
                        continue
                    e = self.inst.setdefault(
                        int(g.group(1)), {"pc": int(g.group(2), 16)}
                    )
                    if name == "send":
                        e.setdefault("send", tick)
                        e["nsend"] = e.get("nsend", 0) + 1
                    elif name == "resp":
                        e["commit"] = tick
                        e["nresp"] = e.get("nresp", 0) + 1
                    elif name == "del":
                        e["ndel"] = e.get("ndel", 0) + 1
                    elif name == "init":
                        e["init"] = tick
                        last_init = int(g.group(1))
                    else:
                        e[name] = tick
                    break
                else:
                    g = R_FWD.search(msg)
                    if g:
                        self.fwd.setdefault(int(g.group(2)), []).append(
                            (g.group(1), tick, g.group(4).strip())
                        )
                        continue
                    g = R_BYP.search(msg)
                    if g:
                        self.bypass[int(g.group(1))] = tick
                        continue
                    g = R_DEC.search(msg)
                    if g:
                        self.decide[int(g.group(1))] = g.group(3)
                        continue
                    g = R_REL.search(msg)
                    if g:
                        self.release.add(int(g.group(1)))
                        continue
                    g = R_UNP.search(msg)
                    if g:
                        self.events.append(
                            (
                                tick,
                                g.group(1),
                                int(g.group(2)),
                                int(g.group(4)),
                                int(g.group(5)),
                            )
                        )

    def ev(self, seq, name):
        return [(t, r) for (n, t, r) in self.fwd.get(seq, []) if n == name]

    def committed(self, pc):
        return sorted(
            s for s, e in self.inst.items() if e["pc"] == pc and "commit" in e
        )


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


def run_files(out, v):
    return int(open(os.path.join(out, v + ".rc")).read())


def lifecycle_checks(out, label, p, r, load_pcs=None):
    rc = {v: run_files(out, v) for v in "PR"}
    check(rc["P"] == 0 and rc["R"] == 0, "%s: both runs exit 0" % label)
    sums = {v: cr.program_values(os.path.join(out, v + ".out")) for v in "PR"}
    check(
        sums["P"] == sums["R"] and sums["P"],
        "%s: output identical (%s)" % (label, sums["P"]),
    )
    check(
        len(p.stream) > 0 and p.stream == r.stream,
        "%s: committed instruction stream identical (%d instructions)"
        % (label, len(p.stream)),
    )
    check(
        not p.fwd, "%s P: forwarding off: no forwarding event at all" % label
    )
    # record lifecycle
    created = [
        s for s, evs in r.fwd.items() if any(n == "create" for n, _, _ in evs)
    ]
    bad = []
    for s in created:
        names = [n for n, _, _ in r.fwd[s]]
        if names.count("create") != 1 or names.count("erase") != 1:
            bad.append((s, names))
            continue
        if names.index("erase") < names.index("create"):
            bad.append((s, names))
        st = r.ev(s, "erase")[0][1].split()[-1]
        want = (
            "validated"
            if "publish" in names
            else "failed"
            if "fail" in names
            else "pending"
        )
        if st != want or ("publish" in names and "fail" in names):
            bad.append((s, names, st))
    check(
        not bad,
        "%s R: %d producer records, each created once, erased once (when its "
        "producer left the queue) in the state its events imply (%d "
        "violations)" % (label, len(created), len(bad)),
    )
    # publish only at a bypass success, never with a request to memory
    pub = [
        s for s, evs in r.fwd.items() if any(n == "publish" for n, _, _ in evs)
    ]
    bad = [
        s
        for s in pub
        if s not in r.bypass
        or r.ev(s, "publish")[0][0] != r.bypass[s]
        or r.inst.get(s, {}).get("nsend", 0)
    ]
    check(
        not bad,
        "%s R: %d values published, each at the tick of its final "
        "bypass success and none of those loads sent a memory request (%d "
        "violations)" % (label, len(pub), len(bad)),
    )
    # admission needs a published producer, in time, with the same value
    adm = [
        s for s, evs in r.fwd.items() if any(n == "admit" for n, _, _ in evs)
    ]
    bad = []
    for s in adm:
        t_adm, rest = r.ev(s, "admit")[0]
        m = re.search(r"producer: (\d+) value: 0x([0-9a-f]+)", rest)
        prod, val = int(m.group(1)), int(m.group(2), 16)
        pubs = r.ev(prod, "publish")
        if (
            len(r.ev(s, "admit")) != 1
            or not pubs
            or pubs[0][0] >= t_adm
            or int(pubs[0][1].split("value: 0x")[1], 16) != val
        ):
            bad.append((s, prod))
    check(
        not bad,
        "%s R: %d admitted consumers, each bound to one producer "
        "whose value was published earlier with the same value (%d "
        "violations)" % (label, len(adm), len(bad)),
    )
    # outcomes
    bad = [s for s in adm if len(r.ev(s, "outcome")) != 1]
    kinds = {}
    for s in adm:
        o = r.ev(s, "outcome")
        if o:
            k = o[0][1].split("outcome: ")[1]
            kinds[k] = kinds.get(k, 0) + 1
    ws = windows(os.path.join(out, "R", "stats.txt"))
    pre = "system.cpu.vastra."
    okw = okc = True
    tot_a = tot_o = 0
    prev = 0
    for w in ws:
        o = sum(
            w[pre + n] for n in ("fwdCommitted", "fwdSquashed", "fwdFaulted")
        )
        okw &= (
            w[pre + "fwdAdmitted"] + w[pre + "fwdCarryIn"]
            == o + w[pre + "fwdInFlight"]
        )
        okc &= w[pre + "fwdCarryIn"] == prev
        tot_a += w[pre + "fwdAdmitted"]
        tot_o += o
        prev = w[pre + "fwdInFlight"]
    tot = lambda n: sum(w[pre + n] for w in ws)
    check(
        not bad
        and okw
        and okc
        and prev == 0
        and tot_a == len(adm)
        and tot_o == len(adm)
        and tot("fwdCommitted") == kinds.get("committed", 0)
        and tot("fwdSquashed") == kinds.get("squashed", 0)
        and tot("fwdFaulted") == kinds.get("faulted", 0),
        "%s R: every admitted consumer has exactly one outcome %s; the "
        "counters agree with the trace and the identity admitted + carryIn "
        "= outcomes + inFlight holds in each of %d statistics window(s) "
        "(admitted %d)" % (label, kinds, len(ws), len(adm)),
    )
    # exactly once
    dup = [
        s
        for s, e in r.inst.items()
        if e.get("nresp", 0) > 1
        or e.get("ndel", 0) > 1
        or (
            load_pcs
            and e["pc"] in load_pcs
            and "commit" in e
            and (e.get("nresp", 0) != 1 or e.get("ndel", 0) != 1)
        )
    ]
    check(
        not dup,
        "%s R: no instruction had two responses or two request "
        "deletions%s (%d violations)"
        % (
            label,
            ", and every committed load of the program had exactly one of each"
            if load_pcs
            else "",
            len(dup),
        ),
    )
    # scoreboard
    class O:
        pass

    for v, tr in (("P", p), ("R", r)):
        o = O()
        o.events = tr.events
        if not tr.events:
            info(
                "%s %s: no unpredictable scoreboard marks (default memory FU "
                "timing): accounting not applicable" % (label, v)
            )
            continue
        cr.scoreboard_accounting(o, "%s %s" % (label, v), len(tr.release))
    return adm, kinds


IMM = {"ft": 1024}


def directed(out):
    pcs = cr.read_pcs(os.path.join(out, "pcs.txt"))
    cfg = open(os.path.join(out, "config.txt")).read().strip()
    p, r = T(os.path.join(out, "P", "trace.txt")), T(
        os.path.join(out, "R", "trace.txt")
    )
    loads = set(v for k, v in pcs.items() if k[3:] in ("M", "P", "C", "E"))
    adm, kinds = lifecycle_checks(out, "directed(%s)" % cfg, p, r, loads)
    by_pc = {v: k for k, v in pcs.items()}
    lab = lambda s: by_pc.get(r.inst.get(s, {}).get("pc"), "?")
    pred = {"C": "P", "E": "C"}
    st = lambda n: cr.stat(os.path.join(out, "R"), "vastra." + n)

    # binding: the producer of a consumer is the previous load of its section
    bad = []
    for s in adm:
        m = re.search(r"producer: (\d+)", r.ev(s, "admit")[0][1])
        ls, lp = lab(s), lab(int(m.group(1)))
        if not (
            ls[:2] == lp[:2] and ls[3:] in pred and pred[ls[3:]] == lp[3:]
        ):
            bad.append((ls, lp))
    check(
        not bad,
        "binding: each consumer's producer is the preceding load of "
        "its own section and iteration (%d violations)" % len(bad),
    )

    sec_adm = {}
    for s in adm:
        sec_adm[lab(s)] = sec_adm.get(lab(s), 0) + 1
    info(
        "admitted consumers by instruction: %s" % dict(sorted(sec_adm.items()))
    )

    # values and addresses against the architectural results
    bad_v, bad_a, nv = [], [], 0
    for s in adm:
        t, rest = r.ev(s, "admit")[0]
        m = re.search(r"producer: (\d+) value: 0x([0-9a-f]+)", rest)
        prod, val = int(m.group(1)), int(m.group(2), 16)
        pe = r.inst.get(prod, {})
        if "commit" in pe:
            lst = r.committed(pe["pc"])
            res = r.results.get(pe["pc"], [])
            if len(lst) == len(res):
                nv += 1
                if res[lst.index(prod)][0] != val:
                    bad_v.append(prod)
        ce = r.inst.get(s, {})
        if "commit" in ce:
            lst = r.committed(ce["pc"])
            res = r.results.get(ce["pc"], [])
            if len(lst) == len(res):
                imm = IMM.get(lab(s)[:2], 0)
                a = res[lst.index(s)][1]
                if a != val + imm or ce.get("vaddr") != val + imm:
                    bad_a.append((s, a, val, ce.get("vaddr")))
    check(
        nv > 0 and not bad_v,
        "published values equal the register values "
        "the producers committed (%d checked, %d differ)" % (nv, len(bad_v)),
    )
    check(
        not bad_a,
        "each forwarded load's effective address and request "
        "address equal published value + immediate (%d violations)"
        % len(bad_a),
    )

    # timeline: published < request built < producer commit
    bad_t, gaps, ntl = [], [], 0
    for s in adm:
        t_adm, rest = r.ev(s, "admit")[0]
        prod = int(re.search(r"producer: (\d+)", rest).group(1))
        t_pub = r.ev(prod, "publish")[0][0]
        ce, pe = r.inst[s], r.inst.get(prod, {})
        if "commit" not in pe or "commit" not in ce:
            continue
        ntl += 1
        ok = t_pub < ce["init"] < pe["commit"]
        if "send" in ce:
            ok = ok and t_pub < ce["send"] < pe["commit"]
            gaps.append((pe["commit"] - ce["send"]) // 1000)
        if not ok:
            bad_t.append(
                (lab(s), t_pub, ce.get("init"), ce.get("send"), pe["commit"])
            )
    check(
        ntl >= 20 and not bad_t,
        "timeline: producer value published < "
        "dependent's request built (and sent to memory, when it is) < "
        "producer's commit, for %d forwarded loads that committed (%d "
        "violations)" % (ntl, len(bad_t)),
    )
    for b in bad_t[:3]:
        print("      violation", b)
    if gaps:
        info(
            "forwarded loads that went to memory: request sent %d..%d cycles "
            "(mean %.0f) before their producer's commit"
            % (min(gaps), max(gaps), sum(gaps) / len(gaps))
        )

    # feature-off control: the ordinary dependency
    bad_c, nc = [], 0
    for c_label in ("ch_C", "ch_E", "cm_C"):
        pl = (
            "ch_P"
            if c_label in ("ch_C",)
            else "ch_C"
            if c_label == "ch_E"
            else "cm_P"
        )
        ip, cp = p.committed(pcs[pl]), p.committed(pcs[c_label])
        for a, b in zip(ip, cp):
            nc += 1
            ea, eb = p.inst[a], p.inst[b]
            t_req = eb.get("send", eb.get("init"))
            if eb["init"] < ea["commit"] or t_req < ea["commit"]:
                bad_c.append((c_label, ea["commit"], eb["init"]))
    check(
        nc > 0 and not bad_c,
        "feature-off control: the dependent's request "
        "is built and sent only after its producer commits (%d pairs, %d "
        "violations)" % (nc, len(bad_c)),
    )

    # section expectations (R)
    def count(label):
        return sec_adm.get(label, 0)

    check(
        count("ch_C") >= 10 and count("ch_E") >= 10,
        "ch: the chain is forwarded: C %d, E %d iterations"
        % (count("ch_C"), count("ch_E")),
    )
    both = 0
    for sC in [s for s in adm if lab(s) == "ch_C"]:
        sE = sC + 1
        if sE in adm and lab(sE) == "ch_E":
            tc = r.ev(sC, "admit")[0][0]
            te = r.ev(sE, "admit")[0][0]
            pc_ = r.ev(sC, "publish")
            if pc_ and tc <= pc_[0][0] <= te:
                both += 1
    check(
        both >= 8,
        "ch: in %d iterations C was forwarded, published its "
        "own value and E was forwarded from C (admit(C) <= publish(C) <= "
        "admit(E))" % both,
    )
    check(
        count("cm_C") >= 10,
        "cm: the dependent that misses to memory is "
        "forwarded in %d iterations" % count("cm_C"),
    )
    for sec in ("tm", "sc", "ab", "pd"):
        check(
            count(sec + "_C") == 0,
            "%s: no dependent is forwarded (%d)" % (sec, count(sec + "_C")),
        )
    # why: bw/sc decisions create no record; tm and ab producers fail
    nobase = [
        s
        for s, d in r.decide.items()
        if d == "basewrong" and lab(s)[3:] == "P"
    ]
    check(
        len(nobase) >= 8 and not any(r.ev(s, "create") for s in nobase),
        "bw: %d producers with a wrong base prediction created no record, "
        "so nothing was published for them" % len(nobase),
    )
    nosc = [
        s
        for s, d in r.decide.items()
        if d == "storeconflict" and lab(s) == "sc_P"
    ]
    check(
        len(nosc) >= 8 and not any(r.ev(s, "create") for s in nosc),
        "sc: %d producers with an older conflicting store created no "
        "record" % len(nosc),
    )
    tmf = [s for s in r.fwd if lab(s) == "tm_P" and r.ev(s, "fail")]
    check(
        len(tmf) >= 8 and not any(r.ev(s, "publish") for s in tmf),
        "tm: %d producers with a V$ tag miss ended failed, nothing "
        "published" % len(tmf),
    )
    abf = [s for s in r.fwd if lab(s) == "ab_P" and r.ev(s, "fail")]
    ab_ab = [s for s in abf if "abandoned" in r.ev(s, "fail")[0][1]]
    check(
        len(ab_ab) >= 8 and not any(r.ev(s, "publish") for s in abf),
        "ab: %d candidates were abandoned at the LSQ send point (V$ entry "
        "replaced): failed, nothing published, they went to memory"
        % len(ab_ab),
    )
    went = [s for s in ab_ab if r.inst[s].get("nsend", 0) >= 1]
    check(
        len(went) == len(ab_ab),
        "ab: every abandoned producer sent a real "
        "memory request (%d of %d)" % (len(went), len(ab_ab)),
    )
    pdp = [s for s in r.fwd if lab(s) == "pd_P" and r.ev(s, "publish")]
    gap = [
        (r.ev(s, "publish")[0][0] - r.ev(s, "create")[0][0]) // 1000
        for s in pdp
    ]
    check(
        len(pdp) >= 8 and sum(1 for g in gap if g >= 20) >= 8,
        "pd: the producer behind an older store stays pending %d..%d "
        "cycles between its record's creation and its publication; its "
        "dependents are not forwarded (%d)"
        % (min(gap or [0]), max(gap or [0]), count("pd_C")),
    )
    ftb = [(s, e) for s, e in r.inst.items() if lab(s) == "ft_C"]
    pend = [
        s
        for s, _ in ftb
        if any("state: pending" in x for _, x in r.ev(s, "blocked"))
    ]
    fail = [
        s
        for s, _ in ftb
        if any("state: failed" in x for _, x in r.ev(s, "blocked"))
    ]
    if cfg == "default":
        check(
            len(pend) >= 8,
            "ft: dependents found their producer pending "
            "(translation fault: no bypass decision) and were not forwarded "
            "in %d cases (%s forwarded)" % (len(pend), count("ft_C")),
        )
        check(
            count("ft_C") == 0,
            "ft: no dependent of a faulting producer " "was forwarded",
        )
    sq = kinds.get("squashed", 0)
    if cfg == "default":
        check(
            sq >= 1,
            "sq: %d forwarded consumers on the wrong path were "
            "squashed (outcome squashed, one each)" % sq,
        )
    info(
        "blocked consumers: pending %d, failed %d (counters %d, %d)"
        % (
            sum(
                1
                for s in r.fwd
                if any(
                    n == "blocked" and "pending" in x for n, _, x in r.fwd[s]
                )
            ),
            sum(
                1
                for s in r.fwd
                if any(
                    n == "blocked" and "failed" in x for n, _, x in r.fwd[s]
                )
            ),
            st("fwdBlockedPending"),
            st("fwdBlockedFailed"),
        )
    )
    if cfg == "stall-on-use":
        check(
            st("fwdBlockedPending") == 0 and st("fwdBlockedFailed") == 0,
            "stall-on-use: a dependent cannot issue before its producer's "
            "release, so none is ever blocked on a pending or failed record "
            "(0, 0)",
        )

    # measurements, separate from the checks above
    def committed_pairs(label_p, label_c):
        return list(zip(r.committed(pcs[label_c]), p.committed(pcs[label_c])))

    for sec, lbl in (("ch", "ch_E"), ("cm", "cm_C")):
        d = []
        for sr, sp in committed_pairs(None, lbl):
            d.append((p.inst[sp]["commit"] - r.inst[sr]["commit"]) // 1000)
        if d:
            info(
                "%s: commit tick of %s earlier with forwarding by %d..%d "
                "cycles (mean %.1f) than without (different absolute times, "
                "relative to the run start)"
                % (sec, lbl, min(d), max(d), sum(d) / len(d))
            )
    span = (
        lambda t, sec: (
            max(t.inst[s]["commit"] for s in t.committed(pcs[sec + "_U"]))
            - min(t.inst[s]["issue"] for s in t.committed(pcs[sec + "_M"]))
        )
        // 1000
    )
    for sec in ("ch", "cm", "ft", "sq", "pd", "ab", "sc", "tm", "bw"):
        info(
            "%s: section span (first M issue to last U commit): off %d, on %d "
            "cycles" % (sec, span(p, sec), span(r, sec))
        )
    info(
        "numCycles (information only): off %d, on %d"
        % (
            cr.stat(os.path.join(out, "P"), "numCycles"),
            cr.stat(os.path.join(out, "R"), "numCycles"),
        )
    )


def lifecycle(out):
    for name in sorted(os.listdir(out)):
        d = os.path.join(out, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "R.rc")):
            continue
        print("--- " + name)
        p, r = T(os.path.join(d, "P", "trace.txt")), T(
            os.path.join(d, "R", "trace.txt")
        )
        adm, kinds = lifecycle_checks(d, name, p, r)
        info(
            "%s: %d records published, %d consumers forwarded %s"
            % (
                name,
                sum(1 for s in r.fwd if r.ev(s, "publish")),
                len(adm),
                kinds,
            )
        )


if __name__ == "__main__":
    {"directed": directed, "lifecycle": lifecycle}[sys.argv[1]](sys.argv[2])
    if failures or cr.failures:
        print("%d correctness check(s) failed" % (failures + cr.failures))
        sys.exit(1)
