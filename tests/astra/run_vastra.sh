#!/bin/bash
#
# Tests of the diagnostic V-ASTRA probe (src/cpu/minor/vastra.hh), on the
# research baseline configs/research/vp_baseline.py (see README.md,
# "Diagnostic V-ASTRA probe").
#
#   run_vastra.sh check-asm MODE   build the program of MODE and check it
#   run_vastra.sh MODE             build, run, check
#   run_vastra.sh all              every MODE below, in this order
#
# MODE is one of
#   classify          base correct and wrong, tag miss, byte miss, hit, and
#                     a load that crosses a 16 byte boundary
#   store             a conflicting older store in flight, and a control
#   discard           a candidate on a wrong path
#   fault             candidates whose load takes a translation fault
#   noninterference   the ASTRA program and the LVP phase workload with the
#                     probe off and on
#   verify-detect     strict verification stops the run when the scan of the
#                     older stores is switched off (a stale candidate)
#
# OUT (default /tmp/vastra-test) is where everything is written, in a
# directory for each MODE.  gem5 must already be built.  ASTRA and the
# shadow V$ are on in every run (the probe needs both); D has the probe off,
# S has it on, and V has it on with strict verification (enableVAstraVerify).

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/astra
OUT=${OUT:-/tmp/vastra-test}
CC=${CC:-riscv64-linux-gnu-gcc}
OBJDUMP=${OBJDUMP:-riscv64-linux-gnu-objdump}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CFG=${CFG:-$HERE/with_params.py}
CHECKER=${CHECKER:-$HERE/check_vastra.py}

TRACE=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
FLAGS=$TRACE,ASTRA,VCache,VAstra
BASE=(--astra -P 'system.cpu[0].enableVCache=True')
DIAG=(-P 'system.cpu[0].enableVAstraDiag=True')
VERIFY=(-P 'system.cpu[0].enableVAstraVerify=True')
SKIPSCAN=(-P 'system.cpu[0].vastraTestSkipStoreScan=True')

usage()
{
    sed -n '3,/^$/{/^$/!p}' "$0" >&2
    exit 2
}

# build_va DIR MODE -D...: DIR/va, DIR/va.S, DIR/va.nm (the same link as
# run_astra.sh: no C library; -n gives one loadable segment; --no-relax-gp
# because SE mode starts with gp = 0)
build_va()
{
    local dir=$1 mode=$2
    shift 2
    mkdir -p "$dir"
    rm -f "$dir/va" "$dir/va.S" "$dir/va.nm" "$dir/pcs.txt"
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "$@" \
        -o "$dir/va" "$HERE/vastra.c"
    "$OBJDUMP" -d --no-show-raw-insn "$dir/va" > "$dir/va.S"
    "$NM" -S --defined-only "$dir/va" > "$dir/va.nm"
    python3 -I "$CHECKER" asm "$mode" "$dir/va.S" "$dir/va.nm" "$dir/pcs.txt"
}

# run_gem5 DIR NAME BINARY ARGS...: DIR/NAME (gem5's output), DIR/NAME.out and
# DIR/NAME.rc (its exit status)
run_gem5()
{
    local dir=$1 name=$2 bin=$3
    shift 3
    rm -rf "${dir:?}/$name" "${dir:?}/$name.out" "${dir:?}/$name.rc"
    local rc=0
    # NOCORE: a run that is expected to panic must not leave a core file
    # (in the current directory) behind
    if [ -n "${NOCORE:-}" ]; then
        (ulimit -c 0; cd "$dir"; exec "$GEM5" -d "$dir/$name" \
            --debug-flags="$FLAGS" --debug-file=trace.txt "$CFG" \
            --cmd="$bin" "$@") > "$dir/$name.out" 2>&1 || rc=$?
    else
        "$GEM5" -d "$dir/$name" --debug-flags="$FLAGS" \
            --debug-file=trace.txt "$CFG" --cmd="$bin" "$@" \
            > "$dir/$name.out" 2>&1 || rc=$?
    fi
    echo "$rc" > "$dir/$name.rc"
    echo "$(basename "$dir") $name: exit status $rc"
}

run_pair()
{
    local dir=$1 bin=$2 tag=$3
    shift 3
    run_gem5 "$dir" "${tag}D" "$bin" "${BASE[@]}" "$@"
    run_gem5 "$dir" "${tag}S" "$bin" "${BASE[@]}" "$@" "${DIAG[@]}"
    run_gem5 "$dir" "${tag}V" "$bin" "${BASE[@]}" "$@" "${DIAG[@]}" \
        "${VERIFY[@]}"
}

run_program()
{
    local mode=$1 define=$2 d=$OUT/$1
    rm -rf "${d:?}"
    build_va "$d" "$mode" "$define"
    run_pair "$d" "$d/va" ""
    python3 -I "$CHECKER" "$mode" "$d"
}

run_noninterference()
{
    local d=$OUT/noninterference
    rm -rf "${d:?}"
    mkdir -p "$d"
    OUT=$d/astra_build "$ROOT/tests/astra/run_astra.sh" check-asm \
        > "$d/astra_build.log" 2>&1
    run_pair "$d" "$d/astra_build/astra" astra

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
    cp "$pcs" "$d/load_pcs.txt"
    run_pair "$d" "$d/lvp_build/phase/ldld" lvp --lvp --lvp-spec-addr \
        --lvp-window "$p" "$c4"
    python3 -I "$CHECKER" noninterference "$d"
}

# The store program with the scan of the older stores switched off: X0 is
# the normal run with strict verification (it passes), X1 admits stale
# candidates (counted, not checked), X2 is X1 with strict verification and
# must stop
run_verify_detect()
{
    local d=$OUT/verify-detect
    rm -rf "${d:?}"
    build_va "$d" store -DVA_STORE
    run_gem5 "$d" X0 "$d/va" "${BASE[@]}" "${DIAG[@]}" "${VERIFY[@]}"
    run_gem5 "$d" X1 "$d/va" "${BASE[@]}" "${DIAG[@]}" "${SKIPSCAN[@]}"
    NOCORE=1 run_gem5 "$d" X2 "$d/va" "${BASE[@]}" "${DIAG[@]}" \
        "${SKIPSCAN[@]}" "${VERIFY[@]}"
    python3 -I "$CHECKER" verify-detect "$d"
}

case ${1:-} in
check-asm)
    [ $# -eq 2 ] || usage
    case $2 in
    classify) build_va "$OUT/classify" classify -DVA_CLASSIFY ;;
    store) build_va "$OUT/store" store -DVA_STORE ;;
    discard) build_va "$OUT/discard" discard -DVA_DISCARD ;;
    fault) build_va "$OUT/fault" fault -DVA_FAULT ;;
    *) usage ;;
    esac
    ;;
all)
    [ $# -eq 1 ] || usage
    status=0
    for m in classify store discard fault noninterference verify-detect; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
classify) [ $# -eq 1 ] || usage; run_program classify -DVA_CLASSIFY ;;
store) [ $# -eq 1 ] || usage; run_program store -DVA_STORE ;;
discard) [ $# -eq 1 ] || usage; run_program discard -DVA_DISCARD ;;
fault) [ $# -eq 1 ] || usage; run_program fault -DVA_FAULT ;;
noninterference) [ $# -eq 1 ] || usage; run_noninterference ;;
verify-detect) [ $# -eq 1 ] || usage; run_verify_detect ;;
*) usage ;;
esac
