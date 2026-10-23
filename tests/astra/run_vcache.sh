#!/bin/bash
#
# Tests of the shadow value cache (V$, src/cpu/minor/vcache.hh), on the research
# baseline configs/research/vp_baseline.py (see README.md, "Shadow V$ tests").
#
#   run_vcache.sh check-asm MODE   build the program of MODE and check its
#                                  disassembly
#   run_vcache.sh MODE             build, run, check
#   run_vcache.sh all              every MODE below, in this order
#
# MODE is one of
#   ops               the script of loads, stores and atomics (vcache.c)
#   squash            a wrong-path load that must leave no trace
#   fault             loads whose first execution takes a translation fault
#   noninterference   the ASTRA program and the LVP phase workload, with the
#                     V$ off and on
#
# OUT (default /tmp/vcache-test) is where everything is written, in a
# directory for each MODE.  gem5 must already be built.  The baseline is run
# through with_params.py, which only adds the -P options (enableVCache).

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/astra
OUT=${OUT:-/tmp/vcache-test}
CC=${CC:-riscv64-linux-gnu-gcc}
OBJDUMP=${OBJDUMP:-riscv64-linux-gnu-objdump}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CFG=${CFG:-$HERE/with_params.py}
CHECKER=${CHECKER:-$HERE/check_vcache.py}

TRACE=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
VC_ON=(-P 'system.cpu[0].enableVCache=True')

usage()
{
    sed -n '3,/^$/{/^$/!p}' "$0" >&2
    exit 2
}

# build_vc DIR MODE -D...: DIR/vc, DIR/vc.S, DIR/vc.nm.  Same link as
# run_astra.sh (no C library; -n gives one loadable segment; --no-relax-gp
# because SE mode starts with gp = 0)
build_vc()
{
    local dir=$1 mode=$2
    shift 2
    mkdir -p "$dir"
    rm -f "$dir/vc" "$dir/vc.S" "$dir/vc.nm" "$dir/pcs.txt"
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "$@" \
        -o "$dir/vc" "$HERE/vcache.c"
    "$OBJDUMP" -d --no-show-raw-insn "$dir/vc" > "$dir/vc.S"
    "$NM" -S --defined-only "$dir/vc" > "$dir/vc.nm"
    python3 -I "$CHECKER" asm "$mode" "$dir/vc.S" "$dir/vc.nm" "$dir/pcs.txt"
}

# run_gem5 DIR NAME FLAGS BINARY ARGS...: DIR/NAME (gem5's output), DIR/NAME.out
# and DIR/NAME.rc (its exit status)
run_gem5()
{
    local dir=$1 name=$2 flags=$3 bin=$4
    shift 4
    rm -rf "${dir:?}/$name" "${dir:?}/$name.out" "${dir:?}/$name.rc"
    local rc=0
    "$GEM5" -d "$dir/$name" --debug-flags="$flags" --debug-file=trace.txt \
        "$CFG" --cmd="$bin" "$@" > "$dir/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$dir/$name.rc"
    echo "$(basename "$dir") $name: exit status $rc"
}

# D: V$ off, S: V$ on, the same options otherwise
run_pair()
{
    local dir=$1 bin=$2 tag=$3
    shift 3
    run_gem5 "$dir" "${tag}D" "$TRACE,VCache" "$bin" "$@"
    run_gem5 "$dir" "${tag}S" "$TRACE,VCache" "$bin" "$@" "${VC_ON[@]}"
}

run_ops()
{
    local d=$OUT/ops
    rm -rf "${d:?}"
    build_vc "$d" ops -DVC_OPS
    run_pair "$d" "$d/vc" ""
    python3 -I "$CHECKER" ops "$d"
}

run_squash()
{
    local d=$OUT/squash
    rm -rf "${d:?}"
    build_vc "$d" squash -DVC_SQUASH
    # ASTRA is on in both runs: it logs the wrong-path load that is
    # discarded (it is looked up at issue)
    run_gem5 "$d" D "$TRACE,VCache,ASTRA" "$d/vc" --astra
    run_gem5 "$d" S "$TRACE,VCache,ASTRA" "$d/vc" --astra "${VC_ON[@]}"
    python3 -I "$CHECKER" squash "$d"
}

run_fault()
{
    local d=$OUT/fault
    rm -rf "${d:?}"
    build_vc "$d" fault -DVC_FAULT
    run_gem5 "$d" D "$TRACE,VCache,ASTRA" "$d/vc" --astra
    run_gem5 "$d" S "$TRACE,VCache,ASTRA" "$d/vc" --astra "${VC_ON[@]}"
    python3 -I "$CHECKER" fault "$d"
}

# The ASTRA test program (built by the ASTRA runner) with ASTRA, and the LVP
# phase workload (built by the LVP runner) with LVP address speculation and
# ASTRA, each with the V$ off (D) and on (S)
run_noninterference()
{
    local d=$OUT/noninterference
    rm -rf "${d:?}"
    mkdir -p "$d"
    OUT=$d/astra_build "$ROOT/tests/astra/run_astra.sh" check-asm \
        > "$d/astra_build.log" 2>&1
    run_pair "$d" "$d/astra_build/astra" astra --astra

    OUT=$d/lvp_build "$ROOT/tests/lvp/run_lvp_stageb.sh" check-asm phase \
        > "$d/lvp_build.log" 2>&1
    local pcs=$d/lvp_build/phase/load_pcs.txt p c c4
    p=$(awk '$1 == "producer" { print $2 }' "$pcs")
    c=$(awk '$1 == "consumer" { print $2 }' "$pcs")
    if [ "$(echo "$p" | wc -w)" -ne 1 ] || [ "$(echo "$c" | wc -w)" -ne 1 ]
    then
        echo "load_pcs.txt: expected one producer and one consumer" >&2
        exit 1
    fi
    c4=$(printf '0x%x' $((c + 4)))
    run_pair "$d" "$d/lvp_build/phase/ldld" lvp --lvp --lvp-spec-addr \
        --lvp-window "$p" "$c4" --astra
    python3 -I "$CHECKER" noninterference "$d"
}

case ${1:-} in
check-asm)
    [ $# -eq 2 ] || usage
    case $2 in
    ops) build_vc "$OUT/ops" ops -DVC_OPS ;;
    squash) build_vc "$OUT/squash" squash -DVC_SQUASH ;;
    fault) build_vc "$OUT/fault" fault -DVC_FAULT ;;
    *) usage ;;
    esac
    ;;
all)
    [ $# -eq 1 ] || usage
    status=0
    for m in ops squash fault noninterference; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
ops) [ $# -eq 1 ] || usage; run_ops ;;
squash) [ $# -eq 1 ] || usage; run_squash ;;
fault) [ $# -eq 1 ] || usage; run_fault ;;
noninterference) [ $# -eq 1 ] || usage; run_noninterference ;;
*) usage ;;
esac
