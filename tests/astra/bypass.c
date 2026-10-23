/*
 * Workloads for the V-ASTRA bypass tests of run_bypass.sh (see README.md,
 * "V-ASTRA bypass tests").  The probed load is one instruction at its own
 * PC (bp_probe), called once per iteration with a delay in between, so that
 * ASTRA has trained on the previous iteration when it is looked up.
 *
 *  BP_LAT      latency to hide: a ring of 32 nodes (64 bytes apart, each
 *              holding the address of the next) is walked (the V$ has the
 *              bytes), then 1 MiB is written by stores (they evict the nodes
 *              from L1 and L2; a store does not touch a V$ entry that holds
 *              another tag), then the ring is walked again: every load's
 *              address is the previous load's value, so a load that misses
 *              to DRAM delays all the following ones, unless it is
 *              bypassed.  Three laps.
 *  BP_SYSCALL  stale V$: 24 doublewords are loaded (warm), probed (bypass
 *              candidates, the old values), then a read system call
 *              overwrites all of them behind the LSQ's back, then they are
 *              probed again.  Every value is checked against what it must be;
 *              "errors N" is printed, and must be 0.
 *  BP_SPECIAL  load-reserved and atomic loads on lines the V$ holds: never
 *              bypassed.
 *  BP_ATOMIC   an older amoadd in flight when the candidate load is decided
 *              (overlapping it, and a control that does not).
 *  BP_SUB      lb lbu lh lhu lw lwu candidates (extension of synthetic data).
 *  BP_ORDER    an older store with late data: is its address known before a
 *              younger candidate is decided?
 *  BP_ABANDON  the V$ entry of the candidate is replaced between its decision
 *              and its send (the bypass is abandoned).
 *  BP_SPLIT    loads that cross a 64 byte cache line (split LSQ requests).
 *  BP_FP       fld and flw candidates, before and after fsd/fsw stores.
 *  BP_SYN      a file mapped twice MAP_SHARED: are virtual synonyms possible?
 *  BP_SHM, BP_MEMFD  other ways to share memory, unimplemented in SE.
 *
 * Freestanding like vastra.c: _start and Linux system calls only.
 */

#define SYS_pipe2 59
#define SYS_read 63
#define SYS_write 64
#define SYS_exit_group 94

#ifndef DELAY
#define DELAY 300
#endif

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;

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
print_num(const char *label, long value, int len)
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

    syscall6(SYS_write, 1, (long)label, len, 0, 0, 0);
    syscall6(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos, 0, 0, 0);
}

