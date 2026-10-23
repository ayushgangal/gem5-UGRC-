#!/usr/bin/env python3
"""Checks of Design A, the V-ASTRA scoreboard release (run_release.sh).

Two kinds of check:
  directed OUT   release.c under vp_stall_on_use.py, feature off (P) and on (R)
  lifecycle OUT  other programs (discard, fault, store, syscall, ...) with the
                 feature off (P) and on (R): equivalence and scoreboard
                 accounting only

Everything is read from traces: the issue and commit ticks of the instructions
(MinorExecute, MinorMem), the release and bypass lines (VBypass, VAstra) and the
scoreboard's own unpredictable-mark events (MinorScoreboard: mark, release,
clear, each with the register's count after the event).  A statistic is never
the only evidence."""

import os
import re
import sys

sys.dont_write_bytecode = True

failures = 0


def check(cond, msg):
    global failures
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failures += 1


def info(msg):
    print("INFO  " + msg)


ID = r"\d+/\d+\.\d+/\d+/(\d+)\.\d+ pc: (0x[0-9a-f]+)"
PAT = [
    ("issue", re.compile(r"Issuing inst: " + ID)),
    ("commit", re.compile(r"Committing inst: " + ID)),
    ("resp", re.compile(r"Memory response inst: " + ID)),
    ("send", re.compile(r"Trying to send request: " + ID)),
    (
        "release",
        re.compile(
            r"VBypass release exec: (\d+) pc: (0x[0-9a-f]+) regs: (\d+)"
        ),
    ),
    ("bypass", re.compile(r"VBypass exec: (\d+) pc: (0x[0-9a-f]+) ")),
    (
        "decide",
        re.compile(
            r"VAstra decide exec: (\d+) pc: (0x[0-9a-f]+) result: (\w+)"
        ),
    ),
    (
        "outcome",
        re.compile(
            r"VAstra outcome exec: (\d+) pc: (0x[0-9a-f]+) outcome: (\w+)"
        ),
    ),
    (
        "unpred",
        re.compile(
            r"Unpredictable (mark|release|clear) inst: "
            + ID
            + r".* regIndex: (\d+) count: (\d+)"
        ),
    ),
]
LINE = re.compile(r"^\s*(\d+): (\S+): (.*)$")
EXEC = re.compile(r"^\s*\d+: system\.cpu: (0x[0-9a-f]+)\s+:(.*)$")
KEYWORDS = (
    "Issuing inst",
    "Committing inst",
    "Memory response inst",
    "Trying to send request",
    "VBypass release",
    "VBypass exec",
    "VAstra decide",
    "VAstra outcome",
    "Unpredictable ",
)


class Trace:
    def __init__(self, path):
        self.inst = {}  # exec seq -> {pc, issue, commit, send}
        self.release = {}  # exec seq -> (tick, pc, regs)
        self.bypass = {}  # exec seq -> tick
        self.decide = {}  # exec seq -> result
        self.outcome = {}  # exec seq -> outcome
        self.events = []  # (tick, kind, seq, reg, count)
        self.stream = []  # committed instructions without ticks
        with open(path, errors="replace") as f:
            for line in f:
                m = EXEC.match(line)
                if m:
                    self.stream.append(m.group(1) + m.group(2))
                    continue
                if not any(k in line for k in KEYWORDS):
                    continue
                m = LINE.match(line)
                if not m:
                    continue
                tick, msg = int(m.group(1)), m.group(3)
                for kind, rx in PAT:
                    g = rx.search(msg)
                    if not g:
                        continue
                    g = g.groups()
                    if kind in ("issue", "commit", "resp", "send"):
                        e = self.inst.setdefault(
                            int(g[0]), {"pc": int(g[1], 16)}
                        )
                        key = "commit" if kind == "resp" else kind
                        if kind == "send":
                            e["send"] = e.get("send", 0) + 1
                        else:
                            e[key] = tick
                    elif kind == "release":
                        self.release[int(g[0])] = (
                            tick,
                            int(g[1], 16),
                            int(g[2]),
                        )
                    elif kind == "bypass":
                        self.bypass[int(g[0])] = tick
                    elif kind == "decide":
                        self.decide[int(g[0])] = g[2]
                    elif kind == "outcome":
                        self.outcome[int(g[0])] = g[2]
                    elif kind == "unpred":
                        self.events.append(
                            (tick, g[0], int(g[1]), int(g[3]), int(g[4]))
                        )
                    break


