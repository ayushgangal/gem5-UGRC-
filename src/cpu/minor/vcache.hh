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
 *  Shadow value cache (V$) for MinorCPU, after Ravenel, Perais, Dupont de
 *  Dinechin and Petrot, "Architecting Value Prediction around In-Order
 *  Execution", HPCA 2025, Section IV-A2 and IV-D.
 *
 *  This is Stage 1 of V-ASTRA: a model of the cache that is maintained from
 *  real memory operations at their non-speculative retirement and never
 *  affects execution.  It is not given a predicted address, supplies no value
 *  to anything, and has no speculative state.  A diagnostic lookup with the
 *  real effective address, made before the retiring load updates the cache,
 *  checks that the cache's bytes are what memory returned.  That is a data
 *  consistency check of the model, NOT an ASTRA or V-ASTRA prediction.
 *
 *  Organisation (the paper's basic one, uncompressed):
 *   - 256 direct-mapped entries of 16 bytes: 4 KiB of data;
 *   - entry index = (vaddr >> 4) & 255, tag = vaddr >> 12 (the whole virtual
 *     page number: the paper's compressed ID and 8 tag bits are not used);
 *   - virtual addresses.
 *
 *  Choices of this model that the paper does not specify (a correctness
 *  oriented design, not paper behaviour):
 *   - each entry has one valid bit per byte.  A load records only the bytes it
 *     read, so the other bytes of its line stay unknown; a lookup hits only if
 *     the tag matches and every requested byte is valid.  The paper's V$
 *     entries hold 16 bytes that a retiring load fills from the memory
 *     hierarchy; that fill is not modelled.
 *   - an entry has an "occupied" flag: the model's equivalent of a valid bit.
 *   - a store updates the bytes it writes in a matching entry (and marks them
 *     valid); it never allocates; a store that does not match is only
 *     counted.
 *   - any access that cannot be maintained exactly clears the valid bits of
 *     the bytes it may modify in the matching entries, so that no stale byte
 *     stays valid: atomics, load-reserved and store-conditional, byte masked
 *     accesses, prefetches, cache maintenance and micro-ops ("special"),
 *     uncacheable and strictly ordered accesses, and stores that cross a
 *     16 byte boundary.  A load that crosses a boundary, or is uncacheable,
 *     is not looked up and does not allocate.
 *
 *  All mutations are made by the caller only for an operation that completed
 *  without a fault and was not discarded, see Execute::vcacheRetire().
 */

#ifndef __CPU_MINOR_VCACHE_HH__
#define __CPU_MINOR_VCACHE_HH__

#include <array>
#include <cstdint>

#include "base/statistics.hh"
#include "base/types.hh"
#include "sim/stats.hh"

namespace gem5
{

namespace minor
{

class VCache : public statistics::Group
{
  public:
    static constexpr unsigned lineBytes = 16;
    static constexpr unsigned lineShift = 4;
    static constexpr unsigned numEntries = 256;
    static constexpr unsigned indexShift = lineShift;
    /** The tag is the virtual address above the 4 KiB the cache covers */
    static constexpr unsigned tagShift = 12;

    /** The diagnostic lookup of a load, before the load updates the cache */
    enum class Lookup : uint8_t
    {
        TagMiss,        /* no entry with this tag */
        ByteMiss,       /* tag match, a requested byte is not valid */
        Correct,        /* all bytes valid and equal to the load's bytes */
        Wrong           /* all bytes valid and different */
    };

    /** What the retiring load or store did to the cache */
    enum class Update : uint8_t
    {
        Allocate,       /* load: the entry was unoccupied */
        Replace,        /* load: the entry held another tag */
        LoadUpdate,     /* load: the entry has this tag */
        StoreUpdate,    /* store: matching entry, bytes written */
        StoreMiss       /* store: no matching entry, nothing done */
    };

    /** The reasons an access is not maintained as an ordinary one */
    enum class Reason : uint8_t
    {
        Uncacheable,
        CrossLine,
        Special
    };

    struct LoadResult
    {
        Lookup lookup;
        Update update;
        unsigned index;
        Addr tag;
    };

    struct StoreResult
    {
        Update update;
        unsigned index;
        Addr tag;
    };

    /** A copy of the entry that a line maps to, for the diagnostic V-ASTRA
     *  probe (see vastra.hh): what a lookup of that line would see now.
     *  Taking it changes nothing and counts nothing. */
    struct Snapshot
    {
        bool tagHit = false;    /* the entry holds this line's tag */
        uint16_t valid = 0;     /* its valid bits (0 unless tagHit) */
        std::array<uint8_t, lineBytes> data{};
    };

    explicit VCache(statistics::Group *parent);

    Snapshot snapshot(Addr vaddr) const;

    static unsigned indexOf(Addr vaddr)
    {
        return (vaddr >> indexShift) & (numEntries - 1);
    }
    static Addr tagOf(Addr vaddr) { return vaddr >> tagShift; }

    /** Does an access of size bytes at vaddr fit in one 16 byte line? */
    static bool
    fitsInLine(Addr vaddr, unsigned size)
    {
        return size >= 1 && (vaddr & (lineBytes - 1)) + size <= lineBytes;
    }

    /** A cacheable load that fits in a line retired with these bytes */
    LoadResult loadRetired(Addr vaddr, unsigned size, const uint8_t *data);

    /** A cacheable store that fits in a line retired with these bytes */
    StoreResult storeRetired(Addr vaddr, unsigned size, const uint8_t *data);

    /** An access that is not maintained as an ordinary one retired.  If it
     *  may have modified memory (is_load false, or a special operation) the
     *  valid bits of the bytes it may have modified are cleared.  Returns
     *  the number of valid bytes cleared. */
    unsigned otherRetired(bool is_load, Reason reason, Addr vaddr,
                          unsigned size);

    /** The state of an entry, for the tests of the model only */
    bool occupied(unsigned index) const { return entries[index].occupied; }

    /** Drop every entry (a committed system call may have changed user
     *  memory behind the LSQ's back).  Returns the valid bytes dropped */
    unsigned flushAll();
    /** The test-only suppression of that flush */
    void flushSkipped() { ++syscallFlushesSkipped; }

  private:
    struct Entry
    {
        bool occupied = false;
        Addr tag = 0;
        std::array<uint8_t, lineBytes> data{};
        uint16_t valid = 0;     /* bit i: byte i of the line is valid */
    };

    std::array<Entry, numEntries> entries;

    /** Clear the valid bits of the bytes [vaddr, vaddr + size) in the
     *  entries that hold their lines; returns the valid bytes cleared */
    unsigned invalidate(Addr vaddr, unsigned size);

  public:
    statistics::Scalar retiredOps;
    statistics::Scalar lookups;
    statistics::Scalar tagHits;
    statistics::Scalar tagMisses;
    statistics::Scalar byteValidHits;
    statistics::Scalar byteInvalidMisses;
    statistics::Scalar valueCorrect;
    statistics::Scalar valueWrong;
    statistics::Scalar loadAllocations;
    statistics::Scalar loadReplacements;
    statistics::Scalar loadUpdates;
    statistics::Scalar storeUpdates;
    statistics::Scalar storeMisses;
    statistics::Scalar uncacheableLoads;
    statistics::Scalar uncacheableStores;
    statistics::Scalar crossLineLoads;
    statistics::Scalar crossLineStores;
    statistics::Scalar specialOps;
    statistics::Scalar invalidationOps;
    statistics::Scalar bytesInvalidated;
    statistics::Scalar syscallFlushes;
    statistics::Scalar syscallFlushEntries;
    statistics::Scalar syscallFlushBytes;
    statistics::Scalar syscallFlushesSkipped;
};

} // namespace minor
} // namespace gem5

#endif /* __CPU_MINOR_VCACHE_HH__ */
