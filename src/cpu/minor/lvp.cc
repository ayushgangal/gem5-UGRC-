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

#include "cpu/minor/lvp.hh"

#include "base/intmath.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "debug/LVP.hh"

namespace gem5
{

namespace minor
{

LastValuePredictor::LastValuePredictor(statistics::Group *parent,
    unsigned entries, unsigned conf_threshold) :
    statistics::Group(parent, "lvp"),
    table(entries),
    indexMask(entries - 1),
    confThreshold(conf_threshold),
    stats(this, *this)
{
    fatal_if(entries == 0 || !isPowerOf2(entries),
        "lvpTableEntries must be a non-zero power of two (got %d)", entries);
    fatal_if(conf_threshold < 1 || conf_threshold > maxConfidence,
        "lvpConfThreshold must be in [1, %d] (got %d)", maxConfidence,
        conf_threshold);
}

LastValuePredictor::LVPStats::LVPStats(statistics::Group *parent,
    const LastValuePredictor &owner) :
    statistics::Group(parent),
    ADD_STAT(lookups, statistics::units::Count::get(),
             "Eligible load predictor lookups"),
    ADD_STAT(confidentPredictions, statistics::units::Count::get(),
             "Lookups that returned a prediction"),
    ADD_STAT(trainings, statistics::units::Count::get(),
             "Eligible loads that completed without fault, not discarded, "
             "and trained the table (predicted or not)"),
    ADD_STAT(correct, statistics::units::Count::get(),
             "Confident predictions whose load committed with the "
             "predicted value"),
    ADD_STAT(incorrect, statistics::units::Count::get(),
             "Confident predictions whose load committed with a different "
             "value"),
    ADD_STAT(predFaulted, statistics::units::Count::get(),
             "Confident predictions whose load faulted before validation"),
    ADD_STAT(predSquashed, statistics::units::Count::get(),
             "Confident predictions whose load was discarded before "
             "validation"),
    ADD_STAT(predInFlight, statistics::units::Count::get(),
             "Confident predictions without a terminal outcome yet"),
    ADD_STAT(predCarryIn, statistics::units::Count::get(),
             "predInFlight at the most recent statistics reset"),
    ADD_STAT(specLoadsIssued, statistics::units::Count::get(),
             "Speculative dependent loads accepted by the LSQ"),
    ADD_STAT(specLoadsCommitted, statistics::units::Count::get(),
             "Speculative dependent loads that committed"),
    ADD_STAT(specLoadsFaulted, statistics::units::Count::get(),
             "Speculative dependent loads that committed with a fault"),
    ADD_STAT(specLoadsSquashed, statistics::units::Count::get(),
             "Speculative dependent loads that were discarded"),
    ADD_STAT(specInFlight, statistics::units::Count::get(),
             "Speculative dependent loads without a terminal outcome yet"),
    ADD_STAT(specCarryIn, statistics::units::Count::get(),
             "specInFlight at the most recent statistics reset"),
    ADD_STAT(mispredictSquashes, statistics::units::Count::get(),
             "Recovery redirects raised because a speculative load used a "
             "prediction that validated incorrect (not every incorrect "
             "prediction, and not the number of discarded instructions)")
{
    predInFlight.functor([&owner]() { return owner.livePredictions(); });
    predCarryIn.functor([&owner]() { return owner.predCarryInAtReset(); });
    specInFlight.functor([&owner]() { return owner.liveSpecLoads(); });
    specCarryIn.functor([&owner]() { return owner.specCarryInAtReset(); });
}

bool
LastValuePredictor::lookup(Addr pc, RegVal &val)
{
    ++stats.lookups;

    const Entry &e = table[index(pc)];
    const bool confident = e.confidence >= confThreshold;

    DPRINTFR(LVP, "table lookup pc: %#x index: %d value: %#x confidence: %d "
        "confident: %d\n", pc, index(pc), e.value, e.confidence, confident);

    if (confident) {
        val = e.value;
        ++stats.confidentPredictions;
        return true;
    }
    return false;
}

void
LastValuePredictor::predictionIssued()
{
    ++livePred;
}

void
LastValuePredictor::producerValidated(bool correct)
{
    panic_if(livePred == 0, "LVP: prediction outcome without live prediction");
    --livePred;
    if (correct)
        ++stats.correct;
    else
        ++stats.incorrect;
}

void
LastValuePredictor::producerFaulted()
{
    panic_if(livePred == 0, "LVP: prediction fault without live prediction");
    --livePred;
    ++stats.predFaulted;
}

void
LastValuePredictor::producerSquashed()
{
    panic_if(livePred == 0, "LVP: prediction squash without live prediction");
    --livePred;
    ++stats.predSquashed;
}

void
LastValuePredictor::consumerAccepted()
{
    ++liveSpec;
    ++stats.specLoadsIssued;
}

void
LastValuePredictor::consumerCommitted()
{
    panic_if(liveSpec == 0, "LVP: speculative load commit without live load");
    --liveSpec;
    ++stats.specLoadsCommitted;
}

void
LastValuePredictor::consumerFaulted()
{
    panic_if(liveSpec == 0, "LVP: speculative load fault without live load");
    --liveSpec;
    ++stats.specLoadsFaulted;
}

void
LastValuePredictor::consumerSquashed()
{
    panic_if(liveSpec == 0, "LVP: speculative load squash without live load");
    --liveSpec;
    ++stats.specLoadsSquashed;
}

void
LastValuePredictor::recoveryRedirect()
{
    ++stats.mispredictSquashes;
}

void
LastValuePredictor::resetStats()
{
    /* The live counts are not statistics and survive the reset.  Remember
     * them so that outcomes counted after the reset for work that started
     * before it can be accounted for:
     *   confidentPredictions + predCarryIn ==
     *       correct + incorrect + predFaulted + predSquashed + predInFlight
     * (and the same for speculative loads). */
    carryPred = livePred;
    carrySpec = liveSpec;
    statistics::Group::resetStats();
}

void
LastValuePredictor::train(Addr pc, RegVal actual)
{
    ++stats.trainings;

    Entry &e = table[index(pc)];
    const uint8_t before = e.confidence;
    const RegVal stored = e.value;

    /* The stored value is compared with the observed one whether or not a
     * prediction was made or used: an entry below the threshold must be
     * able to gain confidence */
    if (e.value == actual) {
        if (e.confidence < maxConfidence)
            ++e.confidence;
    } else if (e.confidence > 0) {
        --e.confidence;
    }
    e.value = actual;

    DPRINTFR(LVP, "table train pc: %#x index: %d stored: %#x actual: %#x "
        "confidence: %d -> %d\n", pc, index(pc), stored, actual, before,
        e.confidence);
}

} // namespace minor
} // namespace gem5
