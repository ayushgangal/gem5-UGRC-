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

#include "cpu/minor/vastra.hh"

#include "base/logging.hh"

namespace gem5
{

namespace minor
{

VAstraDiag::VAstraDiag(statistics::Group *parent) :
    statistics::Group(parent, "vastra"),
    ADD_STAT(predAvailable, statistics::units::Count::get(),
             "Loads with an ASTRA prediction that reached the decision "
             "point (their real base register first available)"),
    ADD_STAT(metadataMissing, statistics::units::Count::get(),
             "Rejected: the real base was not captured or the immediate is "
             "not available"),
    ADD_STAT(baseWrong, statistics::units::Count::get(),
             "Rejected: the predicted base is not the register's value"),
    ADD_STAT(baseCorrect, statistics::units::Count::get(),
             "The predicted base is the register's value"),
    ADD_STAT(rejStoreConflict, statistics::units::Count::get(),
             "Rejected: an older store in flight with a known address "
             "overlaps the load's bytes"),
    ADD_STAT(rejStoreUnknown, statistics::units::Count::get(),
             "Rejected: an older store in flight has no address yet"),
    ADD_STAT(candidates, statistics::units::Count::get(),
             "Loads with a correct base and no conflicting older store: a "
             "copy of the V$ entry was taken"),
    ADD_STAT(candidatesDiscarded, statistics::units::Count::get(),
             "Candidates discarded before they retired"),
    ADD_STAT(candidatesFaulted, statistics::units::Count::get(),
             "Candidates that faulted before they retired"),
    ADD_STAT(rejUnsupported, statistics::units::Count::get(),
             "Candidates that retired as a special, uncacheable or "
             "line-crossing access (not supported by the V$ model)"),
    ADD_STAT(eaMismatch, statistics::units::Count::get(),
             "Candidates whose predicted effective address was not the real "
             "one"),
    ADD_STAT(lookups, statistics::units::Count::get(),
             "Candidates that retired as a supported load: a lookup of the "
             "copy of the V$ entry"),
    ADD_STAT(tagHits, statistics::units::Count::get(),
             "Lookups that found the tag"),
    ADD_STAT(tagMisses, statistics::units::Count::get(),
             "Lookups that did not find the tag"),
    ADD_STAT(byteValidHits, statistics::units::Count::get(),
             "Tag hits whose requested bytes were all valid"),
    ADD_STAT(byteInvalidMisses, statistics::units::Count::get(),
             "Tag hits with a requested byte not valid"),
    ADD_STAT(candidateCorrect, statistics::units::Count::get(),
             "Byte-valid hits whose bytes equalled the load's raw bytes"),
    ADD_STAT(candidateWrong, statistics::units::Count::get(),
             "Byte-valid hits whose bytes differed from the load's raw "
             "bytes"),
    ADD_STAT(verifiedHits, statistics::units::Count::get(),
             "Byte-valid hits that strict verification compared with the "
             "load's raw bytes and found equal (only counted with "
             "enableVAstraVerify; a difference stops the run)"),
    ADD_STAT(candidatesBypassedRetired, statistics::units::Count::get(),
             "Candidates whose response was synthesised from the V$ and "
             "that retired (bypass only)"),
    ADD_STAT(eligibleBypass, statistics::units::Count::get(),
             "Loads that passed every check at the LSQ bypass point"),
    ADD_STAT(syntheticResponses, statistics::units::Count::get(),
             "Loads completed with a synthetic response from the V$"),
    ADD_STAT(memRequestsSuppressed, statistics::units::Count::get(),
             "Memory requests not issued because of a synthetic response"),
    ADD_STAT(scoreboardReleases, statistics::units::Count::get(),
             "Loads whose unpredictable scoreboard marks were released at "
             "the final bypass success (enableVAstraScoreboardRelease)"),
    ADD_STAT(scoreboardReleasedRegs, statistics::units::Count::get(),
             "Destination registers released by those loads"),
    ADD_STAT(scoreboardReleaseNoop, statistics::units::Count::get(),
             "Final bypass successes with no mark to release"),
    ADD_STAT(bypassAbandoned_, statistics::units::Count::get(),
             "Candidates abandoned at the bypass point because the V$ "
             "entry no longer matched the snapshot taken at the decision"),
    ADD_STAT(fbDecisionRejected, statistics::units::Count::get(),
             "Loads with an ASTRA decision but no candidate that went to "
             "memory"),
    ADD_STAT(fbUnsupported, statistics::units::Count::get(),
             "Candidates that went to memory: special, uncacheable or "
             "split access"),
    ADD_STAT(fbCrossLine, statistics::units::Count::get(),
             "Candidates that went to memory: crossing a 16 byte boundary"),
    ADD_STAT(fbEaMismatch, statistics::units::Count::get(),
             "Candidates that went to memory: predicted address is not the "
             "real one"),
    ADD_STAT(fbOlderStore, statistics::units::Count::get(),
             "Candidates that went to memory: an older store in flight "
             "conflicts or has no address (scan repeated at the LSQ)"),
    ADD_STAT(fbTagMiss, statistics::units::Count::get(),
             "Candidates that went to memory: V$ tag miss"),
    ADD_STAT(fbByteMiss, statistics::units::Count::get(),
             "Candidates that went to memory: a requested byte not valid"),
    ADD_STAT(fwdPublished, statistics::units::Count::get(),
             "Validated values published for dependent loads at a final "
             "bypass success (enableVAstraForwarding)"),
    ADD_STAT(fwdRecordsFailed, statistics::units::Count::get(),
             "Forwarding records of candidates that did not reach a final "
             "bypass success (fallback or abandoned)"),
    ADD_STAT(fwdBlockedPending, statistics::units::Count::get(),
             "Dependent loads that found their producer pending (counted "
             "once per consumer)"),
    ADD_STAT(fwdBlockedFailed, statistics::units::Count::get(),
             "Dependent loads that found their producer's record failed "
             "(counted once per consumer)"),
    ADD_STAT(fwdAdmitted, statistics::units::Count::get(),
             "Dependent loads that issued their request with a published "
             "operand value"),
    ADD_STAT(fwdCommitted, statistics::units::Count::get(),
             "Forwarded dependent loads that committed"),
    ADD_STAT(fwdSquashed, statistics::units::Count::get(),
             "Forwarded dependent loads that were discarded"),
    ADD_STAT(fwdFaulted, statistics::units::Count::get(),
             "Forwarded dependent loads that faulted"),
    ADD_STAT(fwdInFlight, statistics::units::Count::get(),
             "Forwarded dependent loads without an outcome yet"),
    ADD_STAT(fwdCarryIn, statistics::units::Count::get(),
             "fwdInFlight at the most recent statistics reset"),
    ADD_STAT(inFlight, statistics::units::Count::get(),
             "Candidates without a terminal outcome yet"),
    ADD_STAT(carryIn, statistics::units::Count::get(),
             "inFlight at the most recent statistics reset")
{
    fwdInFlight.functor([this]() { return liveForwarded(); });
    fwdCarryIn.functor([this]() { return fwdCarryInAtReset(); });
    inFlight.functor([this]() { return liveCandidates(); });
    carryIn.functor([this]() { return carryInAtReset(); });
}

void
VAstraDiag::decided(Decision decision)
{
    ++predAvailable;

    switch (decision) {
      case Decision::MetadataMissing:
        ++metadataMissing;
        break;
      case Decision::BaseWrong:
        ++baseWrong;
        break;
      case Decision::StoreConflict:
        ++baseCorrect;
        ++rejStoreConflict;
        break;
      case Decision::StoreUnknown:
        ++baseCorrect;
        ++rejStoreUnknown;
        break;
      case Decision::Candidate:
        ++baseCorrect;
        ++candidates;
        ++live;
        break;
    }
}

void
VAstraDiag::finish()
{
    panic_if(live == 0, "V-ASTRA probe: outcome for a candidate that was "
             "not decided");
    --live;
}

void
VAstraDiag::discarded()
{
    ++candidatesDiscarded;
    finish();
}

void
VAstraDiag::faulted()
{
    ++candidatesFaulted;
    finish();
}

void
VAstraDiag::retired(Outcome outcome)
{
    finish();

    switch (outcome) {
      case Outcome::Unsupported:
        ++rejUnsupported;
        return;
      case Outcome::EaMismatch:
        ++eaMismatch;
        return;
      default:
        break;
    }

    ++lookups;
    if (outcome == Outcome::TagMiss) {
        ++tagMisses;
        return;
    }
    ++tagHits;
    if (outcome == Outcome::ByteMiss) {
        ++byteInvalidMisses;
        return;
    }
    ++byteValidHits;
    if (outcome == Outcome::Correct)
        ++candidateCorrect;
    else
        ++candidateWrong;
}

void
VAstraDiag::bypassFallback(Fallback reason)
{
    switch (reason) {
      case Fallback::DecisionRejected: ++fbDecisionRejected; break;
      case Fallback::Unsupported: ++fbUnsupported; break;
      case Fallback::CrossLine: ++fbCrossLine; break;
      case Fallback::EaMismatch: ++fbEaMismatch; break;
      case Fallback::OlderStore: ++fbOlderStore; break;
      case Fallback::TagMiss: ++fbTagMiss; break;
      case Fallback::ByteMiss: ++fbByteMiss; break;
    }
}

void
VAstraDiag::retiredBypassed()
{
    ++candidatesBypassedRetired;
    finish();
}

void
VAstraDiag::resetStats()
{
    /* The live count is not a statistic and survives the reset; outcomes
     * counted after the reset for candidates decided before it are
     * accounted for by carryIn */
    carry = live;
    fwdCarry = fwdLive;
    statistics::Group::resetStats();
}

} // namespace minor
} // namespace gem5
