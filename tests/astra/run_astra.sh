#!/bin/bash
#
# Tests for the ASTRA shadow predictor (src/cpu/minor/astra.hh).
#
#   run_astra.sh check-asm   build astra and check its assembly
#   run_astra.sh run         check-asm, then run D, S and SL and check them
#
# OUT (default /tmp/astra-test) is where everything is written.  gem5 must
# already be built (build/RISCV/gem5.opt).  The old binary, assembly and
# results are removed before each command, so nothing from an earlier run can
# be mistaken for this one, and a failure of the build or of the assembly
# check stops the script before gem5 runs.
#
# Every run uses RiscvMinorCPU with caches, enableIdling=False and LVP
# disabled (the default), so that ASTRA is the only predictor:
#   D   ASTRA disabled (the baseline)
#   S   enableAstra: ASTRA in shadow mode, the committed instruction trace
#   SL  S again, with the ASTRA log in the trace as well
# D and SL are run with the ASTRA debug flag too, so that the checker can
# see that a disabled predictor logs nothing.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/astra
OUT=${OUT:-/tmp/astra-test}
CC=${CC:-riscv64-linux-gnu-gcc}
OBJDUMP=${OBJDUMP:-riscv64-linux-gnu-objdump}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CHECKER=${CHECKER:-$HERE/check_astra.py}
ASMCHECK=${ASMCHECK:-$HERE/check_asm.py}

BIN=$OUT/astra

usage()
{
    sed -n '3,/^$/{/^$/!p}' "$0" >&2
    exit 2
}

build()
{
    mkdir -p "$OUT"
    rm -f "$BIN" "$OUT/astra.S" "$OUT/symbols.txt" "$OUT/load_pcs.txt"

    # Same link as tests/lvp/run_lvp_stageb.sh: no C library; -n gives one
    # loadable segment (gem5's loader rejects the separate .bss segment);
    # --no-relax-gp because SE mode starts with gp = 0 and _start does not
    # set it
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start \
        -o "$BIN" "$HERE/astra.c"
}

check_asm()
{
    build
    "$OBJDUMP" -d --no-show-raw-insn "$BIN" > "$OUT/astra.S"
    "$NM" -S --defined-only "$BIN" > "$OUT/symbols.txt"
    python3 -I "$ASMCHECK" "$OUT/astra.S" "$OUT/symbols.txt" \
        "$OUT/load_pcs.txt"
}

run_gem5()
{
    local name=$1 flags=$2
    shift 2

    rm -rf "${OUT:?}/$name"
    local rc=0
    "$GEM5" -d "$OUT/$name" --debug-flags="$flags" --debug-file=trace.txt \
        "$ROOT/configs/deprecated/example/se.py" \
        --cpu-type=RiscvMinorCPU --caches -c "$BIN" \
        -P 'system.cpu[0].enableIdling=False' "$@" \
        > "$OUT/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$OUT/$name.rc"
    echo "astra $name: exit status $rc"
}

run()
{
    local n
    for n in D S SL; do
        rm -rf "${OUT:?}/$n"
        rm -f "${OUT:?}/$n.out" "${OUT:?}/$n.rc"
    done
    check_asm

    local trace=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr
    trace=$trace,FmtTicksOff
    local on=(-P 'system.cpu[0].enableAstra=True'
              -P 'system.cpu[0].astraTableEntries=1024')

    run_gem5 D "$trace,ASTRA"
    run_gem5 S "$trace" "${on[@]}"
    run_gem5 SL "$trace,ASTRA" "${on[@]}"

    python3 -I "$CHECKER" "$OUT"
}

case ${1:-} in
check-asm)
    [ $# -eq 1 ] || usage
    check_asm
    ;;
run)
    [ $# -eq 1 ] || usage
    run
    ;;
*)
    usage
    ;;
esac
