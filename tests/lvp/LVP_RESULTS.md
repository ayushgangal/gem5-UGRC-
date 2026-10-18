# LVP: how it works and what it saves

This document explains how the load-value-prediction (LVP) prototype in gem5
MinorCPU works and compares a run **without LVP** against runs **with LVP** on
the same benchmark. Everything measured today comes from re-running the
scripts in this directory against `build/RISCV/gem5.opt` (the Oct 10 build of
the committed LVP code, which has no ASTRA). Where a figure comes from an
earlier run it says so.

Reproduce with:

```
source ~/gem5-venv/bin/activate
OUT=/tmp/lvp-cycles tests/lvp/run_lvp_cycles.sh          # OFF / CTRL / ON
OUT=/tmp/lvp-cl tests/lvp/run_lvp_stageb.sh run stable    # adds the event log
```

## 1. Headline

| | LVP off | LVP on | Difference |
|---|---|---|---|
| CPU cycles (`system.cpu.numCycles`) | 2,960,967 | 2,935,247 | **25,720 fewer (0.869%)** |
| Checksum | 114688 | 114688 | identical |
| Committed instructions | 163,971 | 163,971 | identical |
| IPC | 0.055378 | 0.055863 | +0.88% |

The saving is real and fully accounted for, but small. Section 7 explains why
it cannot be larger on this benchmark.

## 2. The benchmark: which instructions run

`tests/lvp/ldld.c`, stable variant (freestanding RISC-V, no C library). It
fills an array of 16,384 slots of 64 bytes (a 1 MiB array), each holding a
pointer to one node whose `v[3]` is 7, then sums `node.v[3]` through every slot
and prints the checksum (16,384 x 7 = 114,688).

The loop that LVP targets (`sum_slots`, PCs from the linked binary):

```
10158: ld   a4,0(a5)     # producer: load the pointer from slot i   (cache miss)
1015c: ld   a4,24(a4)    # consumer: load node.v[3] through it
10160: add  a0,a0,a4
10164: addi a5,a5,64     # next slot
10168: bne  a5,a3,10158
```

Instruction budget (sums to the 163,971 committed instructions in every run):

| Part | Instructions |
|---|---|
| fill loop in `_start`, 5 x 16,384 | 81,920 |
| `sum_slots` loop, 5 x 16,384 | 81,920 |
| checksum printing, `_start` set-up, `sum_slots` entry/exit, 3 `ecall`s | 131 |

All 32,768 loads are in `sum_slots`; the fill loop only stores. The assembly is
verified before simulation (`check-asm`): exactly one dependent load pair, no
branch between them, no load anywhere else, no `gp` use.

## 3. How LVP works in this prototype

**Idea.** A load that misses the cache makes its dependents wait. A
*last-value predictor* remembers what a load (by PC) returned last time; after
the same value three times in a row (confidence 3) it predicts it again. A
dependent load can then compute its address from the *predicted* value and go to
memory while the real load is still outstanding. When the real value arrives it
is compared with the prediction: right, the early load is kept; wrong,
everything younger is discarded and refetched.

```
 time -->
 LVP off   P issues ............ P's data returns, P commits | C issues, C commits
 LVP on    P issues ............ P's data returns, P commits
                 C issues here (address from the prediction)   C commits at once
```

**Steps in the code** (`src/cpu/minor/`):

1. **Predict** (`lvpOnIssue`): at issue, an eligible load whose entry in the
   predictor is confident gets a prediction attached, and the predicted value is
   recorded for its destination register.
2. **Early issue** (`lvpSpecAdmissible`): a younger load whose base register is
   that destination may be sent to the memory system while the producer is the
   oldest instruction in flight, in the load/store queue, with no response yet.
   Every normal early-issue check still applies.
3. **Use the prediction without touching the register file**: only while the
   consumer's address is computed, the predicted value is handed to it (a scoped
   override of `getRegOperand`). The predicted value is never written
   architecturally.
4. **Validate** (`lvpValidateProducer`): when the producer's real value
   returns, it is compared with the prediction and the predictor is trained.
5. **Recover if needed**: a prediction that was wrong *and used* redirects fetch
   to the instruction after the producer, discards everything younger through
   the existing stream-change mechanism, and cleans the branch predictor's
   history (a hook in `Fetch2`). The producer itself commits normally. In the
   benchmark here no prediction is ever wrong.
