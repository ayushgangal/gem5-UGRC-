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
    stats(this)
{
    fatal_if(entries == 0 || !isPowerOf2(entries),
        "lvpTableEntries must be a non-zero power of two (got %d)", entries);
    fatal_if(conf_threshold < 1 || conf_threshold > maxConfidence,
        "lvpConfThreshold must be in [1, %d] (got %d)", maxConfidence,
        conf_threshold);
}

LastValuePredictor::LVPStats::LVPStats(statistics::Group *parent) :
    statistics::Group(parent),
    ADD_STAT(lookups, statistics::units::Count::get(),
             "Eligible load predictor lookups"),
    ADD_STAT(confidentPredictions, statistics::units::Count::get(),
             "Lookups that returned a prediction"),
    ADD_STAT(trainings, statistics::units::Count::get(),
             "Actual eligible load values used to train the table"),
    ADD_STAT(correct, statistics::units::Count::get(),
             "Predictions made on dynamic loads that validated correct "
             "(reserved, not yet populated)"),
    ADD_STAT(incorrect, statistics::units::Count::get(),
             "Predictions made on dynamic loads that validated incorrect "
             "(reserved, not yet populated)")
{
}

bool
LastValuePredictor::lookup(Addr pc, RegVal &val)
{
    ++stats.lookups;

    const Entry &e = table[index(pc)];
    if (e.valid && e.tag == pc && e.confidence >= confThreshold) {
        val = e.value;
        ++stats.confidentPredictions;
        return true;
    }
    return false;
}

void
LastValuePredictor::train(Addr pc, RegVal actual)
{
    ++stats.trainings;

    Entry &e = table[index(pc)];
    if (e.valid && e.tag == pc) {
        if (e.value == actual) {
            if (e.confidence < maxConfidence)
                ++e.confidence;
        } else {
            e.value = actual;
            e.confidence = 0;
        }
    } else {
        e.valid = true;
        e.tag = pc;
        e.value = actual;
        e.confidence = 0;
    }
}

} // namespace minor
} // namespace gem5
