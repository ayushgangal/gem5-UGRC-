#!/bin/bash
#
# Targeted tests for the ASTRA shadow predictor, on the research baseline
# configs/research/vp_baseline.py (see README.md, "Targeted tests").
#
#   run_astra_targeted.sh check-asm MODE   build the program of MODE and check
#                                          its disassembly
#   run_astra_targeted.sh MODE             check-asm, run, check the results
#   run_astra_targeted.sh all              every MODE below, in this order
#
# MODE is one of
#   cold           first-use behaviour of the tables
#   alias          two loads that share a LAT entry (and a control run with
#                  tables of twice the size, where they do not)
#   conf           destructive SS aliasing with the pipeline lag removed, and
#                  a control with strides that do not share an SS entry
#   squash         wrong-path loads that are looked up and discarded
#   fault          loads that take a translation fault that is resolved
#   invalid-config ASTRA table sizes 0, 1, 3, 65537 and valid ones, through
#                  the baseline's command line and through the C++ checks
#   lvp-coexist    the LVP phase workload with LVP, and with LVP and ASTRA
#
# OUT (default /tmp/astra-targeted) is where everything is written, in a
# directory for each MODE.  gem5 must already be built.  CFG overrides the
# baseline configuration (it must accept --cmd, --astra and --astra-entries).

set -euo pipefail

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
HERE=$ROOT/tests/astra
OUT=${OUT:-/tmp/astra-targeted}
CC=${CC:-riscv64-linux-gnu-gcc}
OBJDUMP=${OBJDUMP:-riscv64-linux-gnu-objdump}
NM=${NM:-riscv64-linux-gnu-nm}
GEM5=${GEM5:-$ROOT/build/RISCV/gem5.opt}
CFG=${CFG:-$ROOT/configs/research/vp_baseline.py}
WRAP=$HERE/with_params.py
CHECKER=${CHECKER:-$HERE/check_astra_targeted.py}

TRACE=ExecEnable,ExecUser,ExecFaulting,ExecResult,ExecEffAddr,FmtTicksOff
ASTRA_ON=(--astra --astra-entries 1024)

usage()
{
    sed -n '3,/^$/{/^$/!p}' "$0" >&2
    exit 2
}

# build_tg DIR MODE [-D...]: DIR/tg, DIR/tg.S, DIR/tg.nm
# Same link as run_astra.sh: no C library; -n gives one loadable segment;
# --no-relax-gp because SE mode starts with gp = 0
build_tg()
{
    local dir=$1 mode=$2
    shift 2
    mkdir -p "$dir"
    rm -f "$dir/tg" "$dir/tg.S" "$dir/tg.nm" "$dir/pcs.txt"
    "$CC" -O1 -static -nostdlib -ffreestanding -march=rv64g -mabi=lp64d \
        -Wl,-n -Wl,--no-relax-gp -Wl,-e,_start "$@" \
        -o "$dir/tg" "$HERE/astra_targeted.c"
    "$OBJDUMP" -d --no-show-raw-insn "$dir/tg" > "$dir/tg.S"
    "$NM" -S --defined-only "$dir/tg" > "$dir/tg.nm"
    python3 -I "$CHECKER" asm "$mode" "$dir/tg.S" "$dir/tg.nm" "$dir/pcs.txt"
}

# run_gem5 DIR NAME FLAGS BINARY ARGS...: DIR/NAME (gem5's output), DIR/NAME.out
# and DIR/NAME.rc (its exit status).  FLAGS may be empty.  CFGFILE overrides
# the configuration for this run.
run_gem5()
{
    local dir=$1 name=$2 flags=$3 bin=$4
    shift 4
    rm -rf "${dir:?}/$name" "${dir:?}/$name.out" "${dir:?}/$name.rc"
    local rc=0 opts=()
    if [ -n "$flags" ]; then
        opts=(--debug-flags="$flags" --debug-file=trace.txt)
    fi
    "$GEM5" -d "$dir/$name" "${opts[@]}" "${CFGFILE:-$CFG}" --cmd="$bin" \
        "$@" > "$dir/$name.out" 2>&1 || rc=$?
    echo "$rc" > "$dir/$name.rc"
    echo "$(basename "$dir") $name: exit status $rc"
}

run_pair()
{
    local dir=$1 bin=$2 tag=${3:-}
    run_gem5 "$dir" "${tag}D" "$TRACE" "$bin"
    run_gem5 "$dir" "${tag}S" "$TRACE,ASTRA" "$bin" "${ASTRA_ON[@]}"
}

run_cold()
{
    local d=$OUT/cold
    rm -rf "${d:?}"
    build_tg "$d" cold -DTG_COLD
    run_pair "$d" "$d/tg"
    python3 -I "$CHECKER" cold "$d"
}

run_alias()
{
    local d=$OUT/alias
    rm -rf "${d:?}"
    build_tg "$d" alias -DTG_ALIAS
    run_pair "$d" "$d/tg"
    run_gem5 "$d" S2K "$TRACE,ASTRA" "$d/tg" --astra --astra-entries 2048
    python3 -I "$CHECKER" alias "$d"
}

run_conf()
{
    local d=$OUT/conf
    rm -rf "${d:?}"
    mkdir -p "$d"
    build_tg "$d/bin_alias" conf -DTG_CONF -DYSTRIDE=1088
    build_tg "$d/bin_ctrl" conf -DTG_CONF -DYSTRIDE=128
    run_pair "$d" "$d/bin_alias/tg" alias
    run_pair "$d" "$d/bin_ctrl/tg" ctrl
    python3 -I "$CHECKER" conf "$d"
}

