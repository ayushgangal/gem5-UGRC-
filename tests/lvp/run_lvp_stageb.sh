#!/bin/bash
#
# Stage B tests for load-to-load address speculation in MinorCPU.
#
#   run_lvp_stageb.sh check-asm [WORKLOAD]  build ldld and check its assembly
#   run_lvp_stageb.sh run [WORKLOAD]        check-asm, then run A, B, C and
#                                           CL and check the results
#   run_lvp_stageb.sh run-one WORKLOAD      what run does for one workload
#   run_lvp_stageb.sh threshold             the confidence-threshold test
#
# run starts run-one in a separate process for each workload, so that a
# failure anywhere in one workload (the build, the assembly check, ...)
# stops that workload before any simulation, and so that all of them are
# attempted.  The exit status is nonzero if any workload failed.  The old
# binary, assembly and results of a workload are removed before it is
# processed, so that nothing from an earlier run can be mistaken for this
# one.
#
# WORKLOAD is stable (every slot holds the same pointer), phase (the
# pointer alternates between two nodes in blocks of 64 slots, so used
# predictions are wrong and recovery is exercised), fanout (phase, with two
# dependent loads from each pointer), fault (a wrong prediction that
# points into a page that has been unmapped, so that the speculative load
# that uses it takes a translation fault), chain (three dependent loads, the
# middle one both a consumer and a producer) or all (the default, all five).
# Each is a separate binary built from ldld.c, with the address speculation
# window taken from the symbol table of its own binary.
#
# threshold is a separate configuration test, not a workload (all does not
# include it): the fault program, built into OUT/threshold, is run with LVP
# off (TA) and with prediction only (no address speculation) at
# lvpConfThreshold 1, 2 and 3 (T1, T2 and T3), and the checker compares the
# four runs.
#
# OUT (default /tmp/lvp-stage-b) is where everything is written, in a
# subdirectory for each workload.  gem5 must already be built
# (build/RISCV/gem5.opt).
#
# Every run uses RiscvMinorCPU with caches and enableIdling=False:
#   A   LVP disabled
#   B   enableLVP, no address speculation (prediction only)
#   C   enableLVP and address speculation, confined to sum_slots()
#   CL  C again, logging the LVP events instead of the committed trace.  For
#       the phase, fanout, fault and chain workloads, which recover from wrong
#       predictions,
#       the branch predictor events (the Branch debug flag) are logged too,
#       so that the checker can see the Fetch2 recovery hook run.  That adds
#       a line or more for every committed instruction, so the stable
#       workload, which has no recovery, does not log them.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/lvp
OUT=${OUT:-/tmp/lvp-stage-b}
CC=${CC:-riscv64-linux-gnu-gcc}
OBJDUMP=${OBJDUMP:-riscv64-linux-gnu-objdump}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CHECKER=${CHECKER:-$HERE/check_lvp_stageb.py}

# Set by select_workload
WORKLOAD=
WOUT=
BIN=
DEFS=()
# The number of loads that depend on the producer in sum_slots(), and the
# number of producers that have some (2 in a chain of three loads)
CONSUMERS=1
LINKS=1
# Set by check_asm, for the workload that was selected last
WIN_BEGIN=
WIN_END=

select_workload()
{
    case $1 in
    stable) DEFS=(); CONSUMERS=1; LINKS=1 ;;
    phase) DEFS=(-DLVP_PHASE); CONSUMERS=1; LINKS=1 ;;
    fanout) DEFS=(-DLVP_PHASE -DLVP_FANOUT); CONSUMERS=2; LINKS=1 ;;
    fault) DEFS=(-DLVP_FAULT); CONSUMERS=1; LINKS=1 ;;
    chain) DEFS=(-DLVP_CHAIN); CONSUMERS=1; LINKS=2 ;;
    *) echo "unknown workload: $1" >&2; exit 2 ;;
    esac
    WORKLOAD=$1
    WOUT=$OUT/$WORKLOAD
    BIN=$WOUT/ldld
    WIN_BEGIN=
    WIN_END=
    mkdir -p "$WOUT"
}

# Remove what an earlier run left for the selected workload: the binary, its
# disassembly and the load addresses derived from it.  Only these names,
# inside the workload's own directory.
clean_build()
{
    rm -f "${WOUT:?}/ldld" "${WOUT:?}/ldld.S" "${WOUT:?}/sum_slots.S" \
        "${WOUT:?}/load_pcs.txt"
}

