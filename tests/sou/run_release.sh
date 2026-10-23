#!/bin/bash
#
# Tests of Design A, the V-ASTRA scoreboard release
# (enableVAstraScoreboardRelease), always under the stall-on-use configuration
# configs/research/vp_stall_on_use.py (see README.md, "Design A").
#
#   run_release.sh directed    release.c: feature off (P) and on (R)
#   run_release.sh lifecycle   the programs of run_vastra.sh and run_bypass.sh
#                              (discard, fault, store, classify, atomic,
#                              abandon, split, fp, syscall, latency): P and R,
#                              equivalence and scoreboard accounting
#   run_release.sh selftest    the scoreboard accounting check detects damaged
#                              traces (run after directed)
#   run_release.sh config      the parameter is rejected without bypass
#   run_release.sh all
#
# OUT (default /tmp/release-test) is where everything is written.  gem5 must
# already be built.  P and R have ASTRA, the V$, the V-ASTRA probe and the
# bypass on; only enableVAstraScoreboardRelease differs.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/sou
OUT=${OUT:-/tmp/release-test}
CC=${CC:-riscv64-linux-gnu-gcc}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CFG=$ROOT/tests/astra/with_params.py
export VP_CONFIG=$ROOT/configs/research/vp_stall_on_use.py

FLAGS=MinorExecute,MinorMem,MinorScoreboard,VBypass,VAstra,ExecEnable,ExecUser
BASE=(--astra -P 'system.cpu[0].enableVCache=True'
      -P 'system.cpu[0].enableVAstraDiag=True'
      -P 'system.cpu[0].enableVAstraBypass=True')
REL=(-P 'system.cpu[0].enableVAstraScoreboardRelease=True')

build()    # SRC OUTBIN [-D...]
{
    local src=$1 bin=$2
    shift 2
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "$@" -o "$bin" "$src" 2> /dev/null
}

pair()    # DIR BINARY [extra gem5 script args]
{
    local d=$1 bin=$2 v rc
    shift 2
    for v in P R; do
        local x=()
        [ $v = R ] && x=("${REL[@]}")
        rc=0
        (ulimit -c 0; "$GEM5" -d "$d/$v" --debug-flags="$FLAGS" \
            --debug-file=trace.txt "${PAIRCFG:-$CFG}" --cmd="$bin" "${BASE[@]}" "${x[@]}" \
            "$@" > "$d/$v.out" 2>&1) || rc=$?
        echo "$rc" > "$d/$v.rc"
        echo "$(basename "$d") $v: exit status $rc"
    done
}

run_directed()
{
    local d=$OUT/directed
    rm -rf "${d:?}"
    mkdir -p "$d"
    build "$HERE/release.c" "$d/rel"
    : > "$d/pcs.txt"
    for s in ca bw tm sc; do
        for i in M L I1 I2 D J; do
            "$NM" "$d/rel" | awk -v s="${s}_$i" '$3 == s { print s, "0x" $1 }' \
                >> "$d/pcs.txt"
        done
    done
    pair "$d" "$d/rel"
    python3 -I "$HERE/check_release.py" directed "$d"
}

# lifecycle: a directory per program with P and R
run_lifecycle()
{
    local l=$OUT/lifecycle
    rm -rf "${l:?}"
    mkdir -p "$l"
    local m
    for m in discard fault store classify; do
        mkdir -p "$l/vastra-$m"
        OUT=$l/vastra-$m/build bash "$ROOT/tests/astra/run_vastra.sh" \
            check-asm "$m" > "$l/vastra-$m/build.log" 2>&1
        pair "$l/vastra-$m" "$l/vastra-$m/build/$m/va"
    done
    for m in ATOMIC ABANDON SPLIT FP SYSCALL SUB SPECIAL; do
        mkdir -p "$l/bypass-$m"
        build "$ROOT/tests/astra/bypass.c" "$l/bypass-$m/bp" -DBP_"$m"
        pair "$l/bypass-$m" "$l/bypass-$m/bp"
    done
    # drain and statistics reset at every boundary (tests/astra/drain_reset.py
    # runs the same configuration through VP_CONFIG): with a drain (nothing in
    # flight at the boundary) and without (releases pending across a reset)
    for mode in drain reset; do
        for m in classify; do
            mkdir -p "$l/$mode-$m"
            PAIRCFG=$ROOT/tests/astra/drain_reset.py DRAIN_MODE=$mode \
                DRAIN_CHUNK=1500000 pair "$l/$mode-$m" "$l/vastra-$m/build/$m/va"
        done
        mkdir -p "$l/$mode-SYSCALL"
        PAIRCFG=$ROOT/tests/astra/drain_reset.py DRAIN_MODE=$mode \
            DRAIN_CHUNK=1500000 pair "$l/$mode-SYSCALL" "$l/bypass-SYSCALL/bp"
    done
    python3 -I "$HERE/check_release.py" lifecycle "$l"
}

run_config()
{
    local d=$OUT/config rc=0
    rm -rf "${d:?}"
    mkdir -p "$d"
    build "$HERE/release.c" "$d/rel"
    # without bypass: rejected
    "$GEM5" -d "$d/nobypass" "$CFG" --cmd="$d/rel" --astra \
        -P 'system.cpu[0].enableVCache=True' \
        -P 'system.cpu[0].enableVAstraDiag=True' "${REL[@]}" \
        > "$d/nobypass.out" 2>&1 || rc=$?
    if [ $rc -ne 0 ] && ! grep -q "Beginning simulation" "$d/nobypass.out" &&
        grep -q "enableVAstraScoreboardRelease requires enableVAstraBypass" \
            "$d/nobypass.out"; then
        echo "PASS  release without bypass is rejected"
    else
        echo "FAIL  release without bypass was not rejected (rc $rc)"; return 1
    fi
    # the default is off in config.ini of a normal run
    rc=0
    "$GEM5" -d "$d/default" "$CFG" --cmd="$d/rel" --astra \
        > "$d/default.out" 2>&1 || rc=$?
    if [ $rc -eq 0 ] && grep -q "enableVAstraScoreboardRelease=false" \
        "$d/default/config.ini"; then
        echo "PASS  enableVAstraScoreboardRelease defaults to false"
    else
        echo "FAIL  default is not false (rc $rc)"; return 1
    fi
}

case ${1:-} in
directed) run_directed ;;
lifecycle) run_lifecycle ;;
config) run_config ;;
selftest) python3 -I "$HERE/check_release.py" selftest "$OUT/directed" ;;
all)
    status=0
    for m in config directed selftest lifecycle; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
*) sed -n '3,/^$/{/^$/!p}' "$0" >&2; exit 2 ;;
esac
