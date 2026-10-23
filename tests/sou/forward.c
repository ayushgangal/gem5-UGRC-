/*
 * Workload for the Design B (V-ASTRA forwarding to dependent loads) tests of
 * run_forward.sh.  Every iteration of every section has the same shape:
 *
 *   M  ld t0, 0(a7)     an older, independent load that misses to DRAM and
 *                       holds the head of the in-flight queue for ~100 cycles
 *   [S sd ...]          sections "sc", "ab" and "pd": an older store
 *   P  ld t1, 0(a0)     the producer: loads a pointer
 *   C  ld t2, 0(t1)     a dependent load: its base is P's result
 *   E  ld t3, 0(t2)     a second link (sections with a chain)
 *   U  add t4, t3, a2   the final use
 *
 * Sections (what happens to P decides whether C can be forwarded):
 *   ch  chain       lines 64 bytes apart that were loaded before (the V$ has
 *                   them): P, C and E are V-ASTRA candidates; P's value is
 *                   published and C is forwarded, then E from C
 *   bw  base wrong  the lines of "ch" in an order with no stride: ASTRA
 *                   predicts the wrong base for P; nothing is published
 *   tm  tag miss    P's lines were never loaded: V$ tag miss, falls back
 *   sc  conflict    an older store to P's bytes: rejected at the decision
 *   ab  abandon     an older load aliasing P's V$ set retires between P's
 *                   decision and its send, and a store holds the send: P is
 *                   pending, then abandoned
 *   pd  pending     a non-overlapping older store holds P's send (P pending
 *                   behind M), then P succeeds
 *   sq  squash      the chain behind a branch that is guessed wrongly about
 *                   half of the time: wrong-path P and C are decided and
 *                   forwarded and then squashed
 *   ft  fault       P loads from fresh pages and takes a translation fault
 *                   (its request goes to memory without the bypass hook, so
 *                   its record stays pending); C is blocked while P is
 *                   pending; the fault squashes C
 *   cm  memory      P's pointers point to cold lines (never written or loaded
 *                   before: not in any cache; alternately 128 and 384
 *                   bytes apart (SS indexes of their own): a pattern ASTRA's
 *                   stride sequences learn but the L1 stride prefetcher
 *                   cannot use): C is not a candidate (V$ tag miss) and
 *                   misses to DRAM: forwarding lets that
 *                   request start before P commits (the measurable case)
 * The far lines of M: 1088 * k + 16 bytes into a 128 KiB block of a 2 MiB
 * array, one block per section (k from a full period LCG of 64: distinct
 * cold lines, no stride; address = 16 mod 64 so the V$ index of M's line is
 * never one that P, C, E use), except in "ab" where M's line has P's V$ index.
 * Freestanding: _start and Linux system calls only.
 */

#define SYS_write 64
#define SYS_exit_group 94
#define SYS_mmap 222

#ifndef N
#define N 16
#endif

typedef unsigned long u64;

static unsigned char buf[16384] __attribute__((aligned(4096)));
static unsigned char farbuf[1 << 21] __attribute__((aligned(4096)));

static long
syscall6(long num, long a0, long a1, long a2, long a3, long a4, long a5)
{
    register long x17 __asm__("a7") = num;
    register long x10 __asm__("a0") = a0;
    register long x11 __asm__("a1") = a1;
    register long x12 __asm__("a2") = a2;
    register long x13 __asm__("a3") = a3;
    register long x14 __asm__("a4") = a4;
    register long x15 __asm__("a5") = a5;

    __asm__ volatile("ecall" : "+r"(x10)
                     : "r"(x17), "r"(x11), "r"(x12), "r"(x13), "r"(x14),
                       "r"(x15) : "memory");
    return x10;
}

static void
print_checksum(long value)
{
    char b[40];
    int pos = sizeof(b);
    unsigned long v = (unsigned long)value;

    b[--pos] = '\n';
    do {
        b[--pos] = '0' + v % 10;
        v /= 10;
    } while (v != 0);
    syscall6(SYS_write, 1, (long)"checksum ", 9, 0, 0, 0);
    syscall6(SYS_write, 1, (long)(b + pos), sizeof(b) - pos, 0, 0, 0);
}

