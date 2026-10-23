/*
 * Copyright (c) 2013-2014,2018 ARM Limited
 * All rights reserved
 *
 * The license below extends only to copyright in the software and shall
 * not be construed as granting a license to any other intellectual
 * property including but not limited to intellectual property relating
 * to a hardware implementation of the functionality of the software
 * licensed hereunder.  You may use the software subject to the license
 * terms below provided that you ensure that this notice is replicated
 * unmodified and in its entirety in all distributions of the software,
 * modified or unmodified, in source code or in binary form.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

/**
 * @file
 *
 *  The dynamic instruction and instruction/line id (sequence numbers)
 *  definition for Minor.  A spirited attempt is made here to not carry too
 *  much on this structure.
 */

#ifndef __CPU_MINOR_DYN_INST_HH__
#define __CPU_MINOR_DYN_INST_HH__

#include <iostream>

#include "arch/generic/isa.hh"
#include "base/named.hh"
#include "base/refcnt.hh"
#include "base/types.hh"
#include "cpu/inst_seq.hh"
#include "cpu/minor/buffers.hh"
#include "cpu/static_inst.hh"
#include "cpu/timing_expr.hh"
#include "sim/faults.hh"
#include "sim/insttracer.hh"

namespace gem5
{

namespace minor
{

class MinorDynInst;

/** MinorDynInsts are currently reference counted. */
typedef RefCountingPtr<MinorDynInst> MinorDynInstPtr;

/** Id for lines and instructions.  This includes all the relevant sequence
 *  numbers and thread ids for all stages of execution. */
class InstId
{
  public:
    /** First sequence numbers to use in initialisation of the pipeline and
     *  to be expected on the first line/instruction issued */
    static const InstSeqNum firstStreamSeqNum = 1;
    static const InstSeqNum firstPredictionSeqNum = 1;
    static const InstSeqNum firstLineSeqNum = 1;
    static const InstSeqNum firstFetchSeqNum = 1;
    static const InstSeqNum firstExecSeqNum = 1;

  public:
    /** The thread to which this line/instruction belongs */
    ThreadID threadId;

    /** The 'stream' this instruction belongs to.  Streams are interrupted
     *  (and sequence numbers increased) when Execute finds it wants to
     *  change the stream of instructions due to a branch. */
    InstSeqNum streamSeqNum;

    /** The predicted qualifier to stream, attached by Fetch2 as a
     *  consequence of branch prediction */
    InstSeqNum predictionSeqNum;

    /** Line sequence number.  This is the sequence number of the fetched
     *  line from which this instruction was fetched */
    InstSeqNum lineSeqNum;

    /** Fetch sequence number.  This is 0 for bubbles and an ascending
     *  sequence for the stream of all fetched instructions */
    InstSeqNum fetchSeqNum;

    /** 'Execute' sequence number.  These are assigned after micro-op
     *  decomposition and form an ascending sequence (starting with 1) for
     *  post-micro-op decomposed instructions. */
    InstSeqNum execSeqNum;

  public:
    /** Very boring default constructor */
    InstId(
        ThreadID thread_id = 0, InstSeqNum stream_seq_num = 0,
        InstSeqNum prediction_seq_num = 0, InstSeqNum line_seq_num = 0,
        InstSeqNum fetch_seq_num = 0, InstSeqNum exec_seq_num = 0) :
        threadId(thread_id), streamSeqNum(stream_seq_num),
        predictionSeqNum(prediction_seq_num), lineSeqNum(line_seq_num),
        fetchSeqNum(fetch_seq_num), execSeqNum(exec_seq_num)
    { }