# Remove the results of earlier runs of the selected workload, so that the
# checker can only see what this run wrote
clean_results()
{
    local n
    for n in A B C CL; do
        rm -rf "${WOUT:?}/$n"
        rm -f "${WOUT:?}/$n.out" "${WOUT:?}/$n.rc"
    done
}

build()
{
    # No C library is available to the cross compiler, and none is needed.
    # -n (no page alignment) puts everything in one loadable segment: the
    # program has no initialized data, so by default the linker makes a
    # separate segment for .bss with a file offset past the end of the file,
    # which gem5's ELF loader rejects.  The linker warns that the segment
    # is writable and executable, which does not matter in SE mode.
    # --no-relax-gp: the linker would otherwise address globals relative to
    # gp, which a program started by gem5 SE mode has to set up itself (it
    # starts at 0), and this program's _start does not.
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "${DEFS[@]}" \
        -o "$BIN" "$HERE/ldld.c"
}

check_asm()
{
    clean_build
    build
    "$OBJDUMP" -d --no-show-raw-insn "$BIN" > "$WOUT/ldld.S"
    sed -n '/<sum_slots>:/,/^$/p' "$WOUT/ldld.S" > "$WOUT/sum_slots.S"
    cat "$WOUT/sum_slots.S"

    # The address range of sum_slots from the symbol table, to be compared
    # with what objdump attributes to the function
    local addr size
    read -r addr size _ < <("$NM" -S --defined-only "$BIN" |
                            grep ' sum_slots$')

    # The whole disassembly: the dependent load pair, and that there is no
    # other load.  The test expectations in check_lvp_stageb.py rely on
    # what is checked here.
    python3 -I - "$WOUT/ldld.S" sum_slots "0x$addr" "0x$size" \
        "$CONSUMERS" "$WOUT/load_pcs.txt" "$LINKS" <<'PY'
import re
import sys

LOADS = re.compile(r"(l[bhwd]u?|fl[wd]|lr\.[wd]|amo\w+\.[wd]|sc\.[wd])$")
MEM_OPS = re.compile(r"(l[bhwd]u?|fl[wd]|s[bhwd]|fs[wd]|lr\.[wd]|"
                     r"amo\w+\.[wd]|sc\.[wd])$")
CONTROL = re.compile(r"(b[a-z]+|j|jal|jalr|jr|ret|call|tail)$")
# What objdump prints for bytes it can't (or didn't) decode as an
# instruction, or if it names compressed instructions as such.  Any of them
# could hide a load.  By default it does not name compressed instructions
# (a compressed load is printed as the ld or lw it expands to, so it is in
# the inventory), but a program built for rv64g has none, and they are found
# below by the address step to the next instruction.
HIDDEN = re.compile(r"(c\..*|\..*|unknown|\(bad\).*)$")
LD_OPS = re.compile(r"(\w+),(-?\d+)\((\w+)\)$")
# gp, as objdump spells it (gp, or x3 without ABI names), as the base of a
# memory operand ("-2024(gp)") or as a register operand.  The operands have
# had their comments (and the <symbol+offset> annotations after them)
# removed, and only whole operands are matched.
GP_BASE = re.compile(r"(-?\d+|-?0x[0-9a-f]+)?\((gp|x3)\)$")
GP_REG = re.compile(r"(gp|x3)$")


NO_FUNCTION = "(outside any function)"


def parse(path):
    """Returns the blocks, the number of lines that look like an instruction,
    counted separately from the parse.  A block is (name, [(address,
    mnemonic, operands)]): one for each function header, and one for each
    run of instructions outside any function.  A new section ends the
    current block."""
    blocks = []
    cur = None      # the current block, None until one starts
    looks_like = 0
    for line in open(path):
        if line.startswith("Disassembly of section"):
            cur = None
            continue
        m = re.match(r"[0-9a-f]+ <(\S+)>:", line)
        if m:
            cur = (m.group(1), [])
            blocks.append(cur)
            continue
        if re.match(r"\s*[0-9a-f]+:\s", line):
            looks_like += 1
        m = re.match(r"\s+([0-9a-f]+):\s+(\S+)\s*(.*)", line)
        if m:
            if cur is None:
                cur = (NO_FUNCTION, [])
                blocks.append(cur)
            cur[1].append((int(m.group(1), 16), m.group(2),
                           m.group(3).split("#")[0].strip()))
    return blocks, looks_like


def show(insts):
    return "\n".join("    %x: %s %s" % i for i in insts)


problems = []
blocks, looks_like = parse(sys.argv[1])
funcs = {}      # by name, for the load inventory
for fname, body in blocks:
    if fname != NO_FUNCTION:
        funcs.setdefault(fname, []).extend(body)
unscoped = [i for fname, body in blocks if fname == NO_FUNCTION for i in body]
name = sys.argv[2]
sym_addr, sym_size = int(sys.argv[3], 0), int(sys.argv[4], 0)
n_consumers = int(sys.argv[5])      # loads that depend on the producer
load_pcs_path = sys.argv[6]         # where the checker finds the load PCs
n_links = int(sys.argv[7])          # producers that have dependent loads

# Everything that looks like an instruction must have been parsed
parsed = sum(len(body) for body in funcs.values()) + len(unscoped)
print()
print("instructions: %d parsed, %d lines look like one" % (parsed, looks_like))
if parsed != looks_like:
    problems.append("the parser accounts for %d of the %d lines that look "
                    "like an instruction" % (parsed, looks_like))

# Every load in the whole binary.  The incorrect == 0 expectation covers
# every confident prediction in the run, so there must be no load that is
# not part of sum_slots.
for fname, body in funcs.items():
    loads = [i for i in body if LOADS.match(i[1])]
    print("loads in %-24s %d" % (fname + ":", len(loads)))
    if fname != name and loads:
        problems.append("loads outside %s, in %s:\n%s" %
                        (name, fname, show(loads)))
loads = [i for i in unscoped if LOADS.match(i[1])]
print("loads outside any function:  %d (of %d instructions)" %
      (len(loads), len(unscoped)))
if loads:
    problems.append("loads that are not in any function:\n%s" % show(loads))

# The program is built for rv64g, so every instruction is 4 bytes and the
# next one is 4 bytes on.  Only addresses in the same block are compared:
# different functions and sections have unrelated addresses.  A step of 2
# is a compressed instruction, and anything else is code or data that
# objdump skipped.
gaps = [(fname, a, b) for fname, body in blocks
        for a, b in zip(body, body[1:]) if b[0] - a[0] != 4]
if gaps:
    problems.append("instruction addresses that do not advance by 4 within "
                    "a function (%d):\n%s" % (len(gaps),
                    "\n".join("    %s: %x: %s %s -> %x (+%d)" %
                               ((f,) + a + (b[0], b[0] - a[0]))
                               for f, a, b in gaps[:10])))

# gem5 SE mode starts a program with gp = 0, so an instruction that uses gp
# (the linker makes gp-relative accesses when it relaxes) addresses the wrong
# memory unless the program sets gp, and this one does not
gp_uses = [(fname, i, "memory base" if GP_BASE.match(t) else "register")
           for fname, body in blocks for i in body
           for t in (o.strip() for o in i[2].split(","))
           if GP_BASE.match(t) or GP_REG.match(t)]
if gp_uses:
    problems.append("instructions that use gp, which nothing initializes "
                    "(%d):\n%s" % (len(gp_uses),
                    "\n".join("    %s: %x: %s %s   (gp as %s)" %
                               ((f,) + i + (kind,))
                               for f, i, kind in gp_uses[:10])))

hidden = [(fname, i) for fname, body in
          list(funcs.items()) + [("(outside any function)", unscoped)]
          for i in body if HIDDEN.match(i[1])]
if hidden:
    problems.append("instructions that are not decoded as instructions, "
                    "which the load inventory can't cover (%d):\n%s" %
                    (len(hidden),
                    "\n".join("    %s: %x: %s %s" % ((f,) + i)
                               for f, i in hidden[:10])))

# What objdump attributes to the function is exactly what the symbol table
# says: it starts at the symbol, its instructions follow each other (checked
# above), and the last one ends at the end of the symbol
named = [body for fname, body in blocks if fname == name]
if len(named) != 1:
    sys.exit("FAIL: %d disassembled blocks are named %s, expected 1" %
             (len(named), name))
insts = named[0]
if not insts:
    sys.exit("FAIL: %s has no instructions in the disassembly" % name)
end = sym_addr + sym_size
if sym_size % 4 or insts[0][0] != sym_addr or insts[-1][0] + 4 != end:
    problems.append("the disassembly of %s is %x-%x (each 4 bytes) but the "
                    "symbol table says [%x, %x), size %d" %
                    (name, insts[0][0], insts[-1][0], sym_addr, end,
                     sym_size))

# Producer and consumers: a load whose result is the base register of
# later loads, with nothing in between that overwrites that register.  The
# search for consumers stops at the expected number, or when the register is
# overwritten.
groups = []
for i, (addr, mn, ops) in enumerate(insts):
    m = LD_OPS.match(ops) if LOADS.match(mn) else None
    if not m:
        continue
    consumers = []
    for j in range(i + 1, min(i + 9, len(insts))):
        addr2, mn2, ops2 = insts[j]
        m2 = LD_OPS.match(ops2) if LOADS.match(mn2) else None
        if m2 and m2.group(3) == m.group(1):
            consumers.append(insts[j])
            if len(consumers) == n_consumers or m2.group(1) == m.group(1):
                break
            continue
        if ops2.split(",")[0] == m.group(1) and \
                not CONTROL.match(mn2) and not MEM_OPS.match(mn2):
            break   # the register is overwritten
    if consumers:
        last = insts.index(consumers[-1])
        groups.append((insts[i], consumers, insts[i + 1:last]))

mem_ops = [i for i in insts if MEM_OPS.match(i[1])]
print("memory operations in %s: %d" % (name, len(mem_ops)))

if len(groups) != n_links:
    problems.append("expected exactly %d producer(s) with dependent loads "
                    "in %s, found %d:\n%s" %
                    (n_links, name, len(groups), show(insts)))
for prod, consumers, between in groups:
    for cons in consumers:
        print("dependent load pair: %x %s %s  ->  %x %s %s" % (prod + cons))
    if len(consumers) != n_consumers:
        problems.append("expected %d loads that depend on the producer, "
                        "found %d:\n%s" %
                        (n_consumers, len(consumers),
                         show([prod] + between + consumers)))
    # The stable-pointer expectations (no squash) need the consumers to be
    # on the path of their producer
    control = [i for i in between if CONTROL.match(i[1])]
    if control:
        problems.append("branch or jump between the producer and the "
                        "consumers:\n%s" % show([prod] + between + consumers))
    if n_consumers > 1:
        # Every consumer must depend on the producer's register itself, so
        # none may overwrite it, and they must load different things
        base = LD_OPS.match(prod[2]).group(1)
        dests = [LD_OPS.match(c[2]).group(1) for c in consumers]
        offsets = [LD_OPS.match(c[2]).group(2) for c in consumers]
        if base in dests:
            problems.append("a consumer overwrites the producer's register "
                            "%s:\n%s" % (base, show([prod] + consumers)))
        if len(set(offsets)) != len(offsets):
            problems.append("consumers load from the same offset:\n%s" %
                            show([prod] + consumers))
if len(mem_ops) != 1 + n_links * n_consumers:
    problems.append("expected exactly %d memory operations in %s, "
                    "found %d:\n%s" %
                    (1 + n_links * n_consumers, name, len(mem_ops),
                     show(mem_ops)))

if n_links == 2 and len(groups) == 2 and \
        all(len(g[1]) == 1 for g in groups):
    # A chain of three loads: the consumer of the first producer is the
    # second producer, so L1 -> L2 -> L3, each at its own PC, with these
    # offsets and nothing but ld.  The groups were found one register at a
    # time, so L2's destination is L3's base and L1's is L2's.
    (l1, (l2,), between1), (l2b, (l3,), between2) = groups
    if l2 != l2b:
        problems.append("the consumer of the first load is not the producer "
                        "of the last:\n%s" % show([l1, l2, l2b, l3]))
    if len({l1[0], l2[0], l3[0]}) != 3:
        problems.append("the three loads are not at three distinct PCs:\n%s"
                        % show([l1, l2, l3]))
    offsets = [LD_OPS.match(i[2]).group(2) for i in (l1, l2, l3)]
    if offsets != ["0", "8", "24"] or [l1[1], l2[1], l3[1]] != ["ld"] * 3:
        problems.append("expected ld with offsets 0, 8 and 24, got:\n%s" %
                        show([l1, l2, l3]))
    if l1[0] > l2[0] or l2[0] > l3[0]:
        problems.append("the loads are not in program order:\n%s" %
                        show([l1, l2, l3]))

if problems:
    for p in problems:
        print("FAIL: " + p)
    sys.exit(1)
# The checker compares the committed loads with these
with open(load_pcs_path, "w") as f:
    if n_links == 2:
        # a chain: the loads in program order, L1, L2 and L3
        (l1, (l2,), _), (_, (l3,), _) = groups
        for load in (l1, l2, l3):
            f.write("chain 0x%x\n" % load[0])
    else:
        for prod, consumers, between in groups:
            f.write("producer 0x%x\n" % prod[0])
            for cons in consumers:
                f.write("consumer 0x%x\n" % cons[0])
print("PASS: producer and %d consumer(s) found, nothing between them changes "
      "control flow, and no load outside %s" % (n_consumers, name))
PY

    WIN_BEGIN=$(printf '0x%x' $((0x$addr)))
    WIN_END=$(printf '0x%x' $((0x$addr + 0x$size)))
    echo "sum_slots address range: [$WIN_BEGIN, $WIN_END)"
}

