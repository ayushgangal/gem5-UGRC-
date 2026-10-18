#!/bin/bash
#
# Cycle comparison of the stable load-to-load workload (ldld.c) with LVP
# disabled and enabled.
#
#   run_lvp_cycles.sh        build and check ldld, run OFF, CTRL and ON, report
#
# OUT (default /tmp/lvp-cycles) is where everything is written.  gem5 must
# already be built (build/RISCV/gem5.opt).  The binary is built and its
# assembly checked by run_lvp_stageb.sh (check-asm stable), so it is the
# exact binary of the stable tests; nothing is run if that fails.
#
# Every run is the same command except the LVP parameters, with the native
# -P parameter overrides of configs/deprecated/example/se.py:
#   OFF   LVP disabled (the defaults: enableLVP=False)
#   CTRL  enableLVP=True, lvpSpecAddr=False (prediction only: a control,
#         which must cost nothing)
#   ON    enableLVP=True, lvpSpecAddr=True (speculation)
# Same binary, RiscvMinorCPU, --caches, enableIdling=False, clocks and
# memory defaults.  ASTRA is not enabled; an ASTRA parameter is not passed,
# so that the script also works with a gem5 built before ASTRA existed (the
# checker requires enableAstra to be false if the binary has it).  No window
# is set for ON: ldld has no load outside sum_slots().

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/lvp
OUT=${OUT:-/tmp/lvp-cycles}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CHECKER=${CHECKER:-$HERE/lvp_cycles.py}

mkdir -p "$OUT"
rm -rf "${OUT:?}/runs"
mkdir -p "$OUT/runs"

# Build and check the assembly of the stable workload before any simulation
OUT=$OUT "$HERE/run_lvp_stageb.sh" check-asm stable > "$OUT/check_asm.log"
tail -3 "$OUT/check_asm.log"
BIN=$OUT/stable/ldld

run_gem5()
{
    local name=$1
    shift
    local rc=0
    "$GEM5" -d "$OUT/runs/$name" \
        "$ROOT/configs/deprecated/example/se.py" \
        --cpu-type=RiscvMinorCPU --caches -c "$BIN" \
        -P 'system.cpu[0].enableIdling=False' "$@" \
        > "$OUT/runs/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$OUT/runs/$name.rc"
    echo "$name: exit status $rc"
}

run_gem5 OFF
run_gem5 CTRL -P 'system.cpu[0].enableLVP=True' \
    -P 'system.cpu[0].lvpSpecAddr=False'
run_gem5 ON -P 'system.cpu[0].enableLVP=True' \
    -P 'system.cpu[0].lvpSpecAddr=True'

python3 -I "$CHECKER" "$OUT/runs"