static inline void
delay(void)
{
    long n = DELAY;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

static u8 buf[8192] __attribute__((aligned(4096)));
#define FAR_BYTES (1UL << 20)
static u8 farbuf[FAR_BYTES] __attribute__((aligned(4096)));

/* bp_warm loads the 16 bytes of a line; bp_probe loads 8 bytes at +8 (the
 * lines variant) and bp_probe0 loads 8 bytes at +0 (the doubleword variant).
 * a0 is the address. */
__asm__(".text\n"
        ".balign 64\n"
        ".globl bp_warm\nbp_warm:\n\tld a1,0(a0)\n\tld a2,8(a0)\n\tret\n"
        ".size bp_warm, .-bp_warm\n"
        ".balign 16\n"
        ".globl bp_probe\nbp_probe:\n\tld a0,8(a0)\n\tret\n"
        ".size bp_probe, .-bp_probe\n"
        ".balign 16\n"
        ".globl bp_warm0\nbp_warm0:\n\tld a0,0(a0)\n\tret\n"
        ".size bp_warm0, .-bp_warm0\n"
        ".balign 16\n"
        ".globl bp_probe0\nbp_probe0:\n\tld a0,0(a0)\n\tret\n"
        ".size bp_probe0, .-bp_probe0\n"
        ".balign 16\n"
        ".globl bp_step\nbp_step:\n\tld a0,0(a0)\n\tret\n"
        ".size bp_step, .-bp_step\n");

extern long bp_warm(const void *p);
extern long bp_probe(const void *p);
extern long bp_warm0(const void *p);
extern long bp_probe0(const void *p);
extern const void *bp_step(const void *p);

#if defined(BP_LAT)

/* bp_chase(p, n): n dependent loads, p = *p, one load instruction, with a
 * delay after each so that ASTRA has been trained on the previous one when
 * the next is looked up (it learns at retirement) */
static long
bp_chase(const void *p, long n)
{
    while (n-- > 0) {
        p = bp_step(p);
        delay();
    }
    return (long)p;
}

#define NODES 32
#define NODE(k) (256 * ((k) / 2) + 64 * ((k) & 1))

void
_start(void)
{
    long t = 0;

    /* The nodes are alternately 64 and 192 bytes apart: a repeating stride
     * pattern that ASTRA's stride sequences can learn, and that the L1's
     * stride prefetcher (which needs one stride repeated) cannot use */
    for (int k = 0; k < NODES; k++)
        *(volatile u64 *)(buf + NODE(k)) = (u64)(buf + NODE((k + 1) % NODES));

    for (int lap = 0; lap < 3; lap++) {
        /* Loads the ring (the V$ gets the bytes), then 1 MiB of stores
         * evict it from L1 and L2, then it is walked again: each load
         * misses to DRAM unless it is bypassed, and its address is the
         * previous load's value */
        t += (long)bp_chase(buf, NODES);
        for (unsigned long i = 0; i < FAR_BYTES; i += 64)
            *(volatile u64 *)(farbuf + i) = i + lap;
        delay();
        t += (long)bp_chase(buf, NODES);
        t += (long)bp_chase(buf, NODES + lap);
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SYSCALL)

#define N 24
#define OLD(i) ((u64)(i) * 0x0101010101010101UL + 0x1111UL)
#define NEW(i) ((u64)(i) * 0x0f0f0f0f0f0f0f0fUL + 0x7777UL)

static u64 src[N] __attribute__((aligned(64)));

void
_start(void)
{
    long t = 0, errors = 0;
    int fds[2];

    for (int i = 0; i < N; i++) {
        *(volatile u64 *)(buf + 8 * i) = OLD(i);
        src[i] = NEW(i);
    }

    if (syscall6(SYS_pipe2, (long)fds, 0, 0, 0, 0, 0) != 0)
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    syscall6(SYS_write, fds[1], (long)src, sizeof(src), 0, 0, 0);

    for (int i = 0; i < N; i++)
        t += bp_warm0(buf + 8 * i);
    delay();
    for (int i = 0; i < N; i++) {
        u64 v = bp_probe0(buf + 8 * i);
        errors += v != OLD(i);
        t += v;
        delay();
    }

    /* Overwrites all of buf[0, 192) through the functional proxy */
    if (syscall6(SYS_read, fds[0], (long)buf, sizeof(src), 0, 0, 0) !=
        (long)sizeof(src)) {
        errors += 1000;
    }

    for (int i = 0; i < N; i++) {
        u64 v = bp_probe0(buf + 8 * i);
        errors += v != NEW(i);
        t += v;
        delay();
    }

    print_num("errors ", errors, 7);
    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SPECIAL)

/* A load-reserved and an atomic add at one PC each, on lines that were just
 * loaded (the V$ holds their bytes) with a constant stride: neither is an
 * ordinary load, so neither may ever be a bypass candidate */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_lr\nbp_lr:\n\tlr.d a0,(a0)\n\tret\n"
        ".size bp_lr, .-bp_lr\n"
        ".balign 16\n"
        ".globl bp_amo\nbp_amo:\n\tli a1,1\n\tamoadd.d a0,a1,(a0)\n\tret\n"
        ".size bp_amo, .-bp_amo\n");

extern long bp_lr(const void *p);
extern long bp_amo(const void *p);

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 24 * 64; i += 8)
        *(volatile u64 *)(buf + i) = (u64)i + 5;
    for (int i = 0; i < 24; i++)
        t += bp_warm(buf + 64 * i);
    delay();
    for (int i = 0; i < 24; i++) {
        t += bp_lr(buf + 64 * i);
        delay();
    }
    for (int i = 0; i < 24; i++) {
        t += bp_amo(buf + 64 * i);
        delay();
    }
    for (int i = 0; i < 24; i++) {
        t += bp_probe0(buf + 64 * i);
        delay();
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_ATOMIC)

/*
 * An older atomic add in flight when the candidate load is decided.  a0 = the
 * probed line (the load reads +8), a1 = a far address (a load that misses
 * keeps everything younger from retiring), a2 = the addend.  bp_aconf adds to
 * the doubleword the load reads, bp_actrl to another one of the line's
 * neighbours (a different V$ entry).
 */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_aconf\nbp_aconf:\n\tld t1,0(a1)\n\taddi t2,a0,8\n"
        "\tamoadd.d zero,a2,(t2)\n"
        ".globl bp_acl\nbp_acl:\n\tld a0,8(a0)\n\tret\n"
        ".size bp_aconf, .-bp_aconf\n"
        ".balign 16\n"
        ".globl bp_actrl\nbp_actrl:\n\tld t1,0(a1)\n\taddi t2,a0,24\n"
        "\tamoadd.d zero,a2,(t2)\n"
        ".globl bp_acc\nbp_acc:\n\tld a0,8(a0)\n\tret\n"
        ".size bp_actrl, .-bp_actrl\n");