static inline void
delay(void)
{
    long n = 400;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

/* M's address: a7 = a3 + 1088 * (a4 mod 64) + 16, a4 = 5 a4 + 1 (full
 * period 64); each section has a 128 KiB block of the far array of its own,
 * so that its lines are cold */
#define FAR_LCG                                                           \
    "\tli t5,5\n\tmul a4,a4,t5\n\taddi a4,a4,1\n\tandi t5,a4,63\n"       \
    "\tli a7,1088\n\tmul t5,t5,a7\n\tadd a7,a3,t5\n\taddi a7,a7,16\n"
/* in "ab": the same V$ index as P's line (a0): a3 + (a0 mod 4096) +
 * 4096 * (a5 + 1) */
#define FAR_ALIAS                                                         \
    "\tandi t5,a0,2047\n\taddi a5,a5,1\n\tslli a7,a5,12\n\tadd a7,a7,t5\n" \
    "\tadd a7,a7,a3\n"

#define BODY_CHAIN(P)                                                     \
    ".globl " P "_P\n" P "_P:\n\tld t1,0(a0)\n"                           \
    ".globl " P "_C\n" P "_C:\n\tld t2,0(t1)\n"                           \
    ".globl " P "_E\n" P "_E:\n\tld t3,0(t2)\n"                           \
    ".globl " P "_U\n" P "_U:\n\tadd t4,t3,a2\n"
/* "ft": the dependent's offset is 1024 (P loads zero from a fresh page) */
#define BODY_FT(P)                                                        \
    ".globl " P "_P\n" P "_P:\n\tld t1,0(a0)\n"                           \
    ".globl " P "_C\n" P "_C:\n\tld t2,1024(t1)\n"                        \
    ".globl " P "_U\n" P "_U:\n\tadd t4,t2,a2\n"
/* "sq": the body is skipped when bit 3 of the LCG state is zero.  The branch
 * resolves at commit, after M, so a wrong guess executes P, C, E on the wrong
 * path for ~100 cycles: a forwarded C on the wrong path is squashed */
#define PRE_SQ "\tandi t5,a4,8\n\tbeqz t5,3f\n"
#define BODY_SQ(P) BODY_CHAIN(P) "3:\n"
#define BODY_TWO(P)                                                       \
    ".globl " P "_P\n" P "_P:\n\tld t1,0(a0)\n"                           \
    ".globl " P "_C\n" P "_C:\n\tld t2,0(t1)\n"                           \
    ".globl " P "_U\n" P "_U:\n\tadd t4,t2,a2\n"

/* ADDR sets a0 at the top of an iteration, FAR sets a7, PRE is code after M,
 * NEXT advances a0 */
#define SECTION(P, ADDR, PREM, FAR, PRE, BODY, NEXT)                       \
    ".balign 64\n"                                                        \
    ".globl fw_" P "\nfw_" P ":\n"                                        \
    "\tmv a3,a2\n\tli a2,0\n\tmv a6,a0\n\tli a5,0\n\tli a4,0\n"           \
    "1:\n" ADDR PREM FAR                                                  \
    ".globl " P "_M\n" P "_M:\n\tld t0,0(a7)\n" PRE BODY                  \
    "\tadd a2,a2,t4\n"                                                    \
    "\tli t0,400\n2:\taddi t0,t0,-1\n\tbnez t0,2b\n"                      \
    NEXT                                                                  \
    "\taddi a1,a1,-1\n\tbnez a1,1b\n"                                     \
    "\tmv a0,a2\n\tret\n"

#define FT_ADDR ""
/* 4288 = 4096 + 192: every access is in a new page, and the stride's low 10
 * bits (the SS index, not shared with the strides of the other sections)
 * are not zero */
#define FT_NEXT "\tli t5,4288\n\tadd a0,a0,t5\n"
#define LINEAR_ADDR ""
#define LINEAR_NEXT "\taddi a0,a0,64\n"
/* 16 lines of a6 in a full period LCG order: a0 = a6 + 64 * (a5 mod 16) */
#define LCG_ADDR                                                          \
    "\tli t5,5\n\tmul a5,a5,t5\n\taddi a5,a5,1\n\tandi t5,a5,15\n"       \
    "\tslli t5,t5,6\n\tadd a0,a6,t5\n"
/* The store's address and data registers are computed BEFORE M: M holds the
 * head, and an instruction that depends on one computed after it could not
 * commit, so the store could not be sent to the LSQ early (it would sit at
 * the end of the memory FU and block it) */
#define PREM_SC "\taddi t6,a0,1024\n"
#define PREM_FAR "\tli t6,12288\n\tadd t6,a0,t6\n"
#define STORE_SC "\t.globl sc_S\nsc_S:\n\tsd t6,0(a0)\n"
#define STORE_AB "\t.globl ab_S\nab_S:\n\tsd a2,0(t6)\n"
#define STORE_PD "\t.globl pd_S\npd_S:\n\tsd a2,0(t6)\n"

__asm__(".text\n"
        SECTION("ch", LINEAR_ADDR, "", FAR_LCG, "", BODY_CHAIN("ch"),
                LINEAR_NEXT)
        SECTION("bw", LCG_ADDR, "", FAR_LCG, "", BODY_CHAIN("bw"), "")
        SECTION("tm", LINEAR_ADDR, "", FAR_LCG, "", BODY_CHAIN("tm"),
                LINEAR_NEXT)
        SECTION("sc", LINEAR_ADDR, PREM_SC, FAR_LCG, STORE_SC,
                BODY_CHAIN("sc"), LINEAR_NEXT)
        SECTION("ab", LINEAR_ADDR, PREM_FAR, FAR_ALIAS, STORE_AB,
                BODY_CHAIN("ab"), LINEAR_NEXT)
        SECTION("pd", LINEAR_ADDR, PREM_FAR, FAR_LCG, STORE_PD,
                BODY_CHAIN("pd"), LINEAR_NEXT)
        SECTION("sq", LINEAR_ADDR, "", FAR_LCG, PRE_SQ, BODY_SQ("sq"),
                LINEAR_NEXT)
        SECTION("ft", FT_ADDR, "", FAR_LCG, "", BODY_FT("ft"), FT_NEXT)
        SECTION("cm", LINEAR_ADDR, "", FAR_LCG, "", BODY_TWO("cm"),
                LINEAR_NEXT));

extern long fw_ch(const void *p, long n, const void *far);
extern long fw_bw(const void *p, long n, const void *far);
extern long fw_tm(const void *p, long n, const void *far);
extern long fw_sc(const void *p, long n, const void *far);
extern long fw_ab(const void *p, long n, const void *far);
extern long fw_pd(const void *p, long n, const void *far);
extern long fw_cm(const void *p, long n, const void *far);
extern long fw_ft(const void *p, long n, const void *far);
extern long fw_sq(const void *p, long n, const void *far);

/* Layout of buf (V$ index = (address >> 4) & 255; lines 64 bytes apart use
 * indexes that are multiples of 4):
 *   L1 0, L2 1024, L3 2048        the chain: L1[i] -> L2[i] -> L3[i]
 *   L1m 3072                      cm: L1m[i] -> cold line
 *   L1t 4096                      tm: L1t[i] -> L2[i]; never loaded before
 *                                 its section (same V$ index as L1, other tag)
 *   cold 8192                     cm: the cold lines
 *   scratch 12288                 targets of the stores of sc, ab and pd */
#define L1 0
#define L2 1024
#define L3 2048
#define L1M 3072
#define L1T 4096
#define COLD 8192
#define FT_BASE 0x20000000UL

static void
warm(int off, int lines)
{
    for (int i = 0; i < lines; i++)
        (void)*(volatile u64 *)(buf + off + 64 * i);
}

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 16; i++) {
        *(volatile u64 *)(buf + L1 + 64 * i) = (u64)(buf + L2 + 64 * i);
        *(volatile u64 *)(buf + L2 + 64 * i) = (u64)(buf + L3 + 64 * i);
        *(volatile u64 *)(buf + L3 + 64 * i) = 0x1111UL * (i + 1);
        *(volatile u64 *)(buf + L1M + 64 * i) =
            (u64)(buf + COLD + 512 * (i / 2) + 128 * (i & 1));
        *(volatile u64 *)(buf + L1T + 64 * i) = (u64)(buf + L2 + 64 * i);
    }

    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    delay();
    t += fw_ch(buf + L1, N, farbuf + 0);
    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    t += fw_bw(buf + L1, 2 * N, farbuf + 131072);
    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    t += fw_tm(buf + L1T, N, farbuf + 262144);
    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    t += fw_sc(buf + L1, N, farbuf + 393216);
    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    t += fw_ab(buf + L1, N, farbuf + 524288);
    warm(L1, 16); warm(L2, 16); warm(L3, 16);
    t += fw_pd(buf + L1, N, farbuf + 655360);
    warm(L1M, 16);
    t += fw_cm(buf + L1M, N, farbuf + 786432);
    for (int k = 0; k < 3; k++) {
        warm(L1, 16); warm(L2, 16); warm(L3, 16);
        t += fw_sq(buf + L1, N, farbuf + (8 + k) * 131072);
    }

    /* ft: P loads from pages that were never touched: its first execution
     * takes a translation fault, resolved by the SE page fault handler, and
     * P is executed again; it reads zero, so C reads 1024 bytes into the page
     * mapped at address 0 */
    if (syscall6(SYS_mmap, 0, 4096, 3, 0x32, -1, 0) != 0 ||
        syscall6(SYS_mmap, FT_BASE, 24 * 4096, 3, 0x32, -1, 0) != FT_BASE)
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    t += fw_ft((void *)FT_BASE, N, farbuf + 917504);

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}
