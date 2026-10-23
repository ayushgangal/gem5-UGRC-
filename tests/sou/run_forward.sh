#!/bin/bash
#
# Tests of Design B, V-ASTRA forwarding to dependent loads
# (enableVAstraForwarding; see README.md, "Design B").
#
#   run_forward.sh directed    forward.c, forwarding off (P) and on (R), in the
#                              default timing configuration and in the
#                              stall-on-use configuration
#   run_forward.sh lifecycle   other programs, P and R (equivalence, record and
#                              consumer lifecycle, scoreboard accounting,
#                              drain and statistics reset)
#   run_forward.sh config      invalid combinations are rejected
#   run_forward.sh all
#
# OUT (default /tmp/forward-test) is where everything is written.  gem5 must
# already be built.  P and R have ASTRA, the V$, the V-ASTRA probe, the bypass
# and the scoreboard release on; only enableVAstraForwarding differs.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/sou
OUT=${OUT:-/tmp/forward-test}
CC=${CC:-riscv64-linux-gnu-gcc}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CFG=$ROOT/tests/astra/with_params.py
FLAGS=MinorExecute,MinorMem,MinorScoreboard,VBypass,VAstra,ExecEnable,ExecUser,ExecResult,ExecEffAddr
BASE=(--astra -P 'system.cpu[0].enableVCache=True'
      -P 'system.cpu[0].enableVAstraDiag=True'
      -P 'system.cpu[0].enableVAstraBypass=True'
      -P 'system.cpu[0].enableVAstraScoreboardRelease=True')
FWD=(-P 'system.cpu[0].enableVAstraForwarding=True')

build()    # SRC OUTBIN [-D...]
{
    local src=$1 bin=$2
    shift 2
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "$@" -o "$bin" "$src" 2> /dev/null
}

pair()    # DIR BINARY CONFIG(file) [extra args]
{
    local d=$1 bin=$2 config=$3 v rc
    shift 3
    for v in P R; do
        local x=()
        [ $v = R ] && x=("${FWD[@]}")
        rc=0
        (ulimit -c 0; VP_CONFIG=$config "$GEM5" -d "$d/$v" --debug-flags="$FLAGS" \
            --debug-file=trace.txt "${PAIRCFG:-$CFG}" --cmd="$bin" "${BASE[@]}" \
            "${x[@]}" "$@" > "$d/$v.out" 2>&1) || rc=$?
        echo "$rc" > "$d/$v.rc"
        echo "$(basename "$d") $v: exit status $rc"
    done
}

run_directed()
{
    local name config cfgfile
    for name in stall-on-use default; do
        local d=$OUT/directed-$name
        if [ $name = default ]; then
            cfgfile=$ROOT/configs/research/vp_baseline.py
            config=default
        else
            cfgfile=$ROOT/configs/research/vp_stall_on_use.py
            config=stall-on-use
        fi
        rm -rf "${d:?}"
        mkdir -p "$d"
        build "$HERE/forward.c" "$d/fw"
        : > "$d/pcs.txt"
        for s in ch bw tm sc ab pd cm ft sq; do
            for i in M S P C E U; do
                "$NM" "$d/fw" | awk -v s="${s}_$i" '$3 == s { print s, "0x" $1 }' \
                    >> "$d/pcs.txt"
            done
        done
        echo "$config" > "$d/config.txt"
        pair "$d" "$d/fw" "$cfgfile"
        echo "=== directed ($config)"
        python3 -I "$HERE/check_forward.py" directed "$d"
    done
}

run_lifecycle()
{
    local l=$OUT/lifecycle m
    rm -rf "${l:?}"
    mkdir -p "$l"
    local sou=$ROOT/configs/research/vp_stall_on_use.py
    local def=$ROOT/configs/research/vp_baseline.py
    for m in discard fault store classify; do
        mkdir -p "$l/vastra-$m"
        OUT=$l/vastra-$m/build bash "$ROOT/tests/astra/run_vastra.sh" \
            check-asm "$m" > "$l/vastra-$m/build.log" 2>&1
        pair "$l/vastra-$m" "$l/vastra-$m/build/$m/va" "$def"
    done
    for m in ATOMIC ABANDON SPLIT FP SYSCALL SUB SPECIAL; do
        mkdir -p "$l/bypass-$m"
        build "$ROOT/tests/astra/bypass.c" "$l/bypass-$m/bp" -DBP_"$m"
        pair "$l/bypass-$m" "$l/bypass-$m/bp" "$def"
    done
    # the LVP workloads are natural load-to-load chains (the LVP predictor
    # alone, without address speculation: forwarding excludes lvpSpecAddr)
    for m in stable phase; do
        mkdir -p "$l/ldld-$m"
        OUT=$l/ldld-$m/build bash "$ROOT/tests/lvp/run_lvp_stageb.sh" \
            check-asm "$m" > "$l/ldld-$m/build.log" 2>&1
        pair "$l/ldld-$m" "$l/ldld-$m/build/$m/ldld" "$def"
        mkdir -p "$l/ldld-$m-sou"
        pair "$l/ldld-$m-sou" "$l/ldld-$m/build/$m/ldld" "$sou"
    done
    for mode in drain reset; do
        mkdir -p "$l/$mode-classify" "$l/$mode-SYSCALL"
        PAIRCFG=$ROOT/tests/astra/drain_reset.py DRAIN_MODE=$mode \
            DRAIN_CHUNK=1500000 pair "$l/$mode-classify" \
            "$l/vastra-classify/build/classify/va" "$def"
        PAIRCFG=$ROOT/tests/astra/drain_reset.py DRAIN_MODE=$mode \
            DRAIN_CHUNK=1500000 pair "$l/$mode-SYSCALL" "$l/bypass-SYSCALL/bp" "$def"
        mkdir -p "$l/$mode-forward"
        build "$HERE/forward.c" "$l/$mode-forward/fw"
        PAIRCFG=$ROOT/tests/astra/drain_reset.py DRAIN_MODE=$mode \
            DRAIN_CHUNK=3000000 pair "$l/$mode-forward" "$l/$mode-forward/fw" "$def"
    done
    python3 -I "$HERE/check_forward.py" lifecycle "$l"
}

