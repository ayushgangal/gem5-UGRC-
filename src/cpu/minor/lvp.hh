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
 *  Last-value predictor for MinorCPU load value prediction (LVP).
 */

#ifndef __CPU_MINOR_LVP_HH__
#define __CPU_MINOR_LVP_HH__

#include <cstdint>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "sim/stats.hh"

namespace gem5
{

namespace minor
{

/** Direct-mapped, PC-indexed last-value predictor with 2-bit saturating
 *  confidence counters.  A prediction is only returned once the counter
 *  has reached the configured threshold. */
class LastValuePredictor : public statistics::Group
{
  public:
    /** Maximum value of the 2-bit saturating confidence counter */
    static constexpr uint8_t maxConfidence = 3;

    /**
     * @param parent Parent stats group (the CPU)
     * @param entries Number of table entries, a non-zero power of two
     * @param conf_threshold Counter value (1..maxConfidence) at which a
     *        prediction is made
     */
    LastValuePredictor(statistics::Group *parent, unsigned entries,
                       unsigned conf_threshold);

    /** Look up a prediction for an eligible load at pc.
     *  @return true and set val only if the entry is confident. */
    bool lookup(Addr pc, RegVal &val);

    /** Train the table with the actual value of an eligible load at pc. */
    void train(Addr pc, RegVal actual);

    /** @name Prediction (producer) lifecycle
     *  A confident prediction is attached to an instruction at issue and
     *  must reach exactly one terminal outcome: validated (correct or
     *  incorrect), faulted or squashed.  Each terminal call requires a
     *  live prediction and panics otherwise.
     *  @{ */
    void predictionIssued();
    void producerValidated(bool correct);
    void producerFaulted();
    void producerSquashed();
    /** @} */

    /** @name Speculative dependent load (consumer) lifecycle
     *  A load whose request was accepted by the LSQ using a predicted source
     *  value must reach exactly one terminal outcome.
     *  @{ */
    void consumerAccepted();
    void consumerCommitted();
    void consumerFaulted();
    void consumerSquashed();
    /** @} */

    /** Predictions / speculative loads that have not yet reached a terminal
     *  outcome.  Plain integers, not statistics, so that statistics resets
     *  do not change them */
    uint64_t livePredictions() const { return livePred; }
    uint64_t liveSpecLoads() const { return liveSpec; }

    /** Live counts recorded by the most recent statistics reset */
    uint64_t predCarryInAtReset() const { return carryPred; }
    uint64_t specCarryInAtReset() const { return carrySpec; }

    /** Record the live counts, then reset the statistics */
    void resetStats() override;

  private:
    uint64_t livePred = 0;
    uint64_t liveSpec = 0;
    uint64_t carryPred = 0;
    uint64_t carrySpec = 0;

    struct Entry
    {
        bool valid = false;
        Addr tag = 0;
        RegVal value = 0;
        uint8_t confidence = 0;
    };

    std::vector<Entry> table;
    const uint64_t indexMask;
    const uint8_t confThreshold;

    /** RISC-V instructions are at least 2-byte aligned */
    uint64_t index(Addr pc) const { return (pc >> 1) & indexMask; }

  public:
    struct LVPStats : public statistics::Group
    {
        LVPStats(statistics::Group *parent, const LastValuePredictor &owner);

        /** Eligible predictor lookups (at issue, so includes loads that
         *  are later squashed) */
        statistics::Scalar lookups;
        /** Lookups that returned a prediction */
        statistics::Scalar confidentPredictions;
        /** Eligible loads that completed (not discarded, no fault) and
         *  trained the table, whether or not they were predicted */
        statistics::Scalar trainings;

        /** Terminal outcomes of confident predictions */
        statistics::Scalar correct;
        statistics::Scalar incorrect;
        statistics::Scalar predFaulted;
        statistics::Scalar predSquashed;
        /** Confident predictions without a terminal outcome yet */
        statistics::Value predInFlight;
        /** Value of predInFlight at the most recent statistics reset */
        statistics::Value predCarryIn;

        /** Speculative dependent loads accepted by the LSQ, and their
         *  terminal outcomes */
        statistics::Scalar specLoadsIssued;
        statistics::Scalar specLoadsCommitted;
        statistics::Scalar specLoadsFaulted;
        statistics::Scalar specLoadsSquashed;
        statistics::Value specInFlight;
        statistics::Value specCarryIn;
    } stats;
};

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_LVP_HH__ */