  public:
    /* Equal if the thread and last set sequence number matches */
    bool
    operator== (const InstId &rhs)
    {
        /* If any of fetch and exec sequence number are not set
         *  they need to be 0, so a straight comparison is still
         *  fine */
        bool ret = (threadId == rhs.threadId &&
            lineSeqNum == rhs.lineSeqNum &&
            fetchSeqNum == rhs.fetchSeqNum &&
            execSeqNum == rhs.execSeqNum);

        /* Stream and prediction *must* match if these are the same id */
        if (ret) {
            assert(streamSeqNum == rhs.streamSeqNum &&
                predictionSeqNum == rhs.predictionSeqNum);
        }

        return ret;
    }
};

/** Print this id in the usual slash-separated format expected by
 *  MinorTrace */
std::ostream &operator <<(std::ostream &os, const InstId &id);

class MinorDynInst;

/** Print a short reference to this instruction.  '-' for a bubble and a
 *  series of '/' separated sequence numbers for other instructions.  The
 *  sequence numbers will be in the order: stream, prediction, line, fetch,
 *  exec with exec absent if it is 0.  This is used by MinorTrace. */
std::ostream &operator <<(std::ostream &os, const MinorDynInst &inst);

/** Lifecycle of a confident load value prediction made for a load (the
 *  "producer" role).  The terminal outcome is classified exactly once, where
 *  the instruction leaves Execute's in-flight queue, see
 *  Execute::lvpClassify(). */
enum class LVPProducerState : uint8_t
{
    None,       /* No confident prediction was made for this instruction */
    Predicted,  /* Prediction made, terminal outcome not yet classified */
    Done        /* Classified */
};

/** Result of comparing a prediction with the loaded value.  Recorded when
 *  the memory response is handled and counted when the instruction leaves
 *  the in-flight queue. */
enum class LVPValidation : uint8_t
{
    None,
    Correct,
    Incorrect
};

/** Lifecycle of a load whose request was sent to the LSQ using a predicted
 *  source value (the speculative "consumer" role).  Independent of the
 *  producer role: an instruction can have both. */
enum class LVPConsumerState : uint8_t
{
    None,
    Accepted,   /* Request accepted by the LSQ, terminal outcome pending */
    Done
};

/** Lifecycle of a load looked up in the ASTRA shadow predictor.  The
 *  terminal outcome is classified exactly once, where the instruction
 *  leaves Execute's in-flight queue, see Execute::astraRetire(). */
enum class ASTRAState : uint8_t
{
    None,       /* Not looked up (not an eligible load, or ASTRA is off) */
    Looked,     /* Looked up, terminal outcome not yet classified */
    Done        /* Classified */
};

/** State of a load in the diagnostic V-ASTRA probe (see vastra.hh) */
/** Consumer role of V-ASTRA forwarding (Design B): the request of this load
 *  was built from a published (validated) producer value */
enum class VAFwdConsumer : uint8_t
{
    None,
    Accepted,   /* the request is in the LSQ and used the value */
    Done        /* outcome counted */
};

enum class VAstraState : uint8_t
{
    None,       /* Not decided (no ASTRA prediction, or the probe is off) */
    Rejected,   /* Decided: no candidate */
    Candidate,  /* Decided: a copy of the V$ entry was taken */
    Done        /* A candidate that has had its terminal outcome */
};

/** Dynamic instruction for Minor.
 *  MinorDynInst implements the BubbleIF interface
 *  Has two separate notions of sequence number for pre/post-micro-op
 *  decomposition: fetchSeqNum and execSeqNum */
class MinorDynInst : public RefCounted
{
  private:
    /** A prototypical bubble instruction.  You must call MinorDynInst::init
     *  to initialise this */
    static MinorDynInstPtr bubbleInst;

  public:
    const StaticInstPtr staticInst;

    InstId id;

    /** Trace information for this instruction's execution */
    trace::InstRecord *traceData = nullptr;

    /** The fetch address of this instruction */
    std::unique_ptr<PCStateBase> pc;

    /** This is actually a fault masquerading as an instruction */
    Fault fault;

    /** Tried to predict the destination of this inst (if a control
     *  instruction or a sys call) */
    bool triedToPredict = false;

    /** This instruction was predicted to change control flow and
     *  the following instructions will have a newer predictionSeqNum */
    bool predictedTaken = false;

    /** Predicted branch target */
    std::unique_ptr<PCStateBase> predictedTarget;

    /** Fields only set during execution */

    /** FU this instruction is issued to */
    unsigned int fuIndex = 0;

    /** This instruction is in the LSQ, not a functional unit */
    bool inLSQ = false;

    /** Translation fault in case of a mem ref */
    Fault translationFault;

    /** The instruction has been sent to the store buffer */
    bool inStoreBuffer = false;

    /** Can this instruction be executed out of order.  In this model,
     *  this only happens with mem refs that need to be issued early
     *  to allow other instructions to fill the fetch delay */
    bool canEarlyIssue = false;

    /** Flag controlling conditional execution of the instruction */
    bool predicate = true;

    /** Flag controlling conditional execution of the memory access associated
     *  with the instruction (only meaningful for loads/stores) */
    bool memAccPredicate = true;

    /** execSeqNum of the latest inst on which this inst depends.
     *  This can be used as a sanity check for dependency ordering
     *  where slightly out of order execution is required (notably
     *  initiateAcc for memory ops) */
    InstSeqNum instToWaitFor = 0;

    /** Load value prediction metadata (unused unless enableLVP; see
     *  lvp.hh).  A predicted load's value, and the load-to-load
     *  address speculation state of a dependent load. */
    bool lvpPredicted = false;
    RegVal lvpPredValue = 0;
    InstSeqNum lvpSpecProducer = 0;
    int lvpSpecSrcIdx = -1;
    RegVal lvpSpecSrcVal = 0;
    /** The (unflattened) source register the predicted value stands in for */
    RegId lvpSpecSrcReg;
    /** Set (only around the call that sends the request) while this load
     *  must see lvpSpecSrcVal as its source operand */
    bool lvpSpecActive = false;
    /** Producer role: a speculative load used this prediction */
    bool lvpUsedBySpec = false;
    /** Producer role: this inst raised an LVP recovery redirect.  Fetch2
     *  uses it to tell the redirect from other UnpredictedBranch reasons,
     *  as the inst is not a control instruction and never predicted */
    bool lvpRedirect = false;