extern long bp_aconf(const void *p, const void *far, long v);
extern long bp_actrl(const void *p, const void *far, long v);

#define N 16

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 2 * 64 * N; i += 8)
        *(volatile u64 *)(buf + i) = (u64)i * 0x9e3779b97f4a7c15UL + 3;
    for (int i = 0; i < N; i++)
        t += bp_warm(buf + 64 * i);
    delay();
    for (int i = 0; i < N; i++) {
        t += bp_aconf(buf + 64 * i, farbuf + 2048 + 64 * i, 1);
        delay();
    }
    for (int i = 0; i < N; i++)
        t += bp_warm(buf + 1024 + 64 * i);
    delay();
    for (int i = 0; i < N; i++) {
        t += bp_actrl(buf + 1024 + 64 * i, farbuf + 2048 + 64 * i, 1);
        delay();
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SUB)

/* Sub-doubleword integer loads at one PC each, on doublewords the V$ holds:
 * sign and zero extension of a synthetic response */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_lb\nbp_lb:\n\tlb a0,3(a0)\n\tret\n"
        ".balign 16\n"
        ".globl bp_lbu\nbp_lbu:\n\tlbu a0,3(a0)\n\tret\n"
        ".balign 16\n"
        ".globl bp_lh\nbp_lh:\n\tlh a0,2(a0)\n\tret\n"
        ".balign 16\n"
        ".globl bp_lhu\nbp_lhu:\n\tlhu a0,2(a0)\n\tret\n"
        ".balign 16\n"
        ".globl bp_lw\nbp_lw:\n\tlw a0,4(a0)\n\tret\n"
        ".balign 16\n"
        ".globl bp_lwu\nbp_lwu:\n\tlwu a0,4(a0)\n\tret\n");

extern long bp_lb(const void *p);
extern long bp_lbu(const void *p);
extern long bp_lh(const void *p);
extern long bp_lhu(const void *p);
extern long bp_lw(const void *p);
extern long bp_lwu(const void *p);

#define N 24
#define PAT(i) (((u64)(i) + 1) * 0x9e3779b97f4a7c15UL)

void
_start(void)
{
    long t = 0, errors = 0;

    for (int i = 0; i < N; i++)
        *(volatile u64 *)(buf + 8 * i) = PAT(i);
    for (int i = 0; i < N; i++)
        t += bp_warm0(buf + 8 * i);
    delay();
#define PROBE(fn, expr)                                                    \
    for (int i = 0; i < N; i++) {                                          \
        u64 w = PAT(i);                                                    \
        long v = fn(buf + 8 * i);                                          \
        errors += v != (long)(expr);                                       \
        t += v;                                                            \
        delay();                                                           \
    }
    PROBE(bp_lb, (signed char)(w >> 24))
    PROBE(bp_lbu, (unsigned char)(w >> 24))
    PROBE(bp_lh, (short)(w >> 16))
    PROBE(bp_lhu, (unsigned short)(w >> 16))
    PROBE(bp_lw, (int)(w >> 32))
    PROBE(bp_lwu, (unsigned int)(w >> 32))

    print_num("errors ", errors, 7);
    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_ORDER)

/*
 * An older store whose data is late: its data register is the result of a
 * load that misses to DRAM, so the store cannot compute its address and send
 * its request until that load has committed.  a0 = the probed line, a1 = a
 * far address, a3 = the store address.  If a younger load could be decided
 * before an older store has its address, the "unknown address" branch of the
 * store scan would be reachable; the MinorMem trace shows the order.
 */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_ord\nbp_ord:\n\tld t1,0(a1)\n"
        ".globl bp_ordst\nbp_ordst:\n\tsd t1,0(a3)\n"
        ".globl bp_ordc\nbp_ordc:\n\tld a0,8(a0)\n\tret\n"
        ".size bp_ord, .-bp_ord\n");

extern long bp_ord(const void *p, const void *far, long v, const void *st);

#define N 24

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 64 * N; i += 8)
        *(volatile u64 *)(buf + i) = (u64)i * 0x9e3779b97f4a7c15UL + 5;
    for (int i = 0; i < N; i++)
        t += bp_warm(buf + 64 * i);
    delay();
    for (int i = 0; i < N; i++) {
        t += bp_ord(buf + 64 * i, farbuf + 2048 + 64 * i, i,
                    buf + 4096 + 64 * i + 32);
        delay();
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_ABANDON)

