#!/bin/bash
#
# Tests of the V-ASTRA bypass (enableVAstraBypass, src/cpu/minor/execute.cc
# vastraTryBypass), on the research baseline configs/research/vp_baseline.py
# (see README.md, "V-ASTRA bypass tests").
#
#   run_bypass.sh MODE    build, run, check
#   run_bypass.sh all     every MODE below, in this order
#
# MODE is one of
#   config     invalid combinations are rejected, valid ones are accepted;
#              the V$ guards: a second CPU (either type) or a second
#              hardware thread is refused, other configurations are not
#   classify   the programs of run_vastra.sh (classify, store, discard,
#   store      fault) with bypass off (P) and on (B): same output, same
#   discard    committed trace, rejected candidates still go to memory,
#   fault      bypassed loads never reach memory
#   syscall    a read system call overwrites bytes the V$ holds
#   atomic     an older amoadd in flight when the candidate load is decided
#   abandon    the V$ entry of a candidate is replaced before its send
#   order      an older store with late data: address known before a younger
#              candidate is decided?
#   sub        lb lbu lh lhu lw lwu candidates
#   split      loads that cross a 64 byte line (split LSQ requests)
#   fp         fld and flw candidates, before and after fsd/fsw stores
#   synonym    can two virtual addresses name the same memory in SE mode?
#   special    load-reserved and atomic operations are never bypassed
#   syscall-negative  the syscall program with the flush suppressed
#                     (vastraTestSkipSyscallFlush): the stale bytes must be
#                     consumed (bypass) or detected (strict verification)
#   latency    a pointer chase that misses to DRAM, with and without bypass
#   lvp        the LVP phase workload with LVP, ASTRA, V$ and bypass
#   drain      classify and syscall, drained and statistics-reset in chunks
#
# OUT (default /tmp/bypass-test) is where everything is written.  gem5 must
# already be built.  P and B both have ASTRA, the V$ and the V-ASTRA probe
# on; only enableVAstraBypass differs.

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/astra
OUT=${OUT:-/tmp/bypass-test}
CC=${CC:-riscv64-linux-gnu-gcc}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CHECKER=${CHECKER:-$HERE/check_bypass.py}

EXEC=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
BASE=(--astra -P 'system.cpu[0].enableVCache=True'
      -P 'system.cpu[0].enableVAstraDiag=True')
BYP=(-P 'system.cpu[0].enableVAstraBypass=True')

# run_gem5 DIR NAME FLAGS CFG BINARY ARGS...
run_gem5()
{
    local dir=$1 name=$2 flags=$3 cfg=$4 bin=$5
    shift 5
    rm -rf "${dir:?}/$name" "${dir:?}/$name.out" "${dir:?}/$name.rc"
    local rc=0
    local fl=()
    [ -n "$flags" ] && fl=(--debug-flags="$flags" --debug-file=trace.txt)
    "$GEM5" -d "$dir/$name" "${fl[@]}" "$cfg" --cmd="$bin" "$@" \
        > "$dir/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$dir/$name.rc"
    echo "$(basename "$dir") $name: exit status $rc"
}

# run_pair DIR BINARY CFG ARGS...: P without bypass, B with it
run_pair()
{
    local dir=$1 bin=$2 cfg=$3
    shift 3
    run_gem5 "$dir" P "$EXEC,VBypass" "$cfg" "$bin" "${BASE[@]}" "$@"
    run_gem5 "$dir" B "$EXEC,VBypass,VAstra,MinorMem" "$cfg" "$bin" \
        "${BASE[@]}" "$@" "${BYP[@]}"
}

build_bp()
{
    local dir=$1 mode=$2
    mkdir -p "$dir"
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start -DBP_"$mode" \
        -o "$dir/bp" "$HERE/bypass.c" 2> /dev/null
}

# The programs of run_vastra.sh: its check-asm builds one and finds its PCs
build_va()
{
    local dir=$1 mode=$2
    mkdir -p "$dir"
    OUT=$dir/build bash "$HERE/run_vastra.sh" check-asm "$mode" \
        > "$dir/build.log" 2>&1
    cp "$dir/build/$mode/va" "$dir/va"
    cp "$dir/build/$mode/pcs.txt" "$dir/pcs.txt"
}

run_vastra_mode()
{
    local mode=$1 d=$OUT/$1
    rm -rf "${d:?}"
    build_va "$d" "$mode"
    run_pair "$d" "$d/va" "$HERE/with_params.py"
    python3 -I "$CHECKER" "$mode" "$d"
}

