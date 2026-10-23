# gem5 config wrapper: with_params.py, but m5.simulate() runs in chunks of
# DRAIN_CHUNK ticks (default 7321000).  After each chunk it drains the system
# (unless DRAIN_MODE=reset: then loads can be in flight at the boundary),
# dumps the statistics (one window in stats.txt per chunk) and resets them
# before resuming.  Used by run_bypass.sh to test loads that are pending or
# completed at a drain and a statistics reset.  Takes the arguments of
# with_params.py.

import os
import runpy
import sys

import m5

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = int(os.environ.get("DRAIN_CHUNK", "7321000"))
DRAIN = os.environ.get("DRAIN_MODE", "drain") != "reset"
LIMIT = "simulate() limit reached"
original = m5.simulate


def simulate(*args, **kwargs):
    while True:
        event = original(CHUNK)
        if event.getCause() != LIMIT:
            return event
        if DRAIN:
            m5.drain()
        m5.stats.dump()
        m5.stats.reset()
        print(
            "drained and reset statistics @ tick"
            if DRAIN
            else "reset statistics (no drain) @ tick",
            m5.curTick(),
        )


m5.simulate = simulate
sys.argv = [os.path.join(HERE, "with_params.py")] + sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