/*
 * The V$ entry of the candidate changes between its decision and its send.
 * a0 = the probed line, a1 = a far address with the same V$ index as that
 * line but another tag (a load that misses, older than the candidate), a2 =
 * a value, a3 = an address for a store (older than the candidate, not
 * overlapping it, another V$ index).  The store enters the transfers queue
 * and blocks the younger load until it retires; the far load retires first
 * and replaces the entry of the candidate's line.
 */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_ab\nbp_ab:\n\tld t1,0(a1)\n\tsd a2,0(a3)\n"
        ".globl bp_abc\nbp_abc:\n\tld a0,8(a0)\n\tret\n"
        ".size bp_ab, .-bp_ab\n");

extern long bp_ab(const void *p, const void *far, long v, const void *st);

#define N 24

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 64 * N; i += 8)
        *(volatile u64 *)(buf + i) = (u64)i * 0x9e3779b97f4a7c15UL + 7;
    for (int i = 0; i < N; i++)
        t += bp_warm(buf + 64 * i);
    delay();
    for (int i = 0; i < N; i++) {
        t += bp_ab(buf + 64 * i, farbuf + 64 * i + 4096 * (i + 1), i,
                   buf + 4096 + 64 * i + 32);
        delay();
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SPLIT)

/* Loads of 8 bytes at offset 60 of a 64 byte line: the access crosses the
 * cache line, so the LSQ splits it into two requests */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_split\nbp_split:\n\tld a0,60(a0)\n\tret\n"
        ".size bp_split, .-bp_split\n");

extern long bp_split(const void *p);

#define N 24

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 64 * (N + 1); i += 8)
        *(volatile u64 *)(buf + i) = (u64)i * 0x9e3779b97f4a7c15UL + 11;
    for (int i = 0; i < N; i++) {
        t += bp_split(buf + 64 * i);
        delay();
    }
    for (int i = 0; i < N; i++) {
        t += bp_split(buf + 64 * i);
        delay();
    }

    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_FP)

/* Floating-point loads at one PC each: fld and flw, then the same after
 * floating-point stores.  The loaded bits are moved to an integer register
 * (fmv.x.d, fmv.x.w) so that the program can check them */
__asm__(".text\n"
        ".balign 16\n"
        ".globl bp_fldw\nbp_fldw:\n\tfld ft0,0(a0)\n\tfmv.x.d a0,ft0\n"
        "\tret\n.size bp_fldw, .-bp_fldw\n"
        ".balign 16\n"
        ".globl bp_fldp\nbp_fldp:\n\tfld ft0,0(a0)\n\tfmv.x.d a0,ft0\n"
        "\tret\n.size bp_fldp, .-bp_fldp\n"
        ".balign 16\n"
        ".globl bp_flww\nbp_flww:\n\tflw ft0,0(a0)\n\tfmv.x.w a0,ft0\n"
        "\tret\n.size bp_flww, .-bp_flww\n"
        ".balign 16\n"
        ".globl bp_flwp\nbp_flwp:\n\tflw ft0,0(a0)\n\tfmv.x.w a0,ft0\n"
        "\tret\n.size bp_flwp, .-bp_flwp\n"
        ".balign 16\n"
        ".globl bp_fsd\nbp_fsd:\n\tfmv.d.x ft0,a1\n\tfsd ft0,0(a0)\n"
        "\tret\n.size bp_fsd, .-bp_fsd\n"
        ".balign 16\n"
        ".globl bp_fsw\nbp_fsw:\n\tfmv.w.x ft0,a1\n\tfsw ft0,0(a0)\n"
        "\tret\n.size bp_fsw, .-bp_fsw\n");

extern long bp_fldw(const void *p);
extern long bp_fldp(const void *p);
extern long bp_flww(const void *p);
extern long bp_flwp(const void *p);
extern void bp_fsd(void *p, long v);
extern void bp_fsw(void *p, long v);

#define N 24
#define D0(i) (0x3ff0000000000000UL + (u64)(i) * 0x0000100000000001UL)
#define D1(i) (0xc008000000000000UL ^ ((u64)(i) * 0x0000000100010001UL))
/* single precision patterns: ordinary, a quiet NaN, a denormal, negative */
static u32
f0(int i)
{
    return i % 4 == 0 ? 0x7fc00000u + i : i % 4 == 1 ? 0x00000013u + i :
        i % 4 == 2 ? 0xbf800000u + i : 0x40490fdbu + i;
}
static u32
f1(int i)
{
    return 0x3f000000u ^ (u32)(i * 0x00010203u);
}

