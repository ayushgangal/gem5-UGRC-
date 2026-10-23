#!/bin/bash
#
# Characterization of the stall-on-use baseline variant
# (configs/research/vp_stall_on_use.py) against the canonical baseline
# (configs/research/vp_baseline.py): see README.md.
#
#   run_sou.sh        build sou.c, run both configurations, check
#
# OUT (default /tmp/sou-test) is where everything is written; use a directory
# of your own.  gem5 must already be built.  No predictor is enabled.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/sou
OUT=${OUT:-/tmp/sou-test}
CC=${CC:-riscv64-linux-gnu-gcc}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
FLAGS=MinorExecute,MinorMem,ExecEnable,ExecUser

rm -rf "${OUT:?}"
mkdir -p "$OUT"
"$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
    -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start -o "$OUT/sou" "$HERE/sou.c" 2> /dev/null
: > "$OUT/pcs.txt"
for s in soa_L soa_I1 soa_I2 soa_I3 soa_D soa_J \
         sob_M sob_L sob_I1 sob_I2 sob_I3 sob_D sob_J; do
    "$NM" "$OUT/sou" | awk -v s="$s" '$3 == s { print s, "0x" $1 }' >> "$OUT/pcs.txt"
done

run()    # NAME CONFIG
{
    local rc=0
    "$GEM5" -d "$OUT/$1" --debug-flags="$FLAGS" --debug-file=trace.txt "$2" \
        --cmd="$OUT/sou" > "$OUT/$1.out" 2>&1 || rc=$?
    echo "$rc" > "$OUT/$1.rc"
    echo "$1: exit status $rc"
}
# b: canonical baseline, s: stall-on-use variant (same length names)
run b "$ROOT/configs/research/vp_baseline.py"
run s "$ROOT/configs/research/vp_stall_on_use.py"
python3 -I "$HERE/check_sou.py" "$OUT"
