"""Research baseline: a paper-aligned RISC-V MinorCPU system for the value
prediction experiments (LVP, ASTRA, ...).

Usage (from the gem5 repository root, SE mode, static RISC-V binary):

    build/RISCV/gem5.opt -d OUTDIR configs/research/vp_baseline.py \
        --cmd=BINARY [predictor options]

Migrated from ~/gem5-work/vp_baseline.py (Sep 11 18:43).  Every processor,
cache, prefetcher, memory, branch predictor, clock and pipeline setting is
the original one.  The changes are: the configs import path is derived from
this file's location, the predictor options below, and two comments (the
load issue/predict limit, and the inherited cache timings).

All predictors are DISABLED unless asked for.  The predictor options set only
the predictor SimObject parameters of the CPU, never a baseline setting, so
that runs with and without a predictor differ in nothing else:

    --lvp                      enableLVP
    --lvp-spec-addr            lvpSpecAddr (needs --lvp)
    --lvp-threshold N          lvpConfThreshold, 1..3 (needs --lvp)
    --lvp-entries N            lvpTableEntries, a power of two (needs --lvp)
    --lvp-window BEGIN END     lvpSpecPcBegin/End, half-open [BEGIN, END),
                               BEGIN < END (needs --lvp-spec-addr)
    --astra                    enableAstra (shadow mode)
    --astra-entries N          astraTableEntries, a power of two in
                               [2, 65536] (needs --astra)

The ASTRA parameters exist only in a gem5 binary built from the working tree
that contains the (still uncommitted) ASTRA changes.
"""

import argparse
import os

import m5


def int_arg(text):
    """An integer, decimal or 0x hexadecimal"""
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer: %r" % text)


def power_of_two(text):
    value = int_arg(text)
    if value < 1 or value & (value - 1):
        raise argparse.ArgumentTypeError("not a power of two: %r" % text)
    return value


parser = argparse.ArgumentParser()
parser.add_argument(
    "--cmd", required=True, help="Path to the RISC-V executable"
)

predictors = parser.add_argument_group(
    "predictor options (all predictors are disabled by default)"
)
predictors.add_argument(
    "--lvp", action="store_true", help="Enable load value prediction"
)
predictors.add_argument(
    "--lvp-spec-addr",
    action="store_true",
    help="Let dependent loads use LVP predictions (needs --lvp)",
)
predictors.add_argument(
    "--lvp-threshold",
    type=int,
    choices=[1, 2, 3],
    help="LVP confidence threshold (needs --lvp; gem5 default 3)",
)
predictors.add_argument(
    "--lvp-entries",
    type=power_of_two,
    help="LVP table entries, a power of two (needs --lvp; gem5 default 1024)",
)
predictors.add_argument(
    "--lvp-window",
    type=int_arg,
    nargs=2,
    metavar=("BEGIN", "END"),
    help="Only speculate in the PC range [BEGIN, END) (needs --lvp-spec-addr)",
)
predictors.add_argument(
    "--astra",
    action="store_true",
    help="Enable the ASTRA shadow predictor (needs a build with ASTRA)",
)
predictors.add_argument(
    "--astra-entries",
    type=power_of_two,
    help="ASTRA table entries, a power of two in [2, 65536] (needs --astra;"
    " gem5 default 1024)",
)
args = parser.parse_args()

# Options that only mean something with another one are errors, not ignored
if args.lvp_spec_addr and not args.lvp:
    parser.error("--lvp-spec-addr needs --lvp")
if (args.lvp_threshold is not None or args.lvp_entries is not None) and (
    not args.lvp
):
    parser.error("--lvp-threshold and --lvp-entries need --lvp")
if args.lvp_window is not None:
    if not args.lvp_spec_addr:
        parser.error("--lvp-window needs --lvp-spec-addr")
    window_begin, window_end = args.lvp_window
    if window_begin < 0 or window_end <= window_begin:
        parser.error(
            "--lvp-window needs 0 <= BEGIN < END (got %#x %#x)"
            % (window_begin, window_end)
        )
if args.astra_entries is not None:
    if not args.astra:
        parser.error("--astra-entries needs --astra")
    if not 2 <= args.astra_entries <= 65536:
        parser.error("--astra-entries must be in [2, 65536]")

