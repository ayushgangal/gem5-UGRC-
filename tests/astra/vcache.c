/*
 * Workloads for the shadow value cache tests of run_vcache.sh (see
 * tests/astra/README.md, "Shadow V$ tests").
 *
 *  VC_OPS     A fixed script of loads, stores and atomics on one buffer, each
 *             at an address chosen to exercise one case of the shadow V$
 *             (src/cpu/minor/vcache.hh): allocation, signed and unsigned
 *             subword loads, partial stores, stores that miss, tag
 *             conflicts (4 KiB apart), byte-invalid misses, accesses that
 *             cross a 16 byte boundary, and atomic operations.
 *             check_vcache.py has the expected outcome of every step of the
 *             script (the table SCRIPT there lists them in the order of this
 *             file).
 *  VC_SQUASH  A load on a wrong path (a cold branch that is always taken,
 *             predicted not taken, falls through to it): it is fetched,
 *             issued and discarded and must leave no trace in the V$.
 *  VC_FAULT   Loads from mmap'd pages that have never been touched: the first
 *             load from each takes a translation fault that the SE page fault
 *             handler resolves, and the load is executed again.  The
 *             faulting executions must not update the V$.
 *
 * Every access is a volatile access in the order written (-O1), so the
 * compiler emits exactly one load or store instruction for each.  Offsets are
 * from the start of vbuf, which is 8 KiB aligned.  Freestanding like
 * astra.c: _start and Linux system calls only; no initialised data.
 */

#define SYS_write 64
#define SYS_exit_group 94
#define SYS_mmap 222

typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef short s16;
typedef unsigned int u32;
typedef int s32;
typedef unsigned long u64;
typedef long s64;

static u8 vbuf[16384] __attribute__((aligned(8192)));

#define PTR(T, off) ((volatile T *)(vbuf + (off)))
#define LD(T, off) (*PTR(T, off))
#define ST(T, off, v) (*PTR(T, off) = (v))

static long
syscall6(long num, long arg0, long arg1, long arg2, long arg3, long arg4,
         long arg5)
{
    register long a7 __asm__("a7") = num;
    register long a0 __asm__("a0") = arg0;
    register long a1 __asm__("a1") = arg1;
    register long a2 __asm__("a2") = arg2;
    register long a3 __asm__("a3") = arg3;
    register long a4 __asm__("a4") = arg4;
    register long a5 __asm__("a5") = arg5;

    __asm__ volatile("ecall" : "+r"(a0)
                     : "r"(a7), "r"(a1), "r"(a2), "r"(a3), "r"(a4), "r"(a5)
                     : "memory");

    return a0;
}

static void
print_checksum(long value)
{
    char buf[40];
    int pos = sizeof(buf);
    int neg = value < 0;
    unsigned long v = neg ? -(unsigned long)value : (unsigned long)value;

    buf[--pos] = '\n';
    do {
        buf[--pos] = '0' + v % 10;
        v /= 10;
    } while (v != 0);
    if (neg)
        buf[--pos] = '-';

    syscall6(SYS_write, 1, (long)"checksum ", 9, 0, 0, 0);
    syscall6(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos, 0, 0, 0);
}

#if defined(VC_OPS)

/* A 64 bit load and store with the address given, written in assembly: the
 * compiler splits a misaligned 64 bit access, but the cases that cross a
 * 16 byte boundary need the single instruction */
static long
ld_raw(volatile void *p)
{
    long v;

    __asm__ volatile("ld %0,0(%1)" : "=r"(v) : "r"(p) : "memory");
    return v;
}

static void
sd_raw(volatile void *p, long v)
{
    __asm__ volatile("sd %1,0(%0)" : : "r"(p), "r"(v) : "memory");
}

static long
amoadd_d(volatile void *p, long v)
{
    long old;

    __asm__ volatile("amoadd.d %0,%2,(%1)" : "=r"(old) : "r"(p), "r"(v)
                     : "memory");
    return old;
}

/* A load-reserved and the store-conditional that follows it; returns 0 if
 * the store-conditional succeeded */
static long
lr_sc_d(volatile void *p, long v, long *loaded)
{
    long old, rc;

    __asm__ volatile("lr.d %0,(%2)\n\tsc.d %1,%3,(%2)"
                     : "=&r"(old), "=&r"(rc) : "r"(p), "r"(v) : "memory");
    *loaded = old;
    return rc;
}

static long
lr_d(volatile void *p)
{
    long old;

    __asm__ volatile("lr.d %0,(%1)" : "=r"(old) : "r"(p) : "memory");
    return old;
}