6. **Account**: every prediction and every speculative load ends in exactly one
   outcome (correct/incorrect/faulted/squashed, committed/faulted/squashed),
   classified once where the instruction leaves the in-flight queue.

Parameters (`BaseMinorCPU.py`): `enableLVP` (default off), `lvpSpecAddr`
(default off; consumers use predictions), `lvpConfThreshold` (3),
`lvpTableEntries` (1024), PC window `lvpSpecPcBegin/End`.

## 4. The three runs compared

Identical command except the LVP parameters:

`build/RISCV/gem5.opt -d <dir> configs/deprecated/example/se.py --cpu-type=RiscvMinorCPU --caches -c <ldld> -P 'system.cpu[0].enableIdling=False'` plus

| Run | Extra parameters | Purpose |
|---|---|---|
| OFF | none (`enableLVP=False`) | baseline |
| CTRL | `enableLVP=True`, `lvpSpecAddr=False` | prediction only, a control |
| ON | `enableLVP=True`, `lvpSpecAddr=True` | speculation |

The `config.ini` files of the three runs are identical apart from those two
parameters and each run's own output-directory path. ASTRA is not enabled (the
binary has no ASTRA). No PC window is set because there is no load outside
`sum_slots`.

## 5. Results without and with LVP

### 5.1 Time and architecture

| Statistic | OFF | CTRL | ON |
|---|---|---|---|
| `system.cpu.numCycles` | 2,960,967 | 2,960,967 | **2,935,247** |
| `simTicks` | 1,480,483,500 | 1,480,483,500 | 1,467,623,500 |
| `simSeconds` | 0.001480 | 0.001480 | 0.001468 |
| CPI | 18.0579 | 18.0579 | 17.9010 |
| IPC | 0.055378 | 0.055378 | 0.055863 |
| `simInsts` (committed) | 163,971 | 163,971 | 163,971 |
| Checksum | 114688 | 114688 | 114688 |

The CPU clock is 500 ticks, so cycles = ticks / 500 (the 1000-tick
`system.clk_domain` is the system clock). `numCycles` is simulated CPU cycles,
not host time. CTRL equals OFF in every non-LVP statistic: the prediction
infrastructure costs nothing, so the saving comes from speculation.

### 5.2 Where the cycles go (measured with the committed-trace tick of each PC)

| Phase | OFF | ON |
|---|---|---|
| Set-up: fill loop (until `sum_slots` first runs) | 1,120,862 | 1,120,862 |
| `sum_slots` loop (first instruction to `ret`) | 1,839,832 | 1,814,134 |
| Printing and exit (`ret` to the end) | 273 | 251 |
| Total | 2,960,967 | 2,935,247 |

- The fill loop is identical in both runs, as it must be: LVP does not touch it.
- All of the saving is in the loop LVP targets: **25,698 cycles, 1.40% of the
  `sum_slots` loop**, 112.29 vs 110.72 cycles per iteration, **1.57 cycles per
  iteration**. The remaining 22 cycles are in the printing tail.
- The 0.869% headline is smaller than 1.40% only because the whole-program
  count includes the unaffected fill loop.
- These phase times came from a one-off trace-with-ticks run (the first
  commit tick of PC `0x10144` and of the `ret` at `0x1016c`), not from a script.

### 5.3 Data cache (identical in OFF and ON)

| | Accesses | Hits | Misses |
|---|---|---|---|
| Reads (loads) | 32,769 | 16,384 | 16,385 |
| Writes (fill-loop stores) | 16,393 | 5 | 16,388 |

Every producer load misses and every consumer load hits, exactly as designed.
There is no L2 in this configuration, so a miss goes to memory. The read miss
latency is about 109 CPU cycles (54,636 ticks) in OFF and 108.7 cycles
(54,352 ticks) in ON; an iteration of `sum_slots` is about 112 cycles, so the
loop is essentially one memory miss per iteration.

### 5.4 LVP statistics