# Allow imports from the configs directory of this gem5 tree: this file is
# configs/research/vp_baseline.py
m5.util.addToPath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from m5.objects import (
    System,
    SrcClockDomain,
    VoltageDomain,
    AddrRange,
    RiscvMinorCPU,
    TAGE,
    L2XBar,
    SystemXBar,
    MemCtrl,
    DDR4_2400_4x16,
    StridePrefetcher,
    AMPMPrefetcher,
    AccessMapPatternMatching,
    Process,
    SEWorkload,
    Root,
)

from common.Caches import L1_ICache, L1_DCache, L2Cache


# ============================================================
# SYSTEM
# ============================================================

system = System()

system.clk_domain = SrcClockDomain(
    clock="1GHz",
    voltage_domain=VoltageDomain(),
)

system.mem_mode = "timing"

# DDR4_2400_4x16 provides 4 GiB in this configuration.
system.mem_ranges = [AddrRange("4GB")]

# Paper: 64-byte cache lines
system.cache_line_size = 64


# ============================================================
# CPU
# ============================================================

system.cpu = RiscvMinorCPU()

# Disable cycle-idling because the ActivityRecorder path
# currently triggers an assertion in this gem5 build.
system.cpu.enableIdling = False

# ------------------------------------------------------------
# Pipeline width
# Paper: Fetch through Commit width = 4
# ------------------------------------------------------------

system.cpu.decodeInputWidth = 4
system.cpu.executeInputWidth = 4

system.cpu.executeIssueLimit = 4
system.cpu.executeCommitLimit = 4

# ------------------------------------------------------------
# Memory issue limits
# Paper (Table I): the pipeline issues and predicts at most two loads per
# cycle, and the LSU can handle a single load per cycle.  This baseline keeps
# the MinorCPU memory issue/commit limits at 1, which matches the single-load
# LSU; the paper's two-loads-per-cycle issue/predict capability is not
# modelled here.
# ------------------------------------------------------------

system.cpu.executeMemoryIssueLimit = 1
system.cpu.executeMemoryCommitLimit = 1

# Allow speculative/early memory issue
system.cpu.executeAllowEarlyMemoryIssue = True

# ------------------------------------------------------------
# Demand-load concurrency (part of this MinorCPU approximation)
#
# These three settings are NOT a claim about the paper's model, which is a
# dedicated CVA6-based CPU and has no MinorCPU thread policy.  They exist
# only so that MinorCPU can keep up to 16 demand loads outstanding, to match
# the paper's 16 L1D MSHRs:
#
# - executeMaxAccessesInMemory and executeLSQTransfersQueueSize (default 2
#   each) both cap the loads in the memory system; raising both to 16 gives
#   enough queue capacity for 16 outstanding demand loads (raising only one
#   has no effect).
# - threadPolicy "SingleThreaded" avoids the RoundRobin commit gating: with
#   RoundRobin, Execute::getCommittingThread() does not call commit() while
#   the oldest instruction is a load waiting in the LSQ, so no younger load
#   can be issued early behind it.  This is a one-thread CPU, so the policy
#   has no other purpose here.
#
# They do not make MinorCPU's issue pipeline, LSU behaviour, scoreboard or
# memory timing identical to the paper's CVA6 model.  The memory issue and
# commit limits above stay at 1.
# ------------------------------------------------------------

system.cpu.threadPolicy = "SingleThreaded"
system.cpu.executeMaxAccessesInMemory = 16
system.cpu.executeLSQTransfersQueueSize = 16


# ============================================================
# VALUE PREDICTORS (disabled unless requested on the command line)
#
# Only the predictor parameters of the CPU are set here.
# ============================================================


def set_cpu_param(name, value):
    try:
        setattr(system.cpu, name, value)
    except AttributeError:
        raise SystemExit(
            "error: this gem5 binary has no CPU parameter %s (it was built "
            "without the change that adds it)" % name
        )


if args.lvp:
    set_cpu_param("enableLVP", True)
    if args.lvp_spec_addr:
        set_cpu_param("lvpSpecAddr", True)
    if args.lvp_threshold is not None:
        set_cpu_param("lvpConfThreshold", args.lvp_threshold)
    if args.lvp_entries is not None:
        set_cpu_param("lvpTableEntries", args.lvp_entries)
    if args.lvp_window is not None:
        set_cpu_param("lvpSpecPcBegin", args.lvp_window[0])
        set_cpu_param("lvpSpecPcEnd", args.lvp_window[1])

if args.astra:
    set_cpu_param("enableAstra", True)
    if args.astra_entries is not None:
        set_cpu_param("astraTableEntries", args.astra_entries)


# ============================================================
# BRANCH PREDICTOR
# ============================================================

