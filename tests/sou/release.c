/*
 * Workload for the Design A (validated scoreboard release) tests of
 * run_release.sh.  Every iteration of every section has the same shape:
 *
 *   M  ld  t0, 0(a7)    an older, independent load that misses to DRAM; it
 *                       holds the head of the in-flight queue for ~100 cycles
 *   [S sd  a2, 8(a0)]   section "sc" only: an older store to the bytes L reads
 *   L  ld  t1, 8(a0)    the probed load
 *   I1, I2              independent addi
 *   D  add t2, t1, t3   depends on L
 *   J  addi t6, t6, 4   independent, after D
 *
 * so L is decided while it is not the head and D is a dependent that, in the
 * stall-on-use configuration, cannot issue until L's destination is released
 * (at L's commit, or earlier when V-ASTRA releases it).  The sections differ
 * in what happens to L:
 *   ca  (candidate)       lines 64 bytes apart, loaded before (V$ has them):
 *                         ASTRA predicts the base, the V$ hits: bypass +
 *                         release
 *   bw  (base wrong)      lines in an order with no stride: ASTRA predicts the
 *                         wrong base; no release
 *   tm  (V$ tag miss)     lines never loaded before, stride 64: base right, V$
 *                         tag miss, falls back; no release
 *   sc  (store conflict)  an older store to L's bytes in flight: rejected at
 *                         the decision; no release
 * The far lines of M are 1088 * k + 16 bytes into a 1 MiB array (k from a
 * full period LCG: no stride for the L1 prefetcher; address = 16 mod 64, so
 * the V$ index of M's line is never one that L uses).  Freestanding.
 */

#define SYS_write 64
#define SYS_exit_group 94

#ifndef N
#define N 16
#endif

typedef unsigned long u64;

static unsigned char buf[16384] __attribute__((aligned(4096)));
static unsigned char farbuf[1 << 20] __attribute__((aligned(4096)));

static long
syscall6(long num, long a0, long a1, long a2)
{
    register long x17 __asm__("a7") = num;
    register long x10 __asm__("a0") = a0;
    register long x11 __asm__("a1") = a1;
    register long x12 __asm__("a2") = a2;

    __asm__ volatile("ecall" : "+r"(x10) : "r"(x17), "r"(x11), "r"(x12)
                     : "memory");
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
    syscall6(SYS_write, 1, (long)"checksum ", 9);
    syscall6(SYS_write, 1, (long)(b + pos), sizeof(b) - pos);
}

static inline void
delay(void)
{
    long n = 400;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

/* The far address of M: a7 = a3 + 1088 * (a4 mod 512) + 16, a4 = 5 a4 + 1 */
#define FAR_ADDR                                                          \
    "\tli t5,5\n\tmul a4,a4,t5\n\taddi a4,a4,1\n\tandi t5,a4,511\n"      \
    "\tli a7,1088\n\tmul t5,t5,a7\n\tadd a7,a3,t5\n\taddi a7,a7,16\n"

/* One section: label prefix P, code that sets a0 at the top of an
 * iteration (ADDR), code after M (PRE), code that advances a0 (NEXT) */
#define SECTION(P, ADDR, PRE, NEXT)                                       \
    ".balign 64\n"                                                        \
    ".globl rel_" P "\nrel_" P ":\n"                                      \
    "\tmv a3,a2\n\tli a2,0\n\tli t3,0\n\tli t4,0\n\tli t6,0\n"         \
    "\tmv a6,a0\n\tli a5,0\n"                                           \
    "\tli a4,0\n"                                                         \
    "1:\n" ADDR FAR_ADDR                                                  \
    ".globl " P "_M\n" P "_M:\n\tld t0,0(a7)\n" PRE                       \
    ".globl " P "_L\n" P "_L:\n\tld t1,8(a0)\n"                           \
    ".globl " P "_I1\n" P "_I1:\n\taddi t3,t3,1\n"                        \
    ".globl " P "_I2\n" P "_I2:\n\taddi t4,t4,2\n"                        \
    ".globl " P "_D\n" P "_D:\n\tadd t2,t1,t3\n"                          \
    ".globl " P "_J\n" P "_J:\n\taddi t6,t6,4\n"                          \
    "\tadd a2,a2,t2\n"                                                    \
    "\tli t0,400\n2:\taddi t0,t0,-1\n\tbnez t0,2b\n"                      \
    NEXT                                                                  \
    "\taddi a1,a1,-1\n\tbnez a1,1b\n"                                     \
    "\tadd a0,a2,t3\n\tadd a0,a0,t4\n\tadd a0,a0,t6\n\tret\n"

#define LINEAR_ADDR ""            /* a0 is kept and advanced by NEXT */
#define LINEAR_NEXT "\taddi a0,a0,64\n"
/* 32 lines in a full period LCG order: a0 = a6 + 64 * (a5 mod 32) */
#define LCG_ADDR                                                          \
    "\tli t5,5\n\tmul a5,a5,t5\n\taddi a5,a5,1\n\tandi t5,a5,31\n"       \
    "\tslli t5,t5,6\n\tadd a0,a6,t5\n"

__asm__(".text\n"
        SECTION("ca", LINEAR_ADDR, "", LINEAR_NEXT)
        SECTION("bw", LCG_ADDR, "", "")
        SECTION("tm", LINEAR_ADDR, "", LINEAR_NEXT)
        SECTION("sc", LINEAR_ADDR, "\t.globl sc_S\nsc_S:\n\tsd a2,8(a0)\n",
                LINEAR_NEXT));

extern long rel_ca(const void *p, long n, const void *far);
extern long rel_bw(const void *p, long n, const void *far);
extern long rel_tm(const void *p, long n, const void *far);
extern long rel_sc(const void *p, long n, const void *far);

/* Region layout in buf: V$ index = (address >> 4) & 255, lines 64 bytes
 * apart use indexes that are multiples of 4, and the sections use disjoint
 * ranges:  ca 0..1023 (idx 0..63), sc 1024..2047, bw 2048..4095,
 * tm 8192.. (never loaded before its section; idx of ca, not equal tag) */
#define CA 0
#define SC 1024
#define BW 2048
#define TM 8192

static void
warm(unsigned char *base, int lines)
{
    for (int i = 0; i < lines; i++) {
        volatile u64 *p = (volatile u64 *)(base + 64 * i);
        (void)p[0];
        (void)p[1];
    }
}

void
_start(void)
{
    long t = 0;

    for (int i = 0; i < 16384; i += 8)
        *(volatile u64 *)(buf + i) = (u64)i * 0x9e3779b97f4a7c15UL + 9;

    warm(buf + CA, N);
    warm(buf + SC, N);
    warm(buf + BW, 32);
    delay();

    t += rel_ca(buf + CA, N, farbuf);
    t += rel_bw(buf + BW, 2 * N, farbuf);
    t += rel_tm(buf + TM, N, farbuf);
    t += rel_sc(buf + SC, N, farbuf);

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}