void
_start(void)
{
    long t = 0;
    long tmp;

    /* A: stores that miss, then loads that allocate and hit */
    ST(u64, 0x000, 0x8877665544332211UL);                   /*  1 */
    t += LD(u64, 0x000);                                    /*  2 */
    t += LD(u64, 0x000);                                    /*  3 */
    t += LD(s8, 0x007);                                     /*  4 */
    t += LD(u8, 0x007);                                     /*  5 */
    t += LD(s16, 0x006);                                    /*  6 */
    t += LD(u16, 0x006);                                    /*  7 */
    t += LD(s32, 0x004);                                    /*  8 */
    t += LD(u32, 0x004);                                    /*  9 */

    /* B: a word store, then subword loads */
    ST(u32, 0x010, 0x80ff7f01U);                            /* 10 */
    t += LD(s32, 0x010);                                    /* 11 */
    t += LD(s8, 0x013);                                     /* 12 */
    t += LD(u8, 0x013);                                     /* 13 */

    /* C: partial stores into a valid line */
    t += LD(u64, 0x020);                                    /* 14 */
    ST(u8, 0x022, 0xab);                                    /* 15 */
    ST(u16, 0x024, 0xcdef);                                 /* 16 */
    ST(u8, 0x027, 0x5a);                                    /* 17 */
    t += LD(u64, 0x020);                                    /* 18 */
    t += LD(u8, 0x021);                                     /* 19 */
    t += LD(u16, 0x024);                                    /* 20 */

    /* D: a store to an absent line does not allocate */
    ST(u64, 0x030, 0x0102030405060708UL);                   /* 21 */
    t += LD(u64, 0x030);                                    /* 22 */

    /* E: tag conflicts, 4 KiB apart */
    t += LD(u64, 0x040);                                    /* 23 */
    t += LD(u64, 0x048);                                    /* 24 */
    t += LD(u64, 0x1040);                                   /* 25 */
    t += LD(u64, 0x1048);                                   /* 26 */
    t += LD(u64, 0x040);                                    /* 27 */
    t += LD(u64, 0x1040);                                   /* 28 */

    /* F: byte-invalid misses */
    t += LD(u32, 0x050);                                    /* 29 */
    t += LD(u32, 0x054);                                    /* 30 */
    t += LD(u64, 0x050);                                    /* 31 */

    /* G: accesses that cross a 16 byte boundary */
    t += ld_raw(vbuf + 0x06c);                              /* 32 */
    t += LD(u32, 0x06c);                                    /* 33 */
    t += LD(u64, 0x088);                                    /* 34 */
    t += LD(u64, 0x090);                                    /* 35 */
    sd_raw(vbuf + 0x08c, 0x1122334455667788L);              /* 36 */
    t += LD(u32, 0x088);                                    /* 37 */
    t += LD(u32, 0x08c);                                    /* 38 */
    t += LD(u32, 0x090);                                    /* 39 */
    t += LD(u32, 0x094);                                    /* 40 */

    /* H: atomic operations */
    t += LD(u64, 0x0a0);                                    /* 41 */
    t += amoadd_d(PTR(u64, 0x0a0), 5);                      /* 42 */
    t += LD(u64, 0x0a0);                                    /* 43 */
    t += LD(u64, 0x0b0);                                    /* 44 */
    t += lr_d(PTR(u64, 0x0b0));                             /* 45 */
    t += LD(u64, 0x0b0);                                    /* 46 */
    t += lr_sc_d(PTR(u64, 0x0b0), 9, &tmp);                 /* 47, 48 */
    t += tmp;
    t += LD(u64, 0x0b0);                                    /* 49 */

    /* I: a store makes the bytes it writes valid in a matching entry */
    t += LD(u32, 0x0d0);                                    /* 50 */
    ST(u32, 0x0d4, 0x0a0b0c0dU);                            /* 51 */
    t += LD(u64, 0x0d0);                                    /* 52 */

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(VC_SQUASH)

/*
 * t0 = 1 after a chain of dependent multiplies, so the branch resolves late;
 * it is always taken (t0 != 0), and cold, so predicted not taken.  Its
 * fall-through, vc_wrong, is a load that is never architecturally executed.
 * vc_real is a load that is.  a0 is the pointer.
 */
__asm__(".text\n"
        ".balign 64\n"
        ".globl vc_squash\n"
        "vc_squash:\n"
        "\tli t3,1\n"
        "\tmul t0,t3,t3\n\tmul t0,t0,t0\n\tmul t0,t0,t0\n\tmul t0,t0,t0\n"
        "\tbnez t0,1f\n"
        ".globl vc_wrong\n"
        "vc_wrong:\n\tld t1,0(a0)\n"
        "1:\n"
        ".globl vc_real\n"
        "vc_real:\n\tld a0,64(a0)\n"
        "\tret\n"
        ".size vc_squash, .-vc_squash\n");

extern long vc_squash(volatile void *p);

void
_start(void)
{
    long t = 0;

    ST(u64, 0x240, 0x1111222233334444UL);
    t += vc_squash(vbuf + 0x200);       /* wrong path: +0x200, real: +0x240 */
    t += LD(u64, 0x200);                /* never loaded by a retired load */
    t += LD(u64, 0x240);

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(VC_FAULT)

#define FAULT_ADDR 0x20000000UL
#define FAULT_PAGE 4096
#define FAULT_PROT 3
#define FAULT_MAP_FLAGS 0x32 /* MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED */

void
_start(void)
{
    long t = 0;

    if (syscall6(SYS_mmap, FAULT_ADDR, 2 * FAULT_PAGE, FAULT_PROT,
                 FAULT_MAP_FLAGS, -1, 0) != (long)FAULT_ADDR) {
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    }

    /* Never touched: the first load from each page takes a translation
     * fault, resolved by the SE page fault handler, and is executed again */
    t += *(volatile u64 *)(FAULT_ADDR);
    t += *(volatile u64 *)(FAULT_ADDR);
    t += *(volatile u64 *)(FAULT_ADDR + FAULT_PAGE);
    t += 1;

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#else
#error "define VC_OPS, VC_SQUASH or VC_FAULT"
#endif
