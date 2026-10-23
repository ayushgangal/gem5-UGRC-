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

#include "cpu/minor/vcache.hh"

#include <algorithm>

#include "base/bitfield.hh"
#include "base/logging.hh"

namespace gem5
{

namespace minor
{

VCache::VCache(statistics::Group *parent) :
    statistics::Group(parent, "vcache"),
    ADD_STAT(retiredOps, statistics::units::Count::get(),
             "Memory operations that retired and were given to the shadow "
             "V$ (every other counter below counts them)"),
    ADD_STAT(lookups, statistics::units::Count::get(),
             "Diagnostic lookups: cacheable loads that fit in one line, with "
             "the real address, before the load updates the cache"),
    ADD_STAT(tagHits, statistics::units::Count::get(),
             "Lookups that found an entry with the tag"),
    ADD_STAT(tagMisses, statistics::units::Count::get(),
             "Lookups that found no entry with the tag"),
    ADD_STAT(byteValidHits, statistics::units::Count::get(),
             "Tag hits whose requested bytes were all valid"),
    ADD_STAT(byteInvalidMisses, statistics::units::Count::get(),
             "Tag hits with a requested byte not valid"),
    ADD_STAT(valueCorrect, statistics::units::Count::get(),
             "Byte-valid hits whose bytes equalled the load's bytes"),
    ADD_STAT(valueWrong, statistics::units::Count::get(),
             "Byte-valid hits whose bytes differed from the load's bytes "
             "(a data inconsistency of the model)"),
    ADD_STAT(loadAllocations, statistics::units::Count::get(),
             "Loads that recorded their bytes in an unoccupied entry"),
    ADD_STAT(loadReplacements, statistics::units::Count::get(),
             "Loads that replaced an entry holding another tag"),
    ADD_STAT(loadUpdates, statistics::units::Count::get(),
             "Loads that recorded their bytes in an entry with their tag"),
    ADD_STAT(storeUpdates, statistics::units::Count::get(),
             "Stores that wrote their bytes into a matching entry"),
    ADD_STAT(storeMisses, statistics::units::Count::get(),
             "Stores that matched no entry (not allocated)"),
    ADD_STAT(uncacheableLoads, statistics::units::Count::get(),
             "Uncacheable or strictly ordered loads (not looked up, not "
             "allocated)"),
    ADD_STAT(uncacheableStores, statistics::units::Count::get(),
             "Uncacheable or strictly ordered stores (bytes invalidated)"),
    ADD_STAT(crossLineLoads, statistics::units::Count::get(),
             "Loads that cross a 16 byte boundary (rejected)"),
    ADD_STAT(crossLineStores, statistics::units::Count::get(),
             "Stores that cross a 16 byte boundary (rejected, bytes "
             "invalidated)"),
    ADD_STAT(specialOps, statistics::units::Count::get(),
             "Atomic, load-reserved, store-conditional, masked, prefetch, "
             "cache maintenance, micro-op and sizeless accesses (bytes "
             "invalidated)"),
    ADD_STAT(invalidationOps, statistics::units::Count::get(),
             "Operations that cleared valid bits (or tried to)"),
    ADD_STAT(bytesInvalidated, statistics::units::Count::get(),
             "Valid bytes cleared by those operations"),
    ADD_STAT(syscallFlushes, statistics::units::Count::get(),
             "Full flushes at a committed system call"),
    ADD_STAT(syscallFlushEntries, statistics::units::Count::get(),
             "Occupied entries dropped by those flushes"),
    ADD_STAT(syscallFlushBytes, statistics::units::Count::get(),
             "Valid bytes dropped by those flushes"),
    ADD_STAT(syscallFlushesSkipped, statistics::units::Count::get(),
             "Flushes suppressed by the test-only vastraTestSkipSyscallFlush")
{
    static_assert(numEntries * lineBytes == 4096, "4 KiB of data");
    static_assert((1u << lineShift) == lineBytes, "line size");
    static_assert(numEntries == (1u << (tagShift - indexShift)),
                  "the index covers the bits between the line and the tag");
}

VCache::Snapshot
VCache::snapshot(Addr vaddr) const
{
    const Entry &e = entries[indexOf(vaddr)];
    Snapshot snap;

    if (e.occupied && e.tag == tagOf(vaddr)) {
        snap.tagHit = true;
        snap.valid = e.valid;
        snap.data = e.data;
    }

    return snap;
}

VCache::LoadResult
VCache::loadRetired(Addr vaddr, unsigned size, const uint8_t *data)
{
    panic_if(!fitsInLine(vaddr, size), "V$: load %#x/%d crosses a line",
             vaddr, size);

    Entry &e = entries[indexOf(vaddr)];
    const Addr tag = tagOf(vaddr);
    const unsigned off = vaddr & (lineBytes - 1);
    const uint16_t want = mask(size) << off;

    ++retiredOps;
    ++lookups;

    LoadResult res;
    res.index = indexOf(vaddr);
    res.tag = tag;

    /* The diagnostic lookup, before the cache is updated */
    if (!e.occupied || e.tag != tag) {
        ++tagMisses;
        res.lookup = Lookup::TagMiss;
    } else {
        ++tagHits;
        if ((e.valid & want) != want) {
            ++byteInvalidMisses;
            res.lookup = Lookup::ByteMiss;
        } else {
            ++byteValidHits;
            if (std::equal(data, data + size, e.data.begin() + off)) {
                ++valueCorrect;
                res.lookup = Lookup::Correct;
            } else {
                ++valueWrong;
                res.lookup = Lookup::Wrong;
            }
        }
    }

    /* The update: allocate or replace (clearing the old valid bits), or
     * add the load's bytes to the entry that has its tag */
    if (!e.occupied) {
        ++loadAllocations;
        res.update = Update::Allocate;
        e.occupied = true;
        e.tag = tag;
        e.valid = 0;
    } else if (e.tag != tag) {
        ++loadReplacements;
        res.update = Update::Replace;
        e.tag = tag;
        e.valid = 0;
    } else {
        ++loadUpdates;
        res.update = Update::LoadUpdate;
    }
    std::copy(data, data + size, e.data.begin() + off);
    e.valid |= want;

    return res;
}

VCache::StoreResult
VCache::storeRetired(Addr vaddr, unsigned size, const uint8_t *data)
{
    panic_if(!fitsInLine(vaddr, size), "V$: store %#x/%d crosses a line",
             vaddr, size);

    Entry &e = entries[indexOf(vaddr)];
    const Addr tag = tagOf(vaddr);

    ++retiredOps;

    StoreResult res;
    res.index = indexOf(vaddr);
    res.tag = tag;

    if (e.occupied && e.tag == tag) {
        const unsigned off = vaddr & (lineBytes - 1);
        ++storeUpdates;
        res.update = Update::StoreUpdate;
        std::copy(data, data + size, e.data.begin() + off);
        e.valid |= mask(size) << off;
    } else {
        ++storeMisses;
        res.update = Update::StoreMiss;
    }

    return res;
}

unsigned
VCache::invalidate(Addr vaddr, unsigned size)
{
    unsigned cleared = 0;

    if (size == 0)
        return 0;

    for (Addr line = vaddr >> lineShift;
         line <= (vaddr + size - 1) >> lineShift; line++) {
        Entry &e = entries[line & (numEntries - 1)];
        if (!e.occupied || e.tag != tagOf(line << lineShift))
            continue;

        const Addr start = std::max<Addr>(vaddr, line << lineShift);
        const Addr end = std::min<Addr>(vaddr + size,
                                        (line + 1) << lineShift);
        const uint16_t bits =
            mask(unsigned(end - start)) << (start - (line << lineShift));
        cleared += popCount(uint16_t(e.valid & bits));
        e.valid &= ~bits;
    }

    return cleared;
}

unsigned
VCache::flushAll()
{
    unsigned bytes = 0, occupied_entries = 0;

    for (Entry &e : entries) {
        if (e.occupied)
            occupied_entries++;
        bytes += popCount(e.valid);
        e = Entry();
    }

    ++syscallFlushes;
    syscallFlushEntries += occupied_entries;
    syscallFlushBytes += bytes;
    return bytes;
}

unsigned
VCache::otherRetired(bool is_load, Reason reason, Addr vaddr, unsigned size)
{
    ++retiredOps;

    /* Only a load that crosses a boundary does not need an invalidation:
     * it did not modify anything and was not recorded */
    bool invalidating = true;

    switch (reason) {
      case Reason::Uncacheable:
        ++(is_load ? uncacheableLoads : uncacheableStores);
        break;
      case Reason::CrossLine:
        ++(is_load ? crossLineLoads : crossLineStores);
        invalidating = !is_load;
        break;
      case Reason::Special:
        ++specialOps;
        break;
    }

    unsigned cleared = 0;
    if (invalidating) {
        ++invalidationOps;
        cleared = invalidate(vaddr, size);
        bytesInvalidated += cleared;
    }

    return cleared;
}

} // namespace minor
} // namespace gem5