    /** Producer role: this inst carries a confident prediction */
    LVPProducerState lvpProdState = LVPProducerState::None;
    /** Producer role: outcome of validating the prediction */
    LVPValidation lvpValidation = LVPValidation::None;
    /** Consumer role: this inst's request used a predicted source value */
    LVPConsumerState lvpConsState = LVPConsumerState::None;

    /** ASTRA shadow predictor (see astra.hh, only used if enableAstra) */
    ASTRAState astraState = ASTRAState::None;
    /** The base address ASTRA predicted for this load at issue */
    Addr astraPredBase = 0;
    /** The base register's value when the effective address was computed */
    RegVal astraActualBase = 0;
    /** astraActualBase was captured (and not under an LVP prediction) */
    bool astraBaseValid = false;

    /** The address and size of this memory operation once its request has
     *  been pushed into the LSQ (set by LSQ::pushRequest).  Used by the
     *  diagnostic V-ASTRA probe to find the address of an older store */
    bool memAddrKnown = false;
    Addr memVaddr = 0;
    unsigned memSize = 0;

    /** Diagnostic V-ASTRA probe (see vastra.hh, only used if
     *  enableVAstraDiag) */
    VAstraState vaState = VAstraState::None;
    /** The effective address predicted from the predicted base */
    Addr vaPredEa = 0;
    /** A copy of the V$ entry for that address, taken at the decision */
    bool vaTagHit = false;
    uint16_t vaValid = 0;
    uint8_t vaData[16] = {};
    /** Stage 2b bypass: the load's response was synthesised from the V$
     *  (no memory request was made); it is not an observation of memory */
    bool vaBypassed = false;
    /** The bypass point was reached and the load was classified as going to
     *  memory (fallback or abandoned): the LSQ can call the bypass hook
     *  again if the memory send fails and is retried, and the load must be
     *  counted, and decided, once */
    bool vaFellBack = false;
    /** The destination registers of this memory reference were marked
     *  unpredictable at issue (scoreboard, extraAssumedLat == 0) */
    bool sbUnpredictable = false;
    /** Design B producer role: the register value this load writes, from
     *  the V$ bytes at the final bypass success */
    RegVal vaRegValue = 0;
    bool vaRegValid = false;
    /** Design B consumer role (enableVAstraForwarding) */
    InstSeqNum vaFwdProducer = 0;
    RegVal vaFwdSrcVal = 0;
    RegId vaFwdSrcReg;
    /** Set only around the call that builds the request: the operand
     *  vaFwdSrcReg reads vaFwdSrcVal.  See ExecContext::getRegOperand */
    bool vaFwdActive = false;
    VAFwdConsumer vaFwdState = VAFwdConsumer::None;
    /** The producer's record was found pending or failed (counted once) */
    uint8_t vaFwdWaited = 0;    /* bit 0 pending, bit 1 failed */
    /** ... and that mark was released early by V-ASTRA (see
     *  Scoreboard::releaseUnpredictable): the commit-time clear must not
     *  decrement again */
    bool vaScoreReleased = false;

    /** Extra delay at the end of the pipeline */
    Cycles extraCommitDelay{0};
    TimingExpr *extraCommitDelayExpr = nullptr;

    /** Once issued, extraCommitDelay becomes minimumCommitCycle
     *  to account for delay in absolute time */
    Cycles minimumCommitCycle{0};

    /** Flat register indices so that, when clearing the scoreboard, we
     *  have the same register indices as when the instruction was marked
     *  up */
    std::vector<RegId> flatDestRegIdx;

  public:
    MinorDynInst(StaticInstPtr si, InstId id_=InstId(), Fault fault_=NoFault) :
        staticInst(si), id(id_), fault(fault_), translationFault(NoFault),
        flatDestRegIdx(si ? si->numDestRegs() : 0)
    { }

  public:
    /** The BubbleIF interface. */
    bool isBubble() const { return id.fetchSeqNum == 0; }

    /** There is a single bubble inst */
    static MinorDynInstPtr bubble() { return bubbleInst; }

    /** Is this a fault rather than instruction */
    bool isFault() const { return fault != NoFault; }

    /** Is this a real instruction */
    bool isInst() const { return !isBubble() && !isFault(); }

    /** Is this a real mem ref instruction */
    bool isMemRef() const { return isInst() && staticInst->isMemRef(); }

    /** Is this an instruction that can be executed `for free' and
     *  needn't spend time in an FU */
    bool isNoCostInst() const;

    /** Assuming this is not a fault, is this instruction either
     *  a whole instruction or the last microop from a macroop */
    bool isLastOpInInst() const;

    /** Print (possibly verbose) instruction information for
     *  MinorTrace using the given Named object's name */
    void minorTraceInst(const Named &named_object) const;

    /** ReportIF interface */
    void reportData(std::ostream &os) const;

    bool readPredicate() const { return predicate; }

    void setPredicate(bool val) { predicate = val; }

    bool readMemAccPredicate() const { return memAccPredicate; }

    void setMemAccPredicate(bool val) { memAccPredicate = val; }

    ~MinorDynInst();
};

/** Print a summary of the instruction */
std::ostream &operator <<(std::ostream &os, const MinorDynInst &inst);

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_DYN_INST_HH__ */
