# gem5 config wrapper for the configuration-guard tests of run_bypass.sh:
# with_params.py, plus a second hardware thread (SMT) on system.cpu, running
# a second copy of the program.  The V$ guards under test are in
# src/cpu/minor/execute.cc (the Execute constructor) and cpu.cc.

import os
import runpy
import sys

import m5

HERE = os.path.dirname(os.path.abspath(__file__))
CMD = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--cmd="))
original = m5.instantiate


def instantiate(*a, **k):
    from m5.objects import (
        Process,
        RiscvInterrupts,
        RiscvISA,
        Root,
    )

    system = Root.getInstance().system
    system.multi_thread = True
    cpu = system.cpu
    cpu.numThreads = 2
    cpu.decoder = []
    cpu.isa = [RiscvISA(), RiscvISA()]
    cpu.interrupts = [RiscvInterrupts(), RiscvInterrupts()]
    p0 = Process(pid=100)
    p0.cmd = [CMD]
    p1 = Process(pid=101)
    p1.cmd = [CMD]
    cpu.workload = [p0, p1]
    cpu.createThreads()
    return original(*a, **k)


m5.instantiate = instantiate
sys.argv = [os.path.join(HERE, "with_params.py")] + sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