run_syscall()
{
    local d=$OUT/syscall
    rm -rf "${d:?}"
    build_bp "$d" SYSCALL
    run_pair "$d" "$d/bp" "$HERE/with_params.py"
    python3 -I "$CHECKER" syscall "$d"
}

# P bypass off, B bypass on, N bypass on with the flush suppressed (stale
# bytes are consumed: must be seen), X strict verification with the flush
# suppressed (the stale candidate must be detected: the run must stop)
run_syscall_negative()
{
    local d=$OUT/syscall-negative
    rm -rf "${d:?}"
    build_bp "$d" SYSCALL
    local skip=(-P 'system.cpu[0].vastraTestSkipSyscallFlush=True')
    run_gem5 "$d" P "$EXEC,VBypass" "$HERE/with_params.py" "$d/bp" "${BASE[@]}"
    run_gem5 "$d" B "$EXEC,VBypass" "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}" "${BYP[@]}"
    run_gem5 "$d" N "$EXEC,VBypass" "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}" "${BYP[@]}" "${skip[@]}"
    (ulimit -c 0; run_gem5 "$d" X "$EXEC,VBypass,VAstra" \
        "$HERE/with_params.py" "$d/bp" "${BASE[@]}" \
        -P 'system.cpu[0].enableVAstraVerify=True' "${skip[@]}")
    python3 -I "$CHECKER" syscall-negative "$d"
}

# build_bp_pcs DIR MODE SYMBOL...: DIR/bp and DIR/pcs.txt with the symbols' PCs
build_bp_pcs()
{
    local dir=$1 mode=$2
    shift 2
    build_bp "$dir" "$mode"
    : > "$dir/pcs.txt"
    for sym in "$@"; do
        riscv64-linux-gnu-nm "$dir/bp" | awk -v s="$sym" '$3 == s { print s, "0x" $1 }' \
            >> "$dir/pcs.txt"
    done
}

run_simple()    # MODE BPMODE SYMBOL...
{
    local mode=$1 bpmode=$2 d=$OUT/$1
    shift 2
    rm -rf "${d:?}"
    build_bp_pcs "$d" "$bpmode" "$@"
    run_pair "$d" "$d/bp" "$HERE/with_params.py"
    python3 -I "$CHECKER" "$mode" "$d"
}

# The synonym test: a file mapped twice MAP_SHARED (the program must run in a
# directory of its own: it creates syn.dat), and the two other ways to share
# memory, which the SE syscall table lists without an implementation
run_synonym()
{
    local d=$OUT/synonym
    rm -rf "${d:?}"
    mkdir -p "$d/syn" "$d/shm" "$d/memfd"
    build_bp "$d/syn" SYN
    ( cd "$d/syn" && run_pair "$d/syn" "$d/syn/bp" "$HERE/with_params.py" )
    for m in shm:SHM memfd:MEMFD; do
        build_bp "$d/${m%%:*}" "${m##*:}"
        ( ulimit -c 0; cd "$d/${m%%:*}" && run_gem5 "$d/${m%%:*}" B \
            "$EXEC,VBypass" "$HERE/with_params.py" "$d/${m%%:*}/bp" \
            "${BASE[@]}" "${BYP[@]}" )
    done
    python3 -I "$CHECKER" synonym "$d"
}

run_special()
{
    local d=$OUT/special
    rm -rf "${d:?}"
    build_bp "$d" SPECIAL
    riscv64-linux-gnu-nm "$d/bp" | awk '$3 == "bp_lr" || $3 == "bp_amo" ||
        $3 == "bp_probe0" { print $3, "0x" $1 }' > "$d/pcs.txt"
    run_pair "$d" "$d/bp" "$HERE/with_params.py"
    python3 -I "$CHECKER" special "$d"
}

run_latency()
{
    local d=$OUT/latency
    rm -rf "${d:?}"
    build_bp "$d" LAT
    run_gem5 "$d" P "$EXEC,VBypass" "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}"
    run_gem5 "$d" B "$EXEC,VBypass" "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}" "${BYP[@]}"
    # Timed traces (ticks on) for the per-walk spans
    run_gem5 "$d" PT ExecEnable,ExecUser "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}"
    run_gem5 "$d" BT ExecEnable,ExecUser "$HERE/with_params.py" "$d/bp" \
        "${BASE[@]}" "${BYP[@]}"
    riscv64-linux-gnu-nm "$d/bp" | awk '$3 == "bp_step" { print $3, "0x" $1 }' \
        > "$d/pcs.txt"
    python3 -I "$CHECKER" latency "$d"
}