def stat(out, name):
    """The statistic summed over every statistics window of stats.txt (one
    window unless the run dumped and reset the statistics)"""
    total, seen = 0.0, False
    for l in open(os.path.join(out, "stats.txt")):
        if l.startswith("system.cpu." + name + " "):
            total += float(l.split()[1])
            seen = True
    return total if seen else None


def program_values(path):
    """checksum/errors values, tolerant of negative numbers and of the
    program's output being interleaved with the wrapper's and gem5's lines"""
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


def read_pcs(path):
    return {k: int(v, 16) for k, v in (l.split() for l in open(path))}


def scoreboard_accounting(tr, label, released_expected):
    """Replay the unpredictable events of one run"""
    count = {}
    marks = {}  # (seq, reg) -> 'open' | 'release' | 'clear'
    bad = []
    for tick, kind, seq, reg, after in tr.events:
        before = count.get(reg, 0)
        if kind == "mark":
            ok = after == before + 1 and (seq, reg) not in marks
            marks[(seq, reg)] = "open"
        else:
            ok = (
                after == before - 1
                and before > 0
                and marks.get((seq, reg)) == "open"
            )
            if marks.get((seq, reg)) == "open":
                marks[(seq, reg)] = kind
        count[reg] = after
        if not ok:
            bad.append((tick, kind, seq, reg, before, after))
    still_open = [k for k, v in marks.items() if v == "open"]
    nrel = sum(1 for v in marks.values() if v == "release")
    check(
        len(tr.events) > 0 and not bad,
        "%s: %d scoreboard events replay exactly (count after each event = "
        "model, a decrement only for a mark that is open, never below 0, "
        "%d violations)" % (label, len(tr.events), len(bad)),
    )
    for b in bad[:4]:
        print("      violation", b)
    check(
        not still_open and all(v == 0 for v in count.values()),
        "%s: every mark was decremented exactly once and every register "
        "ends at 0 (%d open, final counts %s)"
        % (label, len(still_open), sorted(set(count.values()))),
    )
    check(
        nrel == released_expected,
        "%s: %d marks were released early (expected %d); the others were "
        "cleared at the load's pop" % (label, nrel, released_expected),
    )
    return nrel


def run_pair(out, label):
    p = Trace(os.path.join(out, "P", "trace.txt"))
    r = Trace(os.path.join(out, "R", "trace.txt"))
    rc = {v: int(open(os.path.join(out, v + ".rc")).read()) for v in "PR"}
    check(rc["P"] == 0 and rc["R"] == 0, "%s: both runs exit 0" % label)
    sums = {v: program_values(os.path.join(out, v + ".out")) for v in "PR"}
    check(
        sums["P"] == sums["R"] and sums["P"],
        "%s: output identical (%s)" % (label, sums["P"]),
    )
    check(
        len(p.stream) > 0 and p.stream == r.stream,
        "%s: committed instruction stream identical (%d instructions)"
        % (label, len(p.stream)),
    )
    # P: nothing released
    check(
        not p.release
        and stat(os.path.join(out, "P"), "vastra.scoreboardReleases") == 0,
        "%s P: feature off: no release line, no release counted" % label,
    )
    # R: a release happens exactly at a final bypass success
    check(
        set(r.release) == set(r.bypass),
        "%s R: the loads that released their marks are exactly the loads "
        "whose bypass succeeded (%d = %d)"
        % (label, len(r.release), len(r.bypass)),
    )
    n = stat(os.path.join(out, "R"), "vastra.scoreboardReleases")
    check(
        n == len(r.release)
        and stat(os.path.join(out, "R"), "vastra.scoreboardReleaseNoop") == 0,
        "%s R: counter agrees with the trace (%d releases, 0 no-op)"
        % (label, n),
    )
    # a release is never for a load that was sent to memory
    sent = [s for s in r.release if r.inst.get(s, {}).get("send", 0)]
    check(
        not sent,
        "%s R: no released load sent a memory request (%d of %d "
        "did)" % (label, len(sent), len(r.release)),
    )
    # release before commit, and the commit pops it once (events)
    late = [
        s
        for s, (t, _, _) in r.release.items()
        if r.inst.get(s, {}).get("commit", t) < t
    ]
    check(
        not late,
        "%s R: every release precedes the load's commit/discard" % label,
    )
    scoreboard_accounting(p, label + " P", 0)
    scoreboard_accounting(r, label + " R", len(r.release))
    info(
        "%s: cycles P %d R %d (information only)"
        % (
            label,
            stat(os.path.join(out, "P"), "numCycles"),
            stat(os.path.join(out, "R"), "numCycles"),
        )
    )
    return p, r


