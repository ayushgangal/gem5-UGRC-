# gem5 config wrapper for the targeted ASTRA tests.
#
# Runs configs/research/vp_baseline.py unchanged, with extra
#     -P system.cpu[0].NAME=VALUE
# arguments: they are removed from sys.argv before the baseline parses its
# own options, and are applied to the CPU just before m5.instantiate().  This
# reaches the C++ parameter checks that the baseline's own command line
# options (--astra, --astra-entries, ...) validate first.  True and False are
# booleans, other values are integers (decimal or 0x) if they parse as one.
#
#   gem5.opt CONFIG_DIR/with_params.py --cmd=BINARY \
#       -P 'system.cpu[0].enableAstra=True' \
#       -P 'system.cpu[0].astraTableEntries=3'

import os
import runpy
import sys

import m5

CANON = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "configs",
        "research",
        "vp_baseline.py",
    )
)
# VP_CONFIG (an absolute path) selects another configuration that takes the
# baseline's options, e.g. configs/research/vp_stall_on_use.py (tests/sou)
CANON = os.environ.get("VP_CONFIG", CANON)

params, rest, args = [], [], iter(sys.argv[1:])
for arg in args:
    if arg == "-P":
        params.append(next(args))
    else:
        rest.append(arg)


def parse(value):
    if value in ("True", "False"):
        return value == "True"
    try:
        return int(value, 0)
    except ValueError:
        return value


original = m5.instantiate


def instantiate(*a, **k):
    from m5.objects import Root

    cpu = Root.getInstance().system.cpu
    for p in params:
        name, value = p.split("=", 1)
        setattr(cpu, name.replace("system.cpu[0].", ""), parse(value))
    return original(*a, **k)


m5.instantiate = instantiate
sys.argv = [CANON] + rest
runpy.run_path(CANON, run_name="__main__")