run_squash()
{
    local d=$OUT/squash
    rm -rf "${d:?}"
    build_tg "$d" squash -DTG_SQUASH
    run_pair "$d" "$d/tg"
    python3 -I "$CHECKER" squash "$d"
}

run_fault()
{
    local d=$OUT/fault
    rm -rf "${d:?}"
    build_tg "$d" fault -DTG_FAULT
    run_pair "$d" "$d/tg"
    python3 -I "$CHECKER" fault "$d"
}

# Each case is a gem5 run of the cold program; the checker knows the cases
run_invalid_config()
{
    local d=$OUT/invalid-config
    rm -rf "${d:?}"
    build_tg "$d" cold -DTG_COLD
    local bin=$d/tg n
    # the baseline's command line: --astra-entries is validated by its parser
    for n in 0 1 3 65537; do
        run_gem5 "$d" "cli_$n" "" "$bin" --astra --astra-entries "$n"
    done
    run_gem5 "$d" cli_disabled_3 "" "$bin" --astra-entries 3
    run_gem5 "$d" cli_disabled_2 "" "$bin" --astra-entries 2
    for n in 2 1024 65536; do
        run_gem5 "$d" "cli_ok_$n" "" "$bin" --astra --astra-entries "$n"
    done
    run_gem5 "$d" cli_default "" "$bin" --astra
    # the C++ check in the AstraPredictor constructor, reached by setting the
    # parameters directly
    local cpu='system.cpu[0]'
    for n in 0 1 3 65537; do
        CFGFILE=$WRAP run_gem5 "$d" "cpp_$n" "" "$bin" \
            -P "$cpu.enableAstra=True" -P "$cpu.astraTableEntries=$n"
    done
    CFGFILE=$WRAP run_gem5 "$d" cpp_disabled_3 "" "$bin" \
        -P "$cpu.astraTableEntries=3"
    CFGFILE=$WRAP run_gem5 "$d" cpp_ok_2 "" "$bin" \
        -P "$cpu.enableAstra=True" -P "$cpu.astraTableEntries=2"
    python3 -I "$CHECKER" invalid-config "$d"
}

# The LVP phase workload (built and checked by the LVP runner, into
# OUT/lvp-coexist/lvpbuild) with LVP address speculation (L), with LVP and
# ASTRA (LA) and with LVP and ASTRA and the ASTRA log (LAL)
run_lvp_coexist()
{
    local d=$OUT/lvp-coexist
    rm -rf "${d:?}"
    mkdir -p "$d"
    OUT=$d/lvpbuild "$ROOT/tests/lvp/run_lvp_stageb.sh" check-asm phase \
        > "$d/lvpbuild.log" 2>&1
    local bin=$d/lvpbuild/phase/ldld pcs=$d/lvpbuild/phase/load_pcs.txt
    local p c c4
    p=$(awk '$1 == "producer" { print $2 }' "$pcs")
    c=$(awk '$1 == "consumer" { print $2 }' "$pcs")
    if [ "$(echo "$p" | wc -w)" -ne 1 ] || [ "$(echo "$c" | wc -w)" -ne 1 ]
    then
        echo "load_pcs.txt: expected one producer and one consumer" >&2
        exit 1
    fi
    c4=$(printf '0x%x' $((c + 4)))
    cp "$pcs" "$d/load_pcs.txt"
    local lvp=(--lvp --lvp-spec-addr --lvp-window "$p" "$c4")
    run_gem5 "$d" L "$TRACE" "$bin" "${lvp[@]}"
    run_gem5 "$d" LA "$TRACE" "$bin" "${lvp[@]}" "${ASTRA_ON[@]}"
    run_gem5 "$d" LAL "$TRACE,ASTRA" "$bin" "${lvp[@]}" "${ASTRA_ON[@]}"
    python3 -I "$CHECKER" lvp-coexist "$d"
}

run_mode()
{
    case $1 in
    cold) run_cold ;;
    alias) run_alias ;;
    conf) run_conf ;;
    squash) run_squash ;;
    fault) run_fault ;;
    invalid-config) run_invalid_config ;;
    lvp-coexist) run_lvp_coexist ;;
    *) usage ;;
    esac
}

case ${1:-} in
check-asm)
    [ $# -eq 2 ] || usage
    case $2 in
    cold) build_tg "$OUT/cold" cold -DTG_COLD ;;
    alias) build_tg "$OUT/alias" alias -DTG_ALIAS ;;
    conf) build_tg "$OUT/conf/bin_alias" conf -DTG_CONF -DYSTRIDE=1088 ;;
    squash) build_tg "$OUT/squash" squash -DTG_SQUASH ;;
    fault) build_tg "$OUT/fault" fault -DTG_FAULT ;;
    *) usage ;;
    esac
    ;;
all)
    [ $# -eq 1 ] || usage
    status=0
    for m in cold alias conf squash fault invalid-config lvp-coexist; do
        echo "=== $m"
        bash "$0" "$m" || status=1
    done
    exit $status
    ;;
cold | alias | conf | squash | fault | invalid-config | lvp-coexist)
    [ $# -eq 1 ] || usage
    run_mode "$1"
    ;;
*)
    usage
    ;;
esac