def sections(tr, pcs, sec, names):
    """Committed instances of each name, grouped by iteration"""
    by = {
        n: sorted(
            (s, e)
            for s, e in tr.inst.items()
            if e["pc"] == pcs[sec + "_" + n] and "commit" in e and "issue" in e
        )
        for n in names
    }
    n = min(len(v) for v in by.values())
    return [{x: by[x][i] for x in names} for i in range(n)]


def directed(out):
    pcs = read_pcs(os.path.join(out, "pcs.txt"))
    p, r = run_pair(out, "directed")
    names = ["M", "L", "I1", "I2", "D", "J"]
    tk = lambda e, k: e[k] // 1000
    rel_ok = True
    summary = {}
    for sec, want in (
        ("ca", "candidate"),
        ("bw", "base wrong"),
        ("tm", "V$ tag miss"),
        ("sc", "older store conflict"),
    ):
        ip = sections(p, pcs, sec, names)
        ir = sections(r, pcs, sec, names)
        check(
            len(ip) == len(ir) and len(ip) >= 16,
            "%s (%s): %d iterations in both runs" % (sec, want, len(ip)),
        )
        # feature off: D never issues before L commits (stall-on-use)
        early_p = [
            i
            for i, it in enumerate(ip)
            if tk(it["D"][1], "issue") < tk(it["L"][1], "commit")
        ]
        check(
            not early_p,
            "%s P: the dependent never issues before the "
            "load commits (stall-on-use)" % sec,
        )
        rel_it, norel_it = [], []
        for it in ir:
            (rel_it if it["L"][0] in r.release else norel_it).append(it)
        bad_rel, bad_norel = [], []
        for it in rel_it:
            t_rel = r.release[it["L"][0]][0] // 1000
            d_issue = tk(it["D"][1], "issue")
            l_commit = tk(it["L"][1], "commit")
            if not (0 <= d_issue - t_rel <= 2 and d_issue + 50 <= l_commit):
                bad_rel.append((d_issue, t_rel, l_commit))
        for it in norel_it:
            if tk(it["D"][1], "issue") < tk(it["L"][1], "commit"):
                bad_norel.append(it["L"][0])
        if rel_it:
            check(
                not bad_rel,
                "%s R: after the release the dependent issues within 2 "
                "cycles of it and >= 50 cycles before the load commits "
                "(%d released iterations, %d violations)"
                % (sec, len(rel_it), len(bad_rel)),
            )
            ds = [
                tk(it["D"][1], "issue") - r.release[it["L"][0]][0] // 1000
                for it in rel_it
            ]
            lc = [
                tk(it["L"][1], "commit") - tk(it["D"][1], "issue")
                for it in rel_it
            ]
            info(
                "%s R: D issue - release tick %s; L commit - D issue %d..%d "
                "cycles" % (sec, sorted(set(ds)), min(lc), max(lc))
            )
        check(
            not bad_norel,
            "%s R: a load that was not released keeps its dependents "
            "waiting for its commit (%d iterations, %d violations)"
            % (sec, len(norel_it), len(bad_norel)),
        )
        # execution is never earlier than the producer's commit
        exe = [
            it
            for it in ir
            if tk(it["D"][1], "commit") < tk(it["L"][1], "commit")
        ]
        check(
            not exe,
            "%s R: the dependent never commits (executes) before "
            "its producer" % sec,
        )
        summary[sec] = (len(rel_it), len(norel_it))
        # section expectations
        decides = [r.decide.get(it["L"][0]) for it in ir]
        outcomes = [r.outcome.get(it["L"][0]) for it in ir]
        if sec == "ca":
            check(
                len(rel_it) >= 10,
                "ca R: most candidate iterations "
                "released (%d of %d)" % (len(rel_it), len(ir)),
            )
        elif sec == "bw":
            nbw = sum(1 for d in decides if d == "basewrong")
            check(
                nbw >= 8,
                "bw R: the base prediction was wrong for %d "
                "iterations" % nbw,
            )
            check(
                not any(
                    it["L"][0] in r.release
                    and r.decide.get(it["L"][0]) == "basewrong"
                    for it in ir
                ),
                "bw R: no base-mismatch load was released",
            )
        elif sec == "tm":
            ntm = sum(1 for o in outcomes if o == "tagmiss")
            check(
                ntm >= 8 and len(rel_it) == 0,
                "tm R: %d V$ tag misses "
                "went to memory, none released (%d released)"
                % (ntm, len(rel_it)),
            )
        elif sec == "sc":
            nsc = sum(1 for d in decides if d == "storeconflict")
            check(
                nsc >= 8 and len(rel_it) == 0,
                "sc R: %d older-store "
                "conflicts rejected at the decision, none released (%d "
                "released)" % (nsc, len(rel_it)),
            )
        # measurements: dependent completion and section span
        if rel_it:
            gap_r = [
                tk(it["D"][1], "commit") - tk(it["L"][1], "commit")
                for it in rel_it
            ]
            gap_p = [
                tk(it["D"][1], "commit") - tk(it["L"][1], "commit")
                for it in ip[-len(rel_it) :]
            ]
            info(
                "%s: D commit - L commit: off %s, on %s cycles"
                % (sec, sorted(set(gap_p)), sorted(set(gap_r)))
            )
            check(
                max(gap_r) < min(gap_p),
                "%s: with the release the dependent's own functional-unit "
                "latency is overlapped with the producer's wait (D commit "
                "- L commit %d..%d vs %d..%d without)"
                % (sec, min(gap_r), max(gap_r), min(gap_p), max(gap_p)),
            )
        span = lambda its: tk(its[-1]["J"][1], "commit") - tk(
            its[0]["M"][1], "issue"
        )
        info(
            "%s: section span (first M issue to last J commit): off %d, on "
            "%d cycles" % (sec, span(ip), span(ir))
        )
    return summary


