"""Stall-on-use variant of the research baseline (configs/research/vp_baseline.py).

Usage (from the gem5 repository root, SE mode, static RISC-V binary), with the
same options as the baseline:

    build/RISCV/gem5.opt -d OUTDIR configs/research/vp_stall_on_use.py \
        --cmd=BINARY [predictor options]

This runs vp_baseline.py UNCHANGED (it is executed by runpy, so every option,
setting and SimObject of the baseline is created exactly as there) and makes one
change just before m5.instantiate(): the memory functional unit of the CPU's
functional-unit pool has extraAssumedLat = 0 instead of the Minor default of 2.

Why this is the only change needed (src/cpu/minor/execute.cc, Execute::issue):
the destination registers of a memory reference are marked
  numUnpredictableResults++            only if extraAssumedLat == 0
  returnCycle = now + opLat + extraAssumedLat
and Scoreboard::canInstIssue (scoreboard.cc) refuses to issue an instruction
whose source register has numUnpredictableResults != 0.  The mark is cleared
only when the load leaves the in-flight queue (Execute::commit,
scoreboard.clearInstDests(inst, inst->isMemRef())), that is when the load
commits.  With the default of 2 a load's dependents issue three cycles after
the load whatever its real latency (they are then held at the end of their
functional unit until the load commits); with 0 they cannot issue before the
load's commit.  (The parameter's own documentation says the same: "For mem
refs, if this is 0, the result's time is marked as unpredictable and no
forwarding can take place.")  It is not exactly the paper's stall-on-use: the
release is the load's in-order commit, not the arrival of its data; see
tests/sou/README.md.

Everything else of the memory FU is kept as in MinorDefaultMemFU
(opClasses, opLat = 1, issueLat, srcRegsRelativeLats = [1], description) and
the pool has the same functional units in the same order as
MinorDefaultFUPool.  Predictors are disabled unless asked for, as in the
baseline.  Single CPU, single thread, SE mode: unchanged.
"""

import os
import runpy
import sys

import m5

CANON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "vp_baseline.py"
)

original = m5.instantiate


def instantiate(*args, **kwargs):
    from m5.objects import (
        MinorDefaultFloatSimdFU,
        MinorDefaultIntDivFU,
        MinorDefaultIntFU,
        MinorDefaultIntMulFU,
        MinorDefaultMemFU,
        MinorDefaultMiscFU,
        MinorDefaultPredFU,
        MinorDefaultVecFU,
        MinorFUPool,
        MinorFUTiming,
        Root,
    )

    class StallOnUseMemFU(MinorDefaultMemFU):
        # As MinorDefaultMemFU.timings, with extraAssumedLat 0 (was 2)
        timings = [
            MinorFUTiming(
                description="Mem", srcRegsRelativeLats=[1], extraAssumedLat=0
            )
        ]

    cpu = Root.getInstance().system.cpu
    cpu.executeFuncUnits = MinorFUPool(
        funcUnits=[
            MinorDefaultIntFU(),
            MinorDefaultIntFU(),
            MinorDefaultIntMulFU(),
            MinorDefaultIntDivFU(),
            MinorDefaultFloatSimdFU(),
            MinorDefaultPredFU(),
            StallOnUseMemFU(),
            MinorDefaultMiscFU(),
            MinorDefaultVecFU(),
        ]
    )
    return original(*args, **kwargs)


m5.instantiate = instantiate
sys.argv = [CANON] + sys.argv[1:]
runpy.run_path(CANON, run_name="__main__")
