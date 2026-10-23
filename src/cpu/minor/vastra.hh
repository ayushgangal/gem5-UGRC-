/*
 * Copyright (c) 2023 The Regents of the University of California
 * All rights reserved
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
 *  Diagnostic V-ASTRA probe for MinorCPU: Stage 1b of V-ASTRA.  It asks, for
 *  each load that has an ASTRA prediction, what the shadow value cache
 *  (vcache.hh) would have supplied if the predicted base address had been
 *  used (Ravenel et al., HPCA 2025, Sections IV-A to IV-D, the enhanced
 *  variant that predicts the base register and adds the instruction's own
 *  immediate).  The answer is only compared with what the load really read:
 *  nothing is used, the load's operands, response, issue and timing are not
 *  touched.
 *
 *  Lifecycle of a load that has an ASTRA prediction (Execute::vastraDecide,
 *  at the point where its real base register is first available, just before
 *  its effective address is computed):
 *
 *   - no prediction (not looked up by ASTRA): nothing is counted;
 *   - metadataMissing: the real base was not captured (the load ran on an LVP
 *     predicted source) or the immediate cannot be obtained;
 *   - baseWrong: the predicted base is not the register's value;
 *   - baseCorrect: it is.  The effective address is predicted base +
 *     immediate.  Then the older stores that are still in flight are checked
 *     (a MinorCPU choice, NOT the paper's counting Bloom filter and
 *     invalidation FIFO): a store whose address is known and overlaps the
 *     load's bytes (the load's size is not known yet, so 8 bytes) is
 *     rejStoreConflict; otherwise a store whose address is not known yet is
 *     rejStoreUnknown; otherwise the load is a candidate, and a copy of the
 *     V$ entry for the predicted address is taken at that moment.
 *
 *  A candidate has exactly one terminal outcome:
 *   - discarded or faulted before it retired;
 *   - at retirement (Execute::vcacheRetire, with the real request and the raw
 *     bytes): rejUnsupported (special, uncacheable or crossing a 16 byte
 *     boundary: not supported by the V$ model), eaMismatch (the predicted
 *     effective address is not the real one: should not happen when the base
 *     is right), or a lookup of the copy: a tag miss, a byte miss (a
 *     requested byte not valid), or a byte-valid hit whose bytes equal the
 *     load's raw bytes (candidateCorrect) or not (candidateWrong).
 *  A tag match alone is neither.
 *
 *  Identities (for any statistics window; carryIn is inFlight at the last
 *  reset):
 *   predAvailable = metadataMissing + baseWrong + baseCorrect
 *   baseCorrect = rejStoreConflict + rejStoreUnknown + candidates
 *   candidates + carryIn = discarded + faulted + rejUnsupported + eaMismatch
 *                          + lookups + candidatesBypassedRetired + inFlight
 *  (candidatesBypassedRetired is 0 unless enableVAstraBypass: a bypassed
 *  load's response was synthesised from the V$, so it is neither a lookup
 *  nor evidence about memory)
 *   lookups = tagMisses + tagHits; tagHits = byteInvalidMisses +
 *   byteValidHits; byteValidHits = candidateCorrect + candidateWrong
 */

#ifndef __CPU_MINOR_VASTRA_HH__
#define __CPU_MINOR_VASTRA_HH__

#include <cstdint>

#include "base/logging.hh"
#include "base/statistics.hh"
#include "sim/stats.hh"

namespace gem5
{

namespace minor
{

class VAstraDiag : public statistics::Group
{
  public:
    enum class Decision : uint8_t
    {
        MetadataMissing,
        BaseWrong,
        StoreConflict,
        StoreUnknown,
        Candidate
    };

    enum class Outcome : uint8_t
    {
        Unsupported,
        EaMismatch,
        TagMiss,
        ByteMiss,
        Correct,
        Wrong
    };

    explicit VAstraDiag(statistics::Group *parent);

    /** The decision for a load with an ASTRA prediction.  A candidate is
     *  live until one of the terminal calls below */
    void decided(Decision decision);

    /** Terminal outcomes of a candidate */
    void discarded();
    void faulted();
    void retired(Outcome outcome);

    /** Stage 2b bypass (enableVAstraBypass) */
    enum class Fallback : uint8_t
    {
        DecisionRejected,   /* decided at issue: not a candidate */
        Unsupported,        /* special, uncacheable or split access */
        CrossLine,
        EaMismatch,
        OlderStore,         /* the older stores are scanned again */
        TagMiss,
        ByteMiss
    };

