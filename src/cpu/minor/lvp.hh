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

  private:
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
        LVPStats(statistics::Group *parent);

        /** Eligible predictor lookups */
        statistics::Scalar lookups;
        /** Lookups that returned a prediction */
        statistics::Scalar confidentPredictions;
        /** Actual eligible load values used to train the table */
        statistics::Scalar trainings;
        /** Reserved: validated predictions that were used and right */
        statistics::Scalar correct;
        /** Reserved: validated predictions that were used and wrong */
        statistics::Scalar incorrect;
    } stats;
};

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_LVP_HH__ */
