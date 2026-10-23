# gem5 config wrapper for the configuration-guard tests of run_bypass.sh:
# with_params.py (so -P NAME=VALUE options still apply to system.cpu[0]),
# plus a second CPU in the same system, connected like the first, running
# its own copy of the program.  EXTRA_CPU selects its type: minor (default)
# or timing (RiscvTimingSimpleCPU).  The guard under test is the MinorCPU
# check of the number of CPUs (src/cpu/minor/cpu.cc, MinorCPU::init).

import os
import runpy
import sys

import m5

HERE = os.path.dirname(os.path.abspath(__file__))
KIND = os.environ.get("EXTRA_CPU", "minor")
CMD = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--cmd="))

original = m5.instantiate


def instantiate(*a, **k):
    from common.Caches import L1_DCache, L1_ICache
    from m5.objects import (
        Process,
        RiscvMinorCPU,
        RiscvTimingSimpleCPU,
        Root,
    )

    system = Root.getInstance().system
    cls = {"minor": RiscvMinorCPU, "timing": RiscvTimingSimpleCPU}[KIND]
    cpu2 = cls()
    system.cpu2 = cpu2
    cpu2.addPrivateSplitL1Caches(
        L1_ICache(size="32kB"), L1_DCache(size="64kB")
    )
    cpu2.connectAllPorts(
        system.tol2bus.cpu_side_ports,
        system.membus.cpu_side_ports,
        system.membus.mem_side_ports,
    )
    cpu2.createInterruptController()
    process = Process(pid=101)
    process.cmd = [CMD]
    cpu2.workload = process
    cpu2.createThreads()
    return original(*a, **k)


m5.instantiate = instantiate
sys.argv = [os.path.join(HERE, "with_params.py")] + sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