run_gem5()
{
    local name=$1 flags=$2
    shift 2

    rm -rf "${WOUT:?}/$name"
    local rc=0
    "$GEM5" -d "$WOUT/$name" --debug-flags="$flags" --debug-file=trace.txt \
        "$ROOT/configs/deprecated/example/se.py" \
        --cpu-type=RiscvMinorCPU --caches -c "$BIN" \
        -P 'system.cpu[0].enableIdling=False' "$@" \
        > "$WOUT/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$WOUT/$name.rc"
    echo "$WORKLOAD $name: exit status $rc"
}

# The workloads that an argument (default all) names.  Used in an
# assignment, so that an invalid argument stops the script.
workloads()
{
    case ${1:-all} in
    all) echo stable phase fanout fault chain ;;
    stable | phase | fanout | fault | chain) echo "$1" ;;
    *) echo "unknown workload: $1" >&2; exit 2 ;;
    esac
}

usage()
{
    sed -n '3,/^$/{/^$/!p}' "$0" >&2
    exit 2
}

run_workload()
{
    # Not in a condition: any failure here, in particular of the build and
    # of the assembly check, ends this process before gem5 is run
    clean_results
    check_asm
    TRACE=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
    LVP_ON=(-P 'system.cpu[0].enableLVP=True')
    SPEC=(-P 'system.cpu[0].lvpSpecAddr=True'
          -P "system.cpu[0].lvpSpecPcBegin=$WIN_BEGIN"
          -P "system.cpu[0].lvpSpecPcEnd=$WIN_END")

    run_gem5 A "$TRACE"
    run_gem5 B "$TRACE" "${LVP_ON[@]}" -P 'system.cpu[0].lvpSpecAddr=False'
    run_gem5 C "$TRACE" "${LVP_ON[@]}" "${SPEC[@]}"
    local cl_flags=LVP
    if [ "$WORKLOAD" != stable ]; then
        cl_flags=LVP,Branch
    fi
    run_gem5 CL "$cl_flags" "${LVP_ON[@]}" "${SPEC[@]}"

    python3 -I "$CHECKER" "$WOUT" "$WORKLOAD"
}

