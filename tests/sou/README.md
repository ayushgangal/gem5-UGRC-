# Stall-on-use baseline variant (`configs/research/vp_stall_on_use.py`)

Prerequisite of Design A (validated scoreboard release) of
`ISSUE_TIME_VASTRA_DESIGN.md`: a configuration in which a load's dependents
cannot issue before the load's result is available through the existing MinorCPU
model, which is the paper's baseline behaviour.

## What it is
`configs/research/vp_stall_on_use.py` runs `configs/research/vp_baseline.py`
unchanged (`runpy`) and, just before `m5.instantiate()`, replaces
`system.cpu.executeFuncUnits` with a `MinorFUPool` that has the same functional
units in the same order as `MinorDefaultFUPool`, except that the memory FU's
`MinorFUTiming` has `extraAssumedLat=0` instead of `2`. Nothing else differs
(`config.ini` of the two runs differs in exactly that one line; see
`stage2c-stall-on-use/` artifacts). All options of the baseline work unchanged
(predictors are off unless asked for). Single CPU, single thread, SE mode.

| Setting | `vp_baseline.py` | `vp_stall_on_use.py` |
|---|---|---|
| Mem FU `extraAssumedLat` | 2 (Minor default) | **0** |
| everything else in `config.ini` | | identical |

## Why that parameter, and what it really does (source-grounded)
`Execute::issue` (execute.cc) marks a memory reference's destination registers:
`returnCycle = now + opLat + extraAssumedLat`, and
`numUnpredictableResults++` **only if `extraAssumedLat == 0`**.
`Scoreboard::canInstIssue` refuses to issue an instruction while a source register
has `numUnpredictableResults != 0`. The mark is cleared when the load leaves the
in-flight queue (`Execute::commit`: `scoreboard.clearInstDests(inst,
inst->isMemRef())`), i.e. at its in-order commit.
So with 0, dependents cannot issue before the load's commit, and, because
issue is strictly in order, **nothing younger than a stalled dependent issues
either**; instructions between the load and its first use keep issuing.

## What the traces show (`run_sou.sh`, `check_sou.py`)
The workload (`sou.c`) has long-latency loads (cold, non-strided lines) with a
dependent `D`, three independent instructions before it and one after it
(section A), and a variant with an older missing load `M` ahead of an L1-hit
load `L` (section B). Timing is read from `MinorExecute` and `MinorMem` traces
(iterations 1..11; iteration 0 runs cold code):

