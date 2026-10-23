#!/usr/bin/env python3
"""Checks the disassembly of astra.c and writes load_pcs.txt.

Usage: check_asm.py ASTRA_S SYMBOLS LOAD_PCS_OUT

ASTRA_S is `objdump -d --no-show-raw-insn` of the linked binary, SYMBOLS is
`nm -S --defined-only` of it.  The compiler must have emitted exactly the
loads that tests/astra/astra.c is written to produce, in their functions and
in program order, and no other load anywhere in the binary, because the
test's expectations (and the ASTRA golden model) are built on that load
inventory.  A failure prints what was found and exits 1.

load_pcs.txt has a line "section slot 0xPC imm" for each expected load, in
program order.
"""

import re
import sys

# function -> the immediates of its loads, in program order
EXPECTED = {
    "sec_const": [0],
    "sec_pos": [0],
    "sec_neg": [0],
    "sec_chg": [0],
    "sec_imm": [24, -16],
    "sec_pair": [0, 0],
    "sec_conf": [0, 0],
}

LOAD = re.compile(r"^(l[bhwd]u?|fl[wd]|lr\.[wd]|amo\w+\.[wd]|sc\.[wd])$")
HEADER = re.compile(r"^([0-9a-f]+) <([^>]+)>:")
INSN = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)$")
OPERAND = re.compile(r"^(\w+),(-?\d+)\((\w+)\)")
# The ASTRA lookup index is (pc >> 1) & (entries - 1); tests use 1024
ENTRIES = 1024


def parse(path):
    funcs = {}
    current = None
    count = 0
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            h = HEADER.match(line)
            if h:
                current = h.group(2)
                funcs.setdefault(current, [])
                continue
            m = INSN.match(line)
            if m and current is not None:
                count += 1
                op = m.group(3).split("#")[0].strip()
                funcs[current].append((int(m.group(1), 16), m.group(2), op))
    return funcs, count


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    funcs, count = parse(sys.argv[1])
    problems = []
    found = {}
    for name, insts in funcs.items():
        loads = [i for i in insts if LOAD.match(i[1])]
        if name not in EXPECTED:
            if loads:
                problems.append(
                    "loads in %s, which should have none: %s"
                    % (name, ["%x %s %s" % l for l in loads])
                )
            continue
        found[name] = loads

    for name, imms in EXPECTED.items():
        loads = found.get(name)
        if loads is None:
            problems.append("function %s not found" % name)
            continue
        got = []
        for pc, mnem, op in loads:
            m = OPERAND.match(op)
            if mnem != "ld" or not m:
                problems.append(
                    "%s: unexpected load %x %s %s" % (name, pc, mnem, op)
                )
                got.append(None)
            else:
                got.append(int(m.group(2)))
        if got != imms:
            problems.append(
                "%s: loads have immediates %s, expected %s\n%s"
                % (
                    name,
                    got,
                    imms,
                    "\n".join("  %x %s %s" % i for i in funcs[name]),
                )
            )

    for name, insts in funcs.items():
        for pc, mnem, op in insts:
            if mnem.startswith("c.") or re.search(r"\b(gp|x3)\b", op):
                problems.append(
                    "%s: %x %s %s (compressed or gp use)"
                    % (name, pc, mnem, op)
                )

    rows = []
    for name, imms in EXPECTED.items():
        for slot, (pc, mnem, op) in enumerate(found.get(name, [])):
            m = OPERAND.match(op)
            rows.append(
                (name[len("sec_") :], slot, pc, int(m.group(2)) if m else 0)
            )

    indexes = {}
    for name, slot, pc, imm in rows:
        indexes.setdefault((pc >> 1) & (ENTRIES - 1), []).append(
            "%s/%d" % (name, slot)
        )
    for index, who in indexes.items():
        if len(who) > 1:
            problems.append(
                "loads %s share LAT index %d: the test expects distinct "
                "entries" % (who, index)
            )

    syms = {}
    with open(sys.argv[2]) as f:
        for line in f:
            t = line.split()
            if len(t) == 4:
                syms[t[3]] = int(t[0], 16)
    for need in ("bufA", "bufB"):
        if need not in syms:
            problems.append("symbol %s not found" % need)

    print("instructions parsed: %d" % count)
    for name, slot, pc, imm in rows:
        print("load %-9s slot %d  pc 0x%x  imm %d" % (name, slot, pc, imm))
    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 1

    with open(sys.argv[3], "w") as f:
        for name, slot, pc, imm in rows:
            f.write("%s %d 0x%x %d\n" % (name, slot, pc, imm))
    print("PASS: %d loads in %d functions, no other load" % (len(rows), 7))
    return 0


if __name__ == "__main__":
    sys.exit(main())