system.cpu.branchPred = TAGE()

# Paper:
#   BTB = 4096 entries
#   RAS = 16 entries
system.cpu.branchPred.BTBEntries = 4096
system.cpu.branchPred.RASSize = 16

# ------------------------------------------------------------
# TAGE size
#
# gem5 default:
#   logTagTableSizes = [13, 9, 9, 9, 9, 9, 9, 9]
#
# Increasing each table-size exponent by 2 gives approximately
# 32 KiB of logical TAGE table storage.
# ------------------------------------------------------------

system.cpu.branchPred.tage.logTagTableSizes = [
    15,
    11,
    11,
    11,
    11,
    11,
    11,
    11,
]


# ============================================================
# CACHE TIMING NOTE
#
# The tag/data/response latencies of the L1I, L1D and L2 below are NOT set
# here: they are inherited gem5 defaults (common/Caches.py: L1 2/2/2, L2
# 20/20/20).  They have not been calibrated against the paper's effective
# load-to-use latencies (Table I: L1I 1 cycle, L1D 4 cycles, L2 24 cycles).
# Do not assume timing equivalence with the paper until they are.
# ============================================================


# ============================================================
# L1 INSTRUCTION CACHE
# ============================================================

# Paper: L1I = 16 MSHRs
icache = L1_ICache(
    size="32kB",
    assoc=4,
    mshrs=16,
)


# ============================================================
# L1 DATA CACHE
# ============================================================

dcache = L1_DCache(
    size="64kB",
    assoc=4,
)

# Paper: 16 MSHRs
dcache.mshrs = 16

# ------------------------------------------------------------
# Stride prefetcher
# Paper:
#   64-entry table
#   degree = 4
# ------------------------------------------------------------

dcache.prefetcher = StridePrefetcher(
    degree=4,
    table_entries="64",
)


# ============================================================
# L2 CACHE
# ============================================================

system.l2 = L2Cache(
    size="256kB",
    assoc=8,
)

# Paper: 20 MSHRs
system.l2.mshrs = 20

# ------------------------------------------------------------
# AMPM prefetcher
# Paper: 256-entry access-map table
# ------------------------------------------------------------

system.l2.prefetcher = AMPMPrefetcher(
    ampm=AccessMapPatternMatching(
        access_map_table_entries="256",
    )
)


# ============================================================
# CACHE HIERARCHY / BUSES
# ============================================================

system.membus = SystemXBar()

system.tol2bus = L2XBar(
    clk_domain=system.clk_domain,
)

# L2 <-> buses
system.l2.cpu_side = system.tol2bus.mem_side_ports
system.l2.mem_side = system.membus.cpu_side_ports

# Attach private split L1 caches to MinorCPU
system.cpu.addPrivateSplitL1Caches(
    icache,
    dcache,
)

# Connect CPU to the L2 and memory hierarchy
system.cpu.connectAllPorts(
    system.tol2bus.cpu_side_ports,
    system.membus.cpu_side_ports,
    system.membus.mem_side_ports,
)


# ============================================================
# MEMORY
# ============================================================

system.mem_ctrl = MemCtrl()

# Paper-aligned DDR4-2400 x64 configuration
system.mem_ctrl.dram = DDR4_2400_4x16()

# Connect the complete memory range
system.mem_ctrl.dram.range = system.mem_ranges[0]

# Connect memory controller to the memory bus
system.mem_ctrl.port = system.membus.mem_side_ports


# ============================================================
# INTERRUPT CONTROLLER
# ============================================================

system.cpu.createInterruptController()


# ============================================================
# WORKLOAD
# ============================================================

binary = args.cmd

process = Process()
process.cmd = [binary]

system.workload = SEWorkload.init_compatible(binary)

system.cpu.workload = process
system.cpu.createThreads()


# ============================================================
# ROOT / SIMULATION
# ============================================================

root = Root(
    full_system=False,
    system=system,
)

m5.instantiate()

window_text = ""
if args.lvp_window is not None:
    window_text = " window %#x-%#x" % tuple(args.lvp_window)
print(
    "Predictors: LVP %s%s%s, ASTRA %s"
    % (
        "on" if args.lvp else "off",
        " (speculating)" if args.lvp_spec_addr else "",
        window_text,
        "on" if args.astra else "off",
    )
)
print("Beginning simulation!")

exit_event = m5.simulate()

print(
    "Exiting @ tick",
    m5.curTick(),
    "because",
    exit_event.getCause(),
)