| | baseline | stall-on-use |
|---|---|---|
| A: `D` issue - `L` issue | 2 cycles | 109..455 cycles |
| A: `D` issue - `L` commit | -107..-465 (before the result) | **0** (same cycle as the load's commit) |
| A: instructions between `L` and `D` | issue at once | issue at once (before `L` commits) |
| A: `J` (independent, after `D`) | issues at once | held behind `D`: `J` issue >= `D` issue |
| B: `D` issue vs `L` data arrival (106..442 cycles earlier than `L`'s commit) | before the arrival | **at `L`'s commit, not at the arrival** |
| architectural output, committed instruction stream | identical | identical |

## Limitations (what this is not)
* The release is the load's **in-order commit**, not the arrival of its data
  (section B): an older miss that holds the head also holds the dependents of a
  younger load whose data is already there. That is stall-on-use in Minor's
  model, stricter than the paper's.
* Dependent loads cannot occupy the memory FU before their producer commits
  (they used to issue early and wait there), so load-load chains are slower
  than in the baseline; ALU dependents wait for the producer's commit and then
  take `opLat` (3 cycles in the Int FU).
* `numCycles` of the two configurations is reported as information only and
  must not be used as a criterion.
* Predictors are off in this characterization; Design A is not implemented.
* One FU pool is replaced; nothing in `src/` is changed.

## Design A: V-ASTRA validated scoreboard release (`run_release.sh`)

`enableVAstraScoreboardRelease` (default false; a fatal error without
`enableVAstraBypass`): when a load reaches the final V-ASTRA bypass success
(`vastraTryBypass`: every eligibility, address, V$, freshness and older-store
check passed, the response exists), `Scoreboard::releaseUnpredictable` removes
the unpredictable marks of its destination registers, so instructions that read
them can issue before the load commits.

**What changes and what does not.** Only `numUnpredictableResults`, which the
single reader `Scoreboard::canInstIssue` tests, is decremented. `numResults`,
`writingInst` (so `execSeqNumToWaitFor`/`instToWaitFor`, which decide when a
dependent memory reference may read its operands in `initiateAcc`), `returnCycle`
and the register file are untouched; no value is forwarded, nothing executes
early. Operand values are read from the register file only at an instruction's
own in-order commit or at a memory reference's early issue, which waits for the
producer's commit; so a release cannot expose a stale operand (checked in the
source: `numUnpredictableResults` is read only at scoreboard.cc `canInstIssue`).
Each dynamic instruction carries `sbUnpredictable` (marked at issue, only when
the memory FU has `extraAssumedLat == 0`) and `vaScoreReleased`; the commit-time
`clearInstDests(inst, isMemRef && !vaScoreReleased)` therefore never decrements a
released mark again (nor another writer's mark). With the default FU timing
(`extraAssumedLat=2`) loads are not marked, the release is a counted no-op
(`vastra.scoreboardReleaseNoop`); the experiment belongs to
`configs/research/vp_stall_on_use.py`. `isDrained` panics if a released run
drains with an unpredictable mark outstanding.

**Tests** (`run_release.sh all`, all under `vp_stall_on_use.py`, P = feature off,
R = on, both with ASTRA, the V$ and the bypass):
* `directed` (`release.c`): an older DRAM-missing load `M` holds the head, then
  the probed load `L`, `I1 I2`, a dependent `D`, `J`. Sections: `ca` candidate
  (released), `bw` base wrong, `tm` V$ tag miss, `sc` older store conflict (not
  released). The checker reads issue/commit ticks, the release line, the bypass
  line, the VAstra decision and the scoreboard's own mark/release/clear events
  (MinorScoreboard) from the traces.
* `selftest`: the scoreboard accounting check fails on a damaged trace (a
  repeated clear, a release without a mark, a mark never decremented).
* `lifecycle`: the programs of `run_vastra.sh` and `run_bypass.sh` (discard,
  fault, store, classify, atomic, abandon, split, fp, sub, special, syscall) and
  the drain / statistics-reset variants: P/R equivalence and the same accounting.
* `config`: rejection without bypass, default false in `config.ini`.

**Reading the results.** Earlier *issue* of the dependent is what Design A
changes. Execution (the ALU result, the register write) still happens at the
dependent's commit, which cannot precede the producer's; the only completion
effect is that the dependent's own functional-unit latency is overlapped with the
producer's wait. See `stage2c-release/` report for the measurements; none
supports an end-to-end speedup claim.

## Design B: V-ASTRA forwarding to dependent loads (`run_forward.sh`)

`enableVAstraForwarding` (default false; needs `enableVAstraBypass` and
`enableVAstraScoreboardRelease`; fatal with `lvpSpecAddr`): a load whose only
source is the result of a load that reached the **final V-ASTRA bypass success**
builds its effective address from that validated value and sends its request
before the producer commits. It is the V-ASTRA twin of the LVP operand override,
with a different binding time and no recovery.

**Mechanism** (execute.cc): `vastraDecide` creates a per-thread record
`vaFwdMap[producer execSeqNum] = {Pending, dest register, stream}` for a
candidate that is an ordinary integer load (`lb lbu lh lhu lw lwu ld`, dest not
x0). `vastraTryBypass` sets it `Validated` with the register value (from the
V$ bytes by the load's opcode) only in its success branch, after every check; every
fallback and the abandonment set it `Failed`. In the early-issue block of
`commit()` a consumer (`lvpEligibleConsumer`: ordinary integer load with one integer
source) that is the front of the memory FU and of `inFUMemInsts`, not yet in the
LSQ, in the right stream, is admitted by `vastraFwdAdmissible` if the record named
by its `instToWaitFor` (the youngest writer of its source, fixed at its issue) is
`Validated`, in its stream and for its source register; otherwise it waits and the
ordinary rule (`head > instToWaitFor`) is untouched, so the forwarding is an
additional path, never a requirement (no deadlock). `VAFwdOperandScope` makes
`ExecContext::getRegOperand` return the value for exactly that operand of exactly
that instruction around `commitInst`; `initiateAcc` forms the address; ASTRA's base
capture uses the forwarded (final) value, so the consumer can itself be decided,
bypassed and published (chains). At its response the consumer's register file
source must equal the forwarded value (panic otherwise, never a recovery).
Records are erased in `vastraPop`; `isDrained` asserts none are left.

**Structural facts that shape the tests.** (1) The LSQ requests queue holds one
request: while a producer's request is *deferred* (an older store in the transfers
queue, an older request ahead) `lsq.canRequest()` is false and no later memory
reference can build a request, so a ready dependent cannot even be evaluated; it
meets a *pending* record only when the producer's request left the requests queue
without the bypass hook, i.e. a translation fault (section `ft`). (2) Under the
stall-on-use configuration a dependent cannot issue before its producer's release,
which happens at the same event as the publication, so none is ever blocked
(pending/failed counters are 0 there); with the default timing they are not.
(3) A store whose address depends on an ALU result computed after an older missing
load cannot reach the LSQ early and blocks the memory FU: the workload computes
store addresses before the older load.

**Tests** (`run_forward.sh all`): `config`, `directed` (`forward.c`: sections
`ch` chain, `bw` base wrong, `tm` V$ tag miss, `sc` older-store conflict, `ab`
abandoned, `pd` pending behind a store, `cm` memory-missing dependent, `ft`
translation fault, `sq` wrong-path squash), in the default and in the stall-on-use
configuration, forwarding off (P) and on (R), and `lifecycle` (the vastra and bypass
programs, the LVP `ldld` chains, drain and statistics-reset variants). The checker
reads the trace: published < dependent's request built (and sent) < producer's commit,
the effective address equal to the published value plus the immediate, binding to
the preceding load of the same iteration, one outcome per consumer, records
created/erased once, the feature-off control (request only after the producer's
commit), scoreboard accounting, one response and one deletion per load, and the
per-window identity `admitted + carryIn = outcomes + inFlight`.

**What it does not do.** Dependent ALU instructions still execute at commit; validation
is at execute/LSQ, not issue; the natural `ldld` chains show records but no forwarding
(their producer is the head when it is published). Measured effect: see the
`stage2c-forward` report: each forwarded memory-missing dependent sends its request
92..468 cycles before its producer commits and commits about one cycle after it
instead of ~94 cycles later; the section span shrinks by 0.3-0.4% because the
delay loop that follows is issue-limited and absorbs the saved commit time.