run_lvp()
{
    local d=$OUT/lvp
    rm -rf "${d:?}"
    mkdir -p "$d"
    OUT=$d/lvp_build "$ROOT/tests/lvp/run_lvp_stageb.sh" check-asm phase \
        > "$d/lvp_build.log" 2>&1
    local pcs=$d/lvp_build/phase/load_pcs.txt p c c4
    p=$(awk '$1 == "producer" { print $2 }' "$pcs")
    c=$(awk '$1 == "consumer" { print $2 }' "$pcs")
    c4=$(printf '0x%x' $((c + 4)))
    run_pair "$d" "$d/lvp_build/phase/ldld" "$HERE/with_params.py" \
        --lvp --lvp-spec-addr --lvp-window "$p" "$c4"
    python3 -I "$CHECKER" lvp "$d"
}

declare -A CHUNK_OF=([classify]=7321000 [syscall]=7321000 [latency]=61357000)

run_drain()
{
    local d=$OUT/drain
    rm -rf "${d:?}"
    mkdir -p "$d"
    build_va "$d/classify" classify
    build_bp "$d/syscall" SYSCALL
    build_bp "$d/latency" LAT
    # drain: the system is drained at every boundary (no load in flight);
    # reset: statistics are reset with loads in flight (carry-in)
    for mode in drain reset; do
        for w in classify syscall latency; do
            local bin=$d/$w/va
            [ $w != classify ] && bin=$d/$w/bp
            local wd=$d/$mode-$w
            mkdir -p "$wd"
            export DRAIN_MODE=$mode DRAIN_CHUNK=${CHUNK_OF[$w]}
            if [ $w = latency ]; then   # no MinorMem trace: too long
                run_gem5 "$wd" P "$EXEC,VBypass" "$HERE/drain_reset.py" \
                    "$bin" "${BASE[@]}"
                run_gem5 "$wd" B "$EXEC,VBypass" "$HERE/drain_reset.py" \
                    "$bin" "${BASE[@]}" "${BYP[@]}"
            else
                run_pair "$wd" "$bin" "$HERE/drain_reset.py"
            fi
            unset DRAIN_MODE DRAIN_CHUNK
            python3 -I "$CHECKER" drain "$wd" "$mode"
        done
    done
    # dense loads (the LVP phase workload with ASTRA): small windows, so that
    # candidates are in flight when the statistics are reset
    mkdir -p "$d/lvpw"
    OUT=$d/lvpw/build "$ROOT/tests/lvp/run_lvp_stageb.sh" check-asm phase \
        > "$d/lvpw/build.log" 2>&1
    for mode in drain reset; do
        local wd=$d/$mode-lvp
        mkdir -p "$wd"
        export DRAIN_MODE=$mode DRAIN_CHUNK=337000
        run_gem5 "$wd" P "$EXEC,VBypass" "$HERE/drain_reset.py" \
            "$d/lvpw/build/phase/ldld" "${BASE[@]}"
        run_gem5 "$wd" B "$EXEC,VBypass" "$HERE/drain_reset.py" \
            "$d/lvpw/build/phase/ldld" "${BASE[@]}" "${BYP[@]}"
        unset DRAIN_MODE DRAIN_CHUNK
        python3 -I "$CHECKER" drain "$wd" "$mode"
    done
}

