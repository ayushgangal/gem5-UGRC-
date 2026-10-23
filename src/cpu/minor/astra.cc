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

#include "cpu/minor/astra.hh"

#include "base/intmath.hh"
#include "base/logging.hh"

namespace gem5
{

namespace minor
{

AstraPredictor::AstraPredictor(statistics::Group *parent, unsigned entries) :
    statistics::Group(parent, "astra"),
    lat(entries),
    ss(entries, 0),
    mask(entries - 1),
    ADD_STAT(lookups, statistics::units::Count::get(),
             "Eligible load lookups (at issue, so includes loads that are "
             "later squashed)"),
    ADD_STAT(trainings, statistics::units::Count::get(),
             "Loads that retired without a fault, with a captured base "
             "address, and updated the LAT and SS"),
    ADD_STAT(baseCorrect, statistics::units::Count::get(),
             "Trained loads whose predicted base address was the real one"),
    ADD_STAT(baseIncorrect, statistics::units::Count::get(),
             "Trained loads whose predicted base address was not the real "
             "one"),
    ADD_STAT(zeroStrides, statistics::units::Count::get(),
             "Trainings whose new stride was zero"),
    ADD_STAT(squashedLoads, statistics::units::Count::get(),
             "Looked up loads that were discarded"),
    ADD_STAT(faultedLoads, statistics::units::Count::get(),
             "Looked up loads that committed with a fault"),
    ADD_STAT(baseUnavailableLoads, statistics::units::Count::get(),
             "Looked up loads that retired without a captured base "
             "address"),
    ADD_STAT(inFlight, statistics::units::Count::get(),
             "Looked up loads without a terminal outcome yet"),
    ADD_STAT(carryIn, statistics::units::Count::get(),
             "inFlight at the most recent statistics reset")
{
    fatal_if(entries < 2 || entries > maxEntries || !isPowerOf2(entries),
        "astraTableEntries must be a power of two in [2, %d] (got %d)",
        maxEntries, entries);

    inFlight.functor([this]() { return liveLookups(); });
    carryIn.functor([this]() { return carryInAtReset(); });
}

AstraPredictor::LookupInfo
AstraPredictor::lookup(Addr pc)
{
    const unsigned index = latIndex(pc);
    const LatEntry &e = lat[index];
    const int16_t stride = ss[e.idx];

    ++lookups;
    ++live;

    return LookupInfo{index, e.base, e.idx, stride,
        e.base + static_cast<Addr>(static_cast<int64_t>(stride))};
}

AstraPredictor::UpdateInfo
AstraPredictor::retire(Addr pc, Addr base, Addr predicted)
{
    LatEntry &e = lat[latIndex(pc)];

    UpdateInfo info;
    info.index = latIndex(pc);
    info.prevBase = e.base;
    info.prevIdx = e.idx;
    /* The difference wraps to 16 bits: a stride that does not fit is
     * truncated, as in a 16-bit stride field */
    info.stride = static_cast<int16_t>(
        static_cast<uint16_t>((base - e.base) & 0xffff));
    info.newIdx = ssIndex(info.stride);
    info.correct = predicted == base;

    ss[e.idx] = info.stride;
    e.base = base;
    e.idx = info.newIdx;

    ++trainings;
    if (info.correct)
        ++baseCorrect;
    else
        ++baseIncorrect;
    if (info.stride == 0)
        ++zeroStrides;
    finish();

    return info;
}

void
AstraPredictor::squashed()
{
    ++squashedLoads;
    finish();
}

void
AstraPredictor::faulted()
{
    ++faultedLoads;
    finish();
}

void
AstraPredictor::baseUnavailable()
{
    ++baseUnavailableLoads;
    finish();
}

void
AstraPredictor::finish()
{
    panic_if(live == 0, "ASTRA: outcome for a load that was not looked up");
    --live;
}

void
AstraPredictor::resetStats()
{
    /* The live count is not a statistic and survives the reset.  Remember
     * it so that outcomes counted after the reset for loads looked up
     * before it can be accounted for:
     *   lookups + carryIn == trainings + squashedLoads + faultedLoads +
     *       baseUnavailableLoads + inFlight */
    carry = live;
    statistics::Group::resetStats();
}

} // namespace minor
} // namespace gem5
