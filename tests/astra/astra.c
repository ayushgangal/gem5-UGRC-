/*
 * Workload for the ASTRA shadow predictor (src/cpu/minor/astra.hh).
 *
 * Seven sections, each in its own function so that its loads have their own
 * static PCs, run one after the other on two 128 KiB buffers.  Every load is
 * a volatile 8 byte load, so every iteration executes it, and the loop
 * bodies have no other loads.  N iterations each.  Offsets are from the
 * start of the buffer and "stride" is the change of the load's base address
 * from one iteration to the next.
 *
 *  const  one load, stride 0                       (constant base address)
 *  pos    one load, stride +64
 *  neg    one load, stride -64, walking down
 *  chg    one load, stride 64 for 8 iterations, then 192 for 8, and so on
 *  imm    two loads from one base p with immediates +24 and -16 (the
 *         base address is the register, not the effective address), stride
 *         +64: shows that the base is recovered with a sign extended
 *         immediate
 *  pair   two loads (two PCs) with the same stride, 64 for 24 iterations and
 *         then 192: the stride changes for both at once, so with the
 *         untagged SS table the second PC can benefit from the update made
 *         by the first (constructive aliasing)
 *  conf   two loads with strides 64 and 1088: with the default 1024 entry
 *         tables the stride indexes of these are both 64, so they overwrite
 *         each other's SS entry (destructive aliasing)
 *
 * The checksum is the sum of the loaded values.  The test checks it, the
 * load PCs and the load addresses against an independent model
 * (tests/astra/check_astra.py), and the assembly (check_asm.py): the
 * compiler must emit exactly the expected loads and no other load.
 *
 * Freestanding, like tests/lvp/ldld.c: it starts at _start and uses Linux
 * system calls directly (the cross toolchain has no C library).
 */

#define SYS_write 64
#define SYS_exit_group 94

#define N 64
#define BUF_BYTES 131072
#define BUF_LONGS (BUF_BYTES / 8)
#define STRIDE 64
#define STRIDE_BIG 192
#define CHG_RUN 8
#define PAIR_SWITCH 24
#define CONFLICT_Y_STRIDE 1088 /* 64 + 1024 */

typedef const volatile long vlong;

static long bufA[BUF_LONGS];
static long bufB[BUF_LONGS];

static long
syscall3(long num, long arg0, long arg1, long arg2)
{
    register long a7 __asm__("a7") = num;
    register long a0 __asm__("a0") = arg0;
    register long a1 __asm__("a1") = arg1;
    register long a2 __asm__("a2") = arg2;

    __asm__ volatile("ecall"
                     : "+r"(a0)
                     : "r"(a7), "r"(a1), "r"(a2)
                     : "memory");
    return a0;
}

__attribute__((noinline)) long
sec_const(const char *p, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++)
        s += *(vlong *)p;
    return s;
}

__attribute__((noinline)) long
sec_pos(const char *p, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++) {
        s += *(vlong *)p;
        p += STRIDE;
    }
    return s;
}

__attribute__((noinline)) long
sec_neg(const char *p, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++) {
        s += *(vlong *)p;
        p -= STRIDE;
    }
    return s;
}

__attribute__((noinline)) long
sec_chg(const char *p, long n)
{
    long s = 0;
    long step = STRIDE;
    for (long i = 0; i < n; i++) {
        s += *(vlong *)p;
        p += step;
        if ((i + 1) % CHG_RUN == 0)
            step = (step == STRIDE) ? STRIDE_BIG : STRIDE;
    }
    return s;
}

__attribute__((noinline)) long
sec_imm(const char *p, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++) {
        s += *(vlong *)(p + 24);
        s += *(vlong *)(p - 16);
        p += STRIDE;
    }
    return s;
}

__attribute__((noinline)) long
sec_pair(const char *a, const char *b, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++) {
        long step = (i < PAIR_SWITCH) ? STRIDE : STRIDE_BIG;
        s += *(vlong *)a;
        s += *(vlong *)b;
        a += step;
        b += step;
    }
    return s;
}

__attribute__((noinline)) long
sec_conf(const char *x, const char *y, long n)
{
    long s = 0;
    for (long i = 0; i < n; i++) {
        s += *(vlong *)x;
        s += *(vlong *)y;
        x += STRIDE;
        y += CONFLICT_Y_STRIDE;
    }
    return s;
}

static void
print_checksum(long value)
{
    char buf[40];
    int pos = sizeof(buf);

    buf[--pos] = '\n';
    do {
        buf[--pos] = '0' + value % 10;
        value /= 10;
    } while (value != 0);

    /* "checksum " followed by the number */
    syscall3(SYS_write, 1, (long)"checksum ", 9);
    syscall3(SYS_write, 1, (long)&buf[pos], sizeof(buf) - pos);
}

void
_start(void)
{
    /* Stores only: the value at long index k is 7k+3 in bufA and 11k+5 in
     * bufB */
    for (long i = 0; i < BUF_LONGS; i++) {
        bufA[i] = i * 7 + 3;
        bufB[i] = i * 11 + 5;
    }

    const char *a = (const char *)bufA;
    const char *b = (const char *)bufB;
    long total = 0;

    total += sec_const(a + 4096, N);
    total += sec_pos(a, N);
    total += sec_neg(a + STRIDE * (N - 1), N);
    total += sec_chg(a, N);
    total += sec_imm(a + 64, N);
    total += sec_pair(a, b, N);
    total += sec_conf(a, b, N);

    print_checksum(total);

    syscall3(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}
