/*
 * Workload for the stall-on-use baseline characterization (run_sou.sh).
 * No predictor is involved; the program has long-latency loads and their
 * dependents.  Each section runs N iterations, with a register-only delay
 * between iterations so that they do not overlap.
 *
 *  Section A (one miss):    L   ld  t1, 0(a0)   a cold line: misses to DRAM
 *                           I1..I3  three independent addi
 *                           D   add t2, t1, t3  depends on L
 *                           J   addi t6, t6, 4  independent, after D
 *  Section B (older miss):  M   ld  t0, 0(a3)   an older independent miss
 *                           (a cold line, 256 KiB above L's)
 *                           then the same L (the lines of section A, so L
 *                           hits in the L1), I1..I3, D, J.  M holds the head
 *                           of the in-flight queue for a DRAM access, so L's
 *                           data arrives long before L can commit.
 *
 * The lines are 1088 * (k mod 512) bytes from the base, k following a
 * full-period LCG: distinct lines with no stride for the L1 prefetcher.
 * They are never written; the loads read zeroes.  Freestanding: _start and
 * Linux system calls only.
 */

#define SYS_write 64
#define SYS_exit_group 94

#ifndef N
#define N 12
#endif

typedef unsigned long u64;

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
    char buf[40];
    int pos = sizeof(buf);
    unsigned long v = (unsigned long)value;

    buf[--pos] = '\n';
    do {
        buf[--pos] = '0' + v % 10;
        v /= 10;
    } while (v != 0);
    syscall6(SYS_write, 1, (long)"checksum ", 9);
    syscall6(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos);
}

/*
 * long sou_a(const void *far, long n): section A.  Returns a checksum.
 * long sou_b(const void *far, long n): section B.
 * a0 = far address (advances by 1088 each iteration), a1 = iterations.
 * Every instruction of the body has a global label (soa_L, soa_I1, ...).
 */
__asm__(".text\n"
        ".balign 64\n"
        ".globl sou_a\nsou_a:\n"
        "\tli a2,0\n\tli t3,0\n\tli t4,0\n\tli t5,0\n\tli t6,0\n"
        "\tmv a6,a0\n\tli a5,0\n"
        "1:\n"
        /* a0 = a6 + 1088 * (a5 mod 512), a5 = 5 * a5 + 1: a full period LCG,
         * so the lines are distinct and follow no stride */
        "\tli a7,5\n\tmul a5,a5,a7\n\taddi a5,a5,1\n\tandi a7,a5,511\n"
        "\tli a4,1088\n\tmul a7,a7,a4\n\tadd a0,a6,a7\n"
        ".globl soa_L\nsoa_L:\n\tld t1,0(a0)\n"
        ".globl soa_I1\nsoa_I1:\n\taddi t3,t3,1\n"
        ".globl soa_I2\nsoa_I2:\n\taddi t4,t4,2\n"
        ".globl soa_I3\nsoa_I3:\n\taddi t5,t5,3\n"
        ".globl soa_D\nsoa_D:\n\tadd t2,t1,t3\n"
        ".globl soa_J\nsoa_J:\n\taddi t6,t6,4\n"
        "\tadd a2,a2,t2\n"
        "\tli t0,400\n"
        "2:\taddi t0,t0,-1\n\tbnez t0,2b\n"
        "\taddi a1,a1,-1\n"
        "\tbnez a1,1b\n"
        "\tadd a0,a2,t3\n\tadd a0,a0,t4\n\tadd a0,a0,t5\n\tadd a0,a0,t6\n"
        "\tret\n"
        ".size sou_a, .-sou_a\n"
        ".balign 64\n"
        ".globl sou_b\nsou_b:\n"
        "\tli a2,0\n\tli t3,0\n\tli t4,0\n\tli t5,0\n\tli t6,0\n"
        "\tmv a6,a0\n\tli a5,0\n"
        "1:\n"
        "\tli a7,5\n\tmul a5,a5,a7\n\taddi a5,a5,1\n\tandi a7,a5,511\n"
        "\tli a4,1088\n\tmul a7,a7,a4\n\tadd a0,a6,a7\n"
        "\tlui a4,0x40\n\tadd a3,a0,a4\n"
        ".globl sob_M\nsob_M:\n\tld t0,0(a3)\n"
        ".globl sob_L\nsob_L:\n\tld t1,0(a0)\n"
        ".globl sob_I1\nsob_I1:\n\taddi t3,t3,1\n"
        ".globl sob_I2\nsob_I2:\n\taddi t4,t4,2\n"
        ".globl sob_I3\nsob_I3:\n\taddi t5,t5,3\n"
        ".globl sob_D\nsob_D:\n\tadd t2,t1,t3\n"
        ".globl sob_J\nsob_J:\n\taddi t6,t6,4\n"
        "\tadd a2,a2,t2\n"
        "\tli t0,400\n"
        "2:\taddi t0,t0,-1\n\tbnez t0,2b\n"
        "\taddi a1,a1,-1\n"
        "\tbnez a1,1b\n"
        "\tadd a0,a2,t3\n\tadd a0,a0,t4\n\tadd a0,a0,t5\n\tadd a0,a0,t6\n"
        "\tret\n"
        ".size sou_b, .-sou_b\n");

extern long sou_a(const void *far, long n);
extern long sou_b(const void *far, long n);

void
_start(void)
{
    long t = 0;

    t += sou_a(farbuf, N);
    t += sou_b(farbuf, N);
    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}