# Invalid and valid combinations: gem5 must stop before "Beginning
# simulation!" with the message, or run
run_config()
{
    local d=$OUT/config n=0 bad=0
    rm -rf "${d:?}"
    mkdir -p "$d"
    build_bp "$d" SYSCALL
    cfgcase()    # NAME EXPECT(ok|MESSAGE) -P...
    {
        local name=$1 expect=$2
        shift 2
        local rc=0
        "$GEM5" -d "$d/$name" "${CFGSCRIPT:-$HERE/with_params.py}" \
            --cmd="$d/bp" "$@" \
            > "$d/$name.out" 2>&1 || rc=$?
        n=$((n + 1))
        if [ "$expect" = ok ]; then
            if [ $rc -eq 0 ] && grep -q "Beginning simulation" "$d/$name.out"
            then echo "PASS  $name: accepted"
            else echo "FAIL  $name: rejected (rc $rc)"; bad=$((bad + 1)); fi
        else
            if [ $rc -ne 0 ] && ! grep -q "Beginning simulation" \
                "$d/$name.out" && grep -q "$expect" "$d/$name.out"
            then echo "PASS  $name: rejected: $expect"
            else echo "FAIL  $name: not rejected as expected (rc $rc)"
                 bad=$((bad + 1)); fi
        fi
    }
    local V=(-P 'system.cpu[0].enableVCache=True')
    local D=(-P 'system.cpu[0].enableVAstraDiag=True')
    local B=(-P 'system.cpu[0].enableVAstraBypass=True')
    cfgcase no-astra "enableVAstraBypass requires" "${V[@]}" "${D[@]}" "${B[@]}"
    cfgcase no-vcache "enableVAstraDiag requires\|enableVAstraBypass requires" \
        --astra "${D[@]}" "${B[@]}"
    cfgcase no-diag "enableVAstraBypass requires" --astra "${V[@]}" "${B[@]}"
    cfgcase verify "mutually exclusive" --astra "${V[@]}" "${D[@]}" "${B[@]}" \
        -P 'system.cpu[0].enableVAstraVerify=True'
    cfgcase skipscan "must not be combined" --astra "${V[@]}" "${D[@]}" \
        "${B[@]}" -P 'system.cpu[0].vastraTestSkipStoreScan=True'
    local S=(-P 'system.cpu[0].vastraTestSkipSyscallFlush=True')
    cfgcase skipflush-alone "vastraTestSkipSyscallFlush is test-only" \
        --astra "${V[@]}" "${D[@]}" "${S[@]}"
    cfgcase skipflush-nodiag "enableVAstraBypass requires" \
        --astra "${V[@]}" "${B[@]}" "${S[@]}"
    cfgcase skipflush-bypass ok --astra "${V[@]}" "${D[@]}" "${B[@]}" "${S[@]}"
    # The system guard (MinorCPU::init) and the thread guard (Execute): a
    # second CPU of either type, a second hardware thread; every V$ user is
    # refused, an ordinary configuration with a second CPU is not
    local C=(-P 'system.cpu[0].enableVCache=True')
    export CFGSCRIPT=$HERE/with_cpus.py
    EXTRA_CPU=minor cfgcase two-minor-bypass "need exactly one CPU" --astra \
        "${V[@]}" "${D[@]}" "${B[@]}"
    EXTRA_CPU=timing cfgcase two-timing-bypass "need exactly one CPU" \
        --astra "${V[@]}" "${D[@]}" "${B[@]}"
    EXTRA_CPU=timing cfgcase two-timing-vcache "need exactly one CPU" \
        --astra "${C[@]}"
    EXTRA_CPU=timing cfgcase two-timing-diag "need exactly one CPU" \
        --astra "${V[@]}" "${D[@]}"
    EXTRA_CPU=timing cfgcase two-timing-no-vcache ok --astra
    export CFGSCRIPT=$HERE/with_smt.py
    cfgcase smt-vcache "requires a single thread" --astra "${C[@]}"
    unset CFGSCRIPT
    cfgcase valid ok --astra "${V[@]}" "${D[@]}" "${B[@]}"
    cfgcase verify-alone ok --astra "${V[@]}" "${D[@]}" \
        -P 'system.cpu[0].enableVAstraVerify=True'
    cfgcase default-off ok --astra "${V[@]}" "${D[@]}"
    grep -q "vastraTestSkipSyscallFlush=false" "$d/default-off/config.ini" &&
        echo "PASS  default: vastraTestSkipSyscallFlush=false in config.ini" ||
        { echo "FAIL  default: skip flush is not false"; bad=$((bad+1)); }
    grep -q "enableVAstraBypass=false" "$d/default-off/config.ini" &&
        echo "PASS  default: enableVAstraBypass=false in config.ini" ||
        { echo "FAIL  default: enableVAstraBypass is not false"; bad=$((bad+1)); }
    [ $bad -eq 0 ] || { echo "$bad configuration check(s) failed"; return 1; }
}

case ${1:-} in
all)
    status=0
    for m in config classify store discard fault special atomic abandon order sub split fp synonym syscall syscall-negative latency lvp drain; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
config) run_config ;;
classify|store|discard|fault) run_vastra_mode "$1" ;;
syscall) run_syscall ;;
syscall-negative) run_syscall_negative ;;
special) run_special ;;
atomic) run_simple atomic ATOMIC bp_acl bp_acc ;;
abandon) run_simple abandon ABANDON bp_abc ;;
order) run_simple order ORDER bp_ordst bp_ordc ;;
sub) run_simple sub SUB bp_lb bp_lbu bp_lh bp_lhu bp_lw bp_lwu ;;
split) run_simple split SPLIT bp_split ;;
fp) run_simple fp FP bp_fldp bp_flwp ;;
synonym) run_synonym ;;
latency) run_latency ;;
lvp) run_lvp ;;
drain) run_drain ;;
*) sed -n '3,/^$/{/^$/!p}' "$0" >&2; exit 2 ;;
esac