run_config()
{
    local d=$OUT/config rc=0 n=0 bad=0
    rm -rf "${d:?}"
    mkdir -p "$d"
    build "$HERE/forward.c" "$d/fw"
    cfgcase()    # NAME EXPECT(ok|MESSAGE) args...
    {
        local name=$1 expect=$2
        shift 2
        rc=0
        "$GEM5" -d "$d/$name" "$CFG" --cmd="$d/fw" "$@" > "$d/$name.out" 2>&1 || rc=$?
        if [ "$expect" = ok ]; then
            if [ $rc -eq 0 ] && grep -q "Beginning simulation" "$d/$name.out"; then
                echo "PASS  $name: accepted"
            else echo "FAIL  $name: rejected (rc $rc)"; bad=$((bad + 1)); fi
        else
            if [ $rc -ne 0 ] && ! grep -q "Beginning simulation" "$d/$name.out" &&
                grep -q "$expect" "$d/$name.out"; then
                echo "PASS  $name: rejected: $expect"
            else echo "FAIL  $name: not rejected as expected (rc $rc)"
                 bad=$((bad + 1)); fi
        fi
    }
    local V=(-P 'system.cpu[0].enableVCache=True')
    local D=(-P 'system.cpu[0].enableVAstraDiag=True')
    local B=(-P 'system.cpu[0].enableVAstraBypass=True')
    local R=(-P 'system.cpu[0].enableVAstraScoreboardRelease=True')
    local F=(-P 'system.cpu[0].enableVAstraForwarding=True')
    cfgcase fwd-alone "enableVAstraForwarding requires" --astra "${V[@]}" "${D[@]}" "${F[@]}"
    cfgcase fwd-no-release "enableVAstraForwarding requires" --astra "${V[@]}" \
        "${D[@]}" "${B[@]}" "${F[@]}"
    cfgcase fwd-no-bypass "requires" --astra "${V[@]}" "${D[@]}" "${R[@]}" "${F[@]}"
    cfgcase fwd-lvp-spec "mutually exclusive" --astra --lvp --lvp-spec-addr \
        "${V[@]}" "${D[@]}" "${B[@]}" "${R[@]}" "${F[@]}"
    cfgcase fwd-ok ok --astra "${V[@]}" "${D[@]}" "${B[@]}" "${R[@]}" "${F[@]}"
    cfgcase fwd-with-lvp-shadow ok --astra --lvp "${V[@]}" "${D[@]}" "${B[@]}" \
        "${R[@]}" "${F[@]}"
    # the earlier restrictions still apply
    cfgcase fwd-skip-scan "must not be combined" --astra "${V[@]}" "${D[@]}" \
        "${B[@]}" "${R[@]}" "${F[@]}" -P 'system.cpu[0].vastraTestSkipStoreScan=True'
    cfgcase default-off ok --astra "${V[@]}" "${D[@]}" "${B[@]}" "${R[@]}"
    if grep -q "enableVAstraForwarding=false" "$d/default-off/config.ini"; then
        echo "PASS  enableVAstraForwarding defaults to false in config.ini"
    else
        echo "FAIL  default is not false"; bad=$((bad + 1))
    fi
    # the V$ guards (one CPU) apply to forwarding too
    rc=0
    EXTRA_CPU=timing VP_CONFIG=$ROOT/configs/research/vp_baseline.py "$GEM5" \
        -d "$d/two-cpu" "$ROOT/tests/astra/with_cpus.py" --cmd="$d/fw" --astra \
        "${V[@]}" "${D[@]}" "${B[@]}" "${R[@]}" "${F[@]}" > "$d/two-cpu.out" 2>&1 || rc=$?
    if [ $rc -ne 0 ] && grep -q "need exactly one CPU" "$d/two-cpu.out"; then
        echo "PASS  two-cpu: rejected: need exactly one CPU"
    else
        echo "FAIL  two-cpu: not rejected (rc $rc)"; bad=$((bad + 1))
    fi
    [ $bad -eq 0 ] || { echo "$bad configuration check(s) failed"; return 1; }
}

case ${1:-} in
directed) run_directed ;;
lifecycle) run_lifecycle ;;
config) run_config ;;
all)
    status=0
    for m in config directed lifecycle; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
*) sed -n '3,/^$/{/^$/!p}' "$0" >&2; exit 2 ;;
esac