run_threshold()
{
    # The fault binary, in a directory of its own so that nothing here can
    # touch the results of the fault workload
    DEFS=(-DLVP_FAULT)
    CONSUMERS=1
    LINKS=1
    WORKLOAD=threshold
    WOUT=$OUT/threshold
    BIN=$WOUT/ldld
    WIN_BEGIN=
    WIN_END=
    mkdir -p "$WOUT"
    local n
    for n in TA T1 T2 T3; do
        rm -rf "${WOUT:?}/$n"
        rm -f "${WOUT:?}/$n.out" "${WOUT:?}/$n.rc"
    done
    check_asm

    local trace=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
    run_gem5 TA "$trace"
    local t
    for t in 1 2 3; do
        run_gem5 "T$t" "$trace" -P 'system.cpu[0].enableLVP=True' \
            -P 'system.cpu[0].lvpSpecAddr=False' \
            -P "system.cpu[0].lvpConfThreshold=$t"
    done

    python3 -I "$CHECKER" "$WOUT" threshold
}

case ${1:-} in
threshold)
    [ $# -eq 1 ] || usage
    run_threshold
    ;;
check-asm)
    [ $# -le 2 ] || usage
    list=$(workloads "${2:-all}")
    for w in $list; do
        select_workload "$w"
        echo "=== $w"
        check_asm
    done
    ;;
run)
    [ $# -le 2 ] || usage
    list=$(workloads "${2:-all}")
    status=0
    failed=
    for w in $list; do
        echo "=== $w"
        if ! bash "$0" run-one "$w"; then
            status=1
            failed="$failed $w"
        fi
    done
    if [ $status -ne 0 ]; then
        echo "FAILED workload(s):$failed"
    fi
    exit $status
    ;;
run-one)
    [ $# -eq 2 ] || usage
    case $2 in
    stable | phase | fanout | fault | chain) ;;
    *) echo "unknown workload: $2" >&2; exit 2 ;;
    esac
    select_workload "$2"
    run_workload
    ;;
*)
    usage
    ;;
esac