    void bypassFallback(Fallback reason);
    void bypassAbandoned() { ++bypassAbandoned_; }
    void bypassEligible() { ++eligibleBypass; }
    void bypassCompleted() { ++syntheticResponses; ++memRequestsSuppressed; }

    /** Design A (enableVAstraScoreboardRelease): a load that reached the
     *  final bypass success released its scoreboard marks on regs
     *  registers; regs == 0: there was nothing to release (the load was not
     *  marked unpredictable at issue: default memory FU timing) */
    void
    scoreboardReleased(unsigned regs)
    {
        if (regs) {
            ++scoreboardReleases;
            scoreboardReleasedRegs += regs;
        } else {
            ++scoreboardReleaseNoop;
        }
    }

    /** A candidate whose response was synthesised retired (terminal) */
    void retiredBypassed();

    /** Strict verification (enableVAstraVerify) checked a byte-valid hit
     *  against the load's raw bytes and they were equal */
    void verified() { ++verifiedHits; }

    /** Candidates without a terminal outcome.  Plain integers, not
     *  statistics, so that statistics resets do not change them */
    /** Design B (enableVAstraForwarding) lifecycle of forwarding consumers:
     *  admitted (a dependent load used a published value), then exactly one
     *  of committed, squashed, faulted.  fwdLive survives statistics resets
     *  (carry in) like the live candidate count:
     *    fwdAdmitted + fwdCarryIn = fwdCommitted + fwdSquashed + fwdFaulted
     *                               + fwdInFlight */
    void fwdPublishedOne() { ++fwdPublished; }
    void fwdRecordFailed() { ++fwdRecordsFailed; }
    void fwdBlocked(bool pending)
    {
        ++(pending ? fwdBlockedPending : fwdBlockedFailed);
    }
    void fwdAdmittedOne() { ++fwdAdmitted; ++fwdLive; }
    void fwdOutcome(int kind)
    {
        panic_if(fwdLive == 0, "V-ASTRA forwarding: outcome for a consumer "
                 "that was not admitted");
        --fwdLive;
        ++(kind == 0 ? fwdCommitted : kind == 1 ? fwdSquashed : fwdFaulted);
    }
    uint64_t liveForwarded() const { return fwdLive; }
    uint64_t fwdCarryInAtReset() const { return fwdCarry; }

    uint64_t liveCandidates() const { return live; }
    uint64_t carryInAtReset() const { return carry; }

    void resetStats() override;

  private:
    uint64_t live = 0;
    uint64_t carry = 0;
    uint64_t fwdLive = 0;
    uint64_t fwdCarry = 0;

    void finish();

  public:
    statistics::Scalar predAvailable;
    statistics::Scalar metadataMissing;
    statistics::Scalar baseWrong;
    statistics::Scalar baseCorrect;
    statistics::Scalar rejStoreConflict;
    statistics::Scalar rejStoreUnknown;
    statistics::Scalar candidates;
    statistics::Scalar candidatesDiscarded;
    statistics::Scalar candidatesFaulted;
    statistics::Scalar rejUnsupported;
    statistics::Scalar eaMismatch;
    statistics::Scalar lookups;
    statistics::Scalar tagHits;
    statistics::Scalar tagMisses;
    statistics::Scalar byteValidHits;
    statistics::Scalar byteInvalidMisses;
    statistics::Scalar candidateCorrect;
    statistics::Scalar candidateWrong;
    statistics::Scalar verifiedHits;
    statistics::Scalar candidatesBypassedRetired;
    statistics::Scalar eligibleBypass;
    statistics::Scalar syntheticResponses;
    statistics::Scalar memRequestsSuppressed;
    statistics::Scalar scoreboardReleases;
    statistics::Scalar scoreboardReleasedRegs;
    statistics::Scalar scoreboardReleaseNoop;
    statistics::Scalar bypassAbandoned_;
    statistics::Scalar fbDecisionRejected;
    statistics::Scalar fbUnsupported;
    statistics::Scalar fbCrossLine;
    statistics::Scalar fbEaMismatch;
    statistics::Scalar fbOlderStore;
    statistics::Scalar fbTagMiss;
    statistics::Scalar fbByteMiss;
    statistics::Scalar fwdPublished;
    statistics::Scalar fwdRecordsFailed;
    statistics::Scalar fwdBlockedPending;
    statistics::Scalar fwdBlockedFailed;
    statistics::Scalar fwdAdmitted;
    statistics::Scalar fwdCommitted;
    statistics::Scalar fwdSquashed;
    statistics::Scalar fwdFaulted;
    statistics::Value fwdInFlight;
    statistics::Value fwdCarryIn;
    statistics::Value inFlight;
    statistics::Value carryIn;
};

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_VASTRA_HH__ */