def lifecycle(out):
    for name in sorted(os.listdir(out)):
        d = os.path.join(out, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "R.rc")):
            continue
        print("--- " + name)
        run_pair(d, name)
        r = Trace(os.path.join(d, "R", "trace.txt"))
        rel = len(r.release)
        disc = [x for x in r.release if "commit" not in r.inst.get(x, {})]
        info(
            "%s: %d loads released, %d of them discarded before commit"
            % (name, rel, len(disc))
        )
        if name == "vastra-discard":
            check(
                len(disc) >= 1,
                "%s: a load released at its bypass "
                "success and then squashed leaves its marks balanced "
                "(%d such load(s); no clear event, release counted once)"
                % (name, len(disc)),
            )
            ev = {}
            for t, kind, seq, reg, c in r.events:
                if seq in disc:
                    ev.setdefault(seq, []).append(kind)
            check(
                all(v == ["mark", "release"] for v in ev.values()) and ev,
                "%s: the squashed released load has exactly one mark and "
                "one release event (%s)" % (name, list(ev.values())),
            )
        if name == "vastra-fault":
            check(
                rel == 0,
                "%s: loads that fault never reach the bypass "
                "point, so none is released (%d)" % (name, rel),
            )


def selftest(out):
    """The accounting check must fail on a damaged trace: a clear event
    repeated (a double decrement), a release without its mark, and a mark
    that is never decremented"""
    src = os.path.join(out, "R", "trace.txt")
    lines = open(src, errors="replace").read().splitlines(True)
    ev = [i for i, l in enumerate(lines) if "Unpredictable " in l]
    clear = next(i for i in ev if "Unpredictable clear" in lines[i])
    rel = next(i for i in ev if "Unpredictable release" in lines[i])
    mark = next(i for i in ev if "Unpredictable mark" in lines[i])
    base = os.path.join(out, "selftest")
    os.makedirs(base, exist_ok=True)
    cases = {
        "double-clear": lambda L: L[: clear + 1] + [L[clear]] + L[clear + 1 :],
        "release-no-mark": lambda L: [
            x
            for i, x in enumerate(L)
            if i
            != next(
                j
                for j in ev
                if "Unpredictable mark" in L[j]
                and re.search(
                    r"regIndex: %s "
                    % re.search(r"regIndex: (\d+)", L[rel]).group(1),
                    L[j],
                )
                and j < rel
                and re.search(ID.split("pc")[0], L[j]).group(0)
                == re.search(ID.split("pc")[0], L[rel]).group(0)
            )
        ],
        "mark-never-cleared": lambda L: [
            x for i, x in enumerate(L) if i != clear
        ],
    }
    global failures
    for name, f in cases.items():
        path = os.path.join(base, name + ".txt")
        open(path, "w").writelines(f(lines))
        before = failures
        saved = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            scoreboard_accounting(Trace(path), name, 0)
        finally:
            sys.stdout = saved
        detected = failures > before
        failures = before
        check(detected, "self-test: the accounting check detects '%s'" % name)


if __name__ == "__main__":
    {"directed": directed, "lifecycle": lifecycle, "selftest": selftest}[
        sys.argv[1]
    ](sys.argv[2])
    if failures:
        print("%d correctness check(s) failed" % failures)
        sys.exit(1)
