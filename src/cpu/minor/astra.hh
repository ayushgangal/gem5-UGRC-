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
 *  ASTRA (Address Stride Alias) base address predictor for MinorCPU, from
 *  Ravenel, Perais, Dupont de Dinechin and Petrot, "Architecting Value
 *  Prediction around In-Order Execution", HPCA 2025, Section IV-A.
 *
 *  This is Stage A of an implementation of V-ASTRA: the address predictor
 *  alone, running as a shadow (its predictions are recorded and compared
 *  with the real base address, never used), with uncompressed addresses.
 *  It is not Enhanced V-ASTRA (no LACT, compressor, value cache, CBF).
 *
 *  Representation (paper Section IV-A1 and Table II):
 *   - LAT, 2^n entries selected by the load PC, untagged: the last
 *     committed base address and the "stride index" (an n-bit value).
 *   - SS (Stride -> Stride), 2^n entries, untagged: a signed 16-bit stride.
 *   - prediction = LAT.base + SS[LAT.idx].
 *  Table II lists "Stride 16+n" bits per predictor entry for V-ASTRA (the
 *  16-bit SS stride plus the n-bit LAT stride index, since the predictors
 *  have 2^n entries in each table), which is the reading implemented here.
 *
 *  What the paper does not specify, and the inferences made (all of them
 *  are mirrored by the golden model in tests/astra/check_astra.py):
 *   - PC to LAT index: (pc >> 1) & (entries - 1).
 *   - Stride to SS index ("sized accordingly"): the low n bits of the
 *     16-bit two's complement stride (no hash).
 *   - Update order (order-1 D-FCM): with the previous LAT state (base0,
 *     idx0) and the committed base b,
 *         stride = int16(b - base0)         (wraps if it does not fit)
 *         SS[idx0] = stride
 *         LAT = { base = b, idx = index(stride) }
 *   - Cold start: LAT {0, 0} and SS all 0, so the first lookup predicts 0.
 *     There is no valid bit and no confidence: a prediction is always made.
 *   - A prediction is the plain 64-bit sum (no carry-out handling).
 */

#ifndef __CPU_MINOR_ASTRA_HH__
#define __CPU_MINOR_ASTRA_HH__

#include <cstdint>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "sim/stats.hh"

namespace gem5
{

namespace minor
{

class AstraPredictor : public statistics::Group
{
  public:
    /** Width of an SS stride, from the paper (Section IV-A1) */
    static constexpr unsigned strideBits = 16;
    /** The stride index has at most strideBits bits */
    static constexpr unsigned maxEntries = 1u << strideBits;

    /** What a lookup read, and the prediction made */
    struct LookupInfo
    {
        unsigned index;     /* LAT index */
        Addr latBase;       /* LAT base address */
        unsigned latIdx;    /* LAT stride index (the SS index read) */
        int16_t ssStride;   /* SS[latIdx] */
        Addr predBase;      /* latBase + ssStride */
    };

    /** What a retirement update did */
    struct UpdateInfo
    {
        unsigned index;     /* LAT index */
        Addr prevBase;      /* LAT base before the update */
        unsigned prevIdx;   /* LAT stride index before (SS entry written) */
        int16_t stride;     /* the new stride */
        unsigned newIdx;    /* LAT stride index after */
        bool correct;       /* the prediction equalled the base */
    };

    /**
     * @param parent Parent stats group (the CPU)
     * @param entries Entries of the LAT and of SS: a power of two in
     *        [2, maxEntries]
     */
    AstraPredictor(statistics::Group *parent, unsigned entries);

    /** Look up the prediction for a load at pc.  Starts the load's
     *  lifecycle: it must end in exactly one of retire, squashed, faulted
     *  or baseUnavailable. */
    LookupInfo lookup(Addr pc);

    /** The load retired, without a fault, with this real base address,
     *  having been predicted predicted: update LAT and SS */
    UpdateInfo retire(Addr pc, Addr base, Addr predicted);

    /** Other terminal outcomes of a looked up load; none trains the table */
    void squashed();
    void faulted();
    /** Retired, but its base address was not captured (no training) */
    void baseUnavailable();

    /** Looked up loads without a terminal outcome.  Plain integers, not
     *  statistics, so that statistics resets do not change them */
    uint64_t liveLookups() const { return live; }
    uint64_t carryInAtReset() const { return carry; }

    void resetStats() override;

  private:
    struct LatEntry
    {
        Addr base = 0;
        unsigned idx = 0;
    };

    std::vector<LatEntry> lat;
    std::vector<int16_t> ss;
    const unsigned mask;
    uint64_t live = 0;
    uint64_t carry = 0;

    unsigned latIndex(Addr pc) const { return (pc >> 1) & mask; }
    unsigned ssIndex(int16_t stride) const
    {
        return static_cast<uint16_t>(stride) & mask;
    }
    void finish();

  public:
    statistics::Scalar lookups;
    statistics::Scalar trainings;
    statistics::Scalar baseCorrect;
    statistics::Scalar baseIncorrect;
    statistics::Scalar zeroStrides;
    statistics::Scalar squashedLoads;
    statistics::Scalar faultedLoads;
    statistics::Scalar baseUnavailableLoads;
    statistics::Value inFlight;
    statistics::Value carryIn;
};

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_ASTRA_HH__ */