| Statistic | CTRL | ON |
|---|---|---|
| lookups | 32,769 | 32,769 |
| confident predictions | 32,761 | 32,761 |
| correct | 32,760 | 32,760 |
| incorrect | 0 | 0 |
| trainings | 32,768 | 32,768 |
| predictions squashed | 1 | 1 |
| **speculative loads issued** | **0** | **16,380** |
| **speculative loads committed** | 0 | **16,380** |
| speculative loads squashed or faulted | 0 | 0 |
| recovery redirects (`mispredictSquashes`) | 0 | 0 |
| live / carry-in counts at the end | 0 | 0 |

- Both the producer and the consumer are predicted, so 32,760 predictions are
  correct out of 16,384 iterations x 2. The one squashed prediction belongs to a
  wrong-path load past the end of the array.
- 16,380 of the 16,384 consumers (99.98%) committed having been
  speculative. The first few iterations train the predictor, so they cannot.
- No misprediction, so no recovery cost appears in this benchmark.

### 5.5 Evidence that the consumer really started early

From the event log (run `stable` of `run_lvp_stageb.sh`): each speculative load
is matched by thread and instruction number to its producer's validation, and in
all 16,380 cases the request was accepted before the validation. The time from
acceptance to validation is **min 21,500, median 50,500, max 338,500 ticks**,
that is **43, 101 and 677 CPU cycles**. (An earlier summary converted these at
1000 ticks per cycle and said "about 50 cycles"; the CPU clock is 500 ticks, so
the median is about 101 cycles.)

## 6. Correctness checks that passed

- All three runs exit 0; the checksum equals the independently computed
  7 x 16,384; the committed instruction count is the same.
- The committed instruction trace is byte-identical with and without LVP
  (A, B, C runs of the stable suite), and the statistics of CTRL equal those of
  OFF outside `cpu.lvp.*`.
- Every prediction and speculative load has exactly one outcome, with zero live
  and carry-in counts at the end; the stable-pointer expectations (no wrong
  prediction, no speculative squash or fault) hold.
- The `config.ini` comparison above, and `enableAstra` absent or false.

## 7. Why the saving is only 1.57 cycles per iteration

Each `sum_slots` iteration takes about 112 cycles, and about 109 of them are
the producer's cache miss. LVP does not shorten that miss. What it removes is
the consumer's own latency (an L1 hit plus the queue handling), which is the
remaining few cycles; it saves about half of them.

It cannot also overlap one iteration's miss with the next iteration's. That is
my reading of the Minor source, not something measured: the next producer's
address register (`a5`) is written by the `addi` of the *current* iteration,
and Minor executes instructions in order at the head of the in-flight queue, so
that `addi` cannot run until the current producer has committed. LVP predicts
the consumer's address, not the loop-carried pointer.

So on this benchmark the gain is bounded by the consumer's latency. A
benchmark whose consumer also misses the cache would show a larger saving, and
one with wrong predictions would pay a recovery cost; neither is measured
here, and the workload was not changed to look for them.

## 8. Other workloads (earlier runs, not re-run for this document)

From the project's test suite (`README.md` section 9), each: C (speculation)
against B (prediction only), same binary.

| Workload | What it exercises | Cycles saved | Notes |
|---|---|---|---|
| stable | one consumer, no wrong prediction | 25,720 (0.87%) | reproduced today |
| phase | wrong predictions at 255 block boundaries | 24,172 (0.81%) | 255 recoveries, recovery cost did not erase the gain |
| fan-out | one producer, two consumers | 27,540 (0.90%) | 255 recoveries, each squashing both consumers |
| chain | three dependent loads | 9,422 (0.10%) | 35 L3 loads accepted, 27 recoveries |
| fault | speculative consumer with an unmapped address | none | cycles equal; 32 speculative faults suppressed |

Every one of them also passes the architectural checks (identical committed
trace, independently computed checksum) and the lifecycle accounting.

## 9. Limits of these results

- One deterministic in-order core (RISC-V MinorCPU, caches, no L2, 2 GHz CPU
  clock, no idling) and one microbenchmark: the numbers show the mechanism
  works and what it saves here, not a general speed-up.
- `numCycles` is the only cycle measure used; no host-time figure is involved.
- The binary is the Oct 10 build of the committed LVP code, not a rebuild of the
  current working tree.
- The breakdown in 5.2 and the explanation in section 7 are measured only for
  the phases and statistics shown; the loop-carried-dependence argument is a
  reading of the source.