void
_start(void)
{
    long t = 0, errors = 0;

    for (int i = 0; i < N; i++) {
        *(volatile u64 *)(buf + 8 * i) = D0(i);
        *(volatile u32 *)(buf + 512 + 8 * i) = f0(i);
    }
    for (int i = 0; i < N; i++)
        t += bp_fldw(buf + 8 * i);
    delay();
    for (int i = 0; i < N; i++) {
        long v = bp_fldp(buf + 8 * i);
        errors += (u64)v != D0(i);
        t += v;
        delay();
    }
    for (int i = 0; i < N; i++)
        bp_fsd(buf + 8 * i, (long)D1(i));
    delay();
    for (int i = 0; i < N; i++) {
        long v = bp_fldp(buf + 8 * i);
        errors += (u64)v != D1(i);
        t += v;
        delay();
    }

    for (int i = 0; i < N; i++)
        t += bp_flww(buf + 512 + 8 * i);
    delay();
    for (int i = 0; i < N; i++) {
        long v = bp_flwp(buf + 512 + 8 * i);
        errors += v != (long)(int)f0(i);
        t += v;
        delay();
    }
    for (int i = 0; i < N; i++)
        bp_fsw(buf + 512 + 8 * i, (long)f1(i));
    delay();
    for (int i = 0; i < N; i++) {
        long v = bp_flwp(buf + 512 + 8 * i);
        errors += v != (long)(int)f1(i);
        t += v;
        delay();
    }

    print_num("errors ", errors, 7);
    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SYN)

/*
 * Virtual synonyms: the same file mapped twice with MAP_SHARED at two
 * virtual addresses.  On a real system a store through the second mapping is
 * visible through the first.  A V$ entry loaded through the first mapping
 * and a store through the second is the synonym hazard of the V$ (virtual
 * tags).  The program loads through the first, stores through the second,
 * loads through the first again, and reports how many loads saw the stored
 * value ("shared N"): 0 means the two mappings are independent copies (the
 * SE emulation of shared mappings, see mmapFunc), so no synonym exists.
 * The loads are bypass candidates.
 */
#define SYS_openat 56
#define SYS_ftruncate 46
#define SYS_mmap 222
#define N 24
#define OLD(i) ((u64)(i) * 0x0101010101010101UL + 0x1111UL)
#define NEW(i) ((u64)(i) * 0x0f0f0f0f0f0f0f0fUL + 0x7777UL)

void
_start(void)
{
    long t = 0, shared = 0, same = 0;
    long fd = syscall6(SYS_openat, -100, (long)"syn.dat", 0x242, 0600, 0, 0);

    if (fd < 0 || syscall6(SYS_ftruncate, fd, 4096, 0, 0, 0, 0) != 0)
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    u8 *m1 = (u8 *)syscall6(SYS_mmap, 0, 4096, 3, 1, fd, 0);
    u8 *m2 = (u8 *)syscall6(SYS_mmap, 0, 4096, 3, 1, fd, 0);

    if ((long)m1 < 0 || (long)m2 < 0 || m1 == m2)
        syscall6(SYS_exit_group, 2, 0, 0, 0, 0, 0);

    for (int i = 0; i < N; i++)
        *(volatile u64 *)(m1 + 8 * i) = OLD(i);
    for (int i = 0; i < N; i++)
        t += bp_warm0(m1 + 8 * i);
    delay();
    for (int i = 0; i < N; i++) {      /* candidates: V$ has the bytes */
        t += bp_probe0(m1 + 8 * i);
        delay();
    }
    for (int i = 0; i < N; i++)        /* the store through the other VA */
        *(volatile u64 *)(m2 + 8 * i) = NEW(i);
    for (int i = 0; i < N; i++) {      /* through the first mapping again */
        u64 v = bp_probe0(m1 + 8 * i);
        shared += v == NEW(i);
        same += v == OLD(i);
        t += v;
        delay();
    }
    long back = 0;
    for (int i = 0; i < N; i++)        /* the store did reach the second VA */
        back += *(volatile u64 *)(m2 + 8 * i) == NEW(i);

    print_num("shared ", shared, 7);
    print_num("old ", same, 4);
    print_num("storedvisible ", back, 14);
    print_num("checksum ", t, 9);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_SHM)

/* shmget: the SE syscall table lists it without an implementation */
void
_start(void)
{
    long id = syscall6(194, 0, 4096, 01600, 0, 0, 0);

    print_num("shmget ", id, 7);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(BP_MEMFD)

/* memfd_create: likewise listed without an implementation */
void
_start(void)
{
    long fd = syscall6(279, (long)"x", 0, 0, 0, 0, 0);

    print_num("memfd ", fd, 6);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#else
#error "define one BP_ mode (see the list at the top of the file)"
#endif
