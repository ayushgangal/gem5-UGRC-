/*
 * Workloads for the diagnostic V-ASTRA probe tests of run_vastra.sh (see
 * tests/astra/README.md, "Diagnostic V-ASTRA probe").  ASTRA, the shadow V$
 * and the probe are on in every run.
 *
 * Every probed load is a single `ld` in assembly with a base register and an
 * immediate, at its own PC, called once per iteration with delays in between
 * (register-only loops) so that each load retires, and so trains ASTRA and
 * the V$, before the next one is looked up.  A section walks lines of a
 * region (vbuf + 4096 * section) with a constant stride of 64 bytes unless
 * it says otherwise.
 *
 *  VA_CLASSIFY  five sections, 24 iterations each:
 *    hit        the 16 bytes of every line were loaded first (the V$ has
 *               them); the probed load reads the 8 bytes at +8:  a correct
 *               base predicted gives a byte-valid hit with the right bytes
 *    basewrong  32 lines loaded first, then probed in an order whose stride
 *               changes every time: ASTRA predicts the wrong base although
 *               the V$ holds valid bytes (for the actual address, and
 *               often for the wrong one with other data)
 *    tagmiss    no line was loaded first: a correct base finds no tag
 *    bytemiss   only the 8 bytes at +0 of each line were loaded first; the
 *               probed load reads +8: the tag matches, the bytes are invalid
 *    cross      the lines were loaded first; the probed load reads 8 bytes
 *               at +12, which crosses a 16 byte boundary: not supported by
 *               the V$ model
 *  VA_STORE     each iteration is a load that misses (so nothing retires for
 *               a while), a store, and the probed load, in one function: the
 *               store is still in flight when the load is decided.  In the
 *               "conflict" section it writes the bytes the load reads, in the
 *               "control" section the next line.
 *  VA_DISCARD   a loop that skips a probed load once, on a branch that is
 *               predicted wrongly: the load is fetched, issued and decided
 *               on the wrong path with a correct prediction, and discarded.
 *  VA_FAULT     the probed load walks never-touched mmap'd pages with a
 *               stride of 4096: after two iterations ASTRA predicts the base
 *               and the first execution of each load takes a translation
 *               fault (resolved by the SE page fault handler; the load is
 *               executed again).
 *
 * Freestanding like astra.c: _start and Linux system calls only; the data
 * are written by stores; no initialised data.
 */

#define SYS_write 64
#define SYS_exit_group 94
#define SYS_mmap 222

#ifndef DELAY
#define DELAY 300
#endif

typedef unsigned char u8;
typedef unsigned long u64;

#define REGION(n) (vbuf + 4096UL * (n))
static u8 vbuf[8 * 4096] __attribute__((aligned(4096)));
#define FAR_BYTES (1UL << 20)
static u8 farbuf[FAR_BYTES] __attribute__((aligned(4096)));

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
    unsigned long v = (unsigned long)value;

    buf[--pos] = '\n';
    do {
        buf[--pos] = '0' + v % 10;
        v /= 10;
    } while (v != 0);

    syscall6(SYS_write, 1, (long)"checksum ", 9, 0, 0, 0);
    syscall6(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos, 0, 0, 0);
}

/* Register-only: lets everything before it retire and train the tables
 * before the next load is issued and looked up */
static inline void
delay(void)
{
    long n = DELAY;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

/* The data of the buffers are written by stores (with their addresses) so
 * that every load returns something that identifies where it came from */
static void
fill(u8 *p, unsigned long bytes)
{
    for (unsigned long i = 0; i < bytes; i += 8)
        *(volatile u64 *)(p + i) = (u64)(p + i) * 0x9e3779b97f4a7c15UL + i;
}

#if defined(VA_CLASSIFY)

/*
 * va_warm loads the 16 bytes of a line, va_warm8 the first 8.  The probed
 * loads: va_hit, va_bw, va_tm, va_bm read 8 bytes at +8; va_x reads 8 bytes
 * at +12.  a0 is the base address.
 */
__asm__(".text\n"
        ".balign 64\n"
        ".globl va_warm\nva_warm:\n\tld a1,0(a0)\n\tld a2,8(a0)\n\tret\n"
        ".size va_warm, .-va_warm\n"
        ".balign 16\n"
        ".globl va_warm8\nva_warm8:\n\tld a1,0(a0)\n\tret\n"
        ".size va_warm8, .-va_warm8\n"
        ".balign 16\n"
        ".globl va_hit\nva_hit:\n\tld a0,8(a0)\n\tret\n"
        ".size va_hit, .-va_hit\n"
        ".balign 16\n"
        ".globl va_bw\nva_bw:\n\tld a0,8(a0)\n\tret\n"
        ".size va_bw, .-va_bw\n"
        ".balign 16\n"
        ".globl va_tm\nva_tm:\n\tld a0,8(a0)\n\tret\n"
        ".size va_tm, .-va_tm\n"
        ".balign 16\n"
        ".globl va_bm\nva_bm:\n\tld a0,8(a0)\n\tret\n"
        ".size va_bm, .-va_bm\n"
        ".balign 16\n"
        ".globl va_x\nva_x:\n\tld a0,12(a0)\n\tret\n"
        ".size va_x, .-va_x\n");

extern long va_warm(const void *p);
extern long va_warm8(const void *p);
extern long va_hit(const void *p);
extern long va_bw(const void *p);
extern long va_tm(const void *p);
extern long va_bm(const void *p);
extern long va_x(const void *p);

#define N_ITER 24
#define N_BW 14

void
_start(void)
{
    long t = 0;

    fill(REGION(0), 32 * 64);
    fill(REGION(1), 32 * 64);
    fill(REGION(2), 32 * 64);
    fill(REGION(3), 32 * 64);
    fill(REGION(4), 32 * 64);
    fill(REGION(5), 32 * 64);

    /* hit: loaded first, then probed, constant stride 64 */
    for (int i = 0; i < N_ITER; i++)
        t += va_warm(REGION(0) + 64 * i);
    delay();
    for (int i = 0; i < N_ITER; i++) {
        t += va_hit(REGION(0) + 64 * i);
        delay();
    }

    /* basewrong: the line number changes by 2i + 4 each time */
    for (int k = 0; k < 32; k++)
        t += va_warm(REGION(1) + 64 * k);
    delay();
    for (int i = 0; i < N_BW; i++) {
        t += va_bw(REGION(1) + 64 * ((i * i + 3 * i) & 31));
        delay();
    }

    /* tagmiss: nothing loaded first */
    for (int i = 0; i < N_ITER; i++) {
        t += va_tm(REGION(2) + 64 * i);
        delay();
    }

    /* bytemiss: only bytes 0..7 of each line loaded first */
    for (int i = 0; i < N_ITER; i++)
        t += va_warm8(REGION(3) + 64 * i);
    delay();
    for (int i = 0; i < N_ITER; i++) {
        t += va_bm(REGION(3) + 64 * i);
        delay();
    }

    /* cross: bytes 12..19 cross the 16 byte boundary */
    for (int i = 0; i < N_ITER; i++)
        t += va_warm(REGION(4) + 64 * i);
    delay();
    for (int i = 0; i < N_ITER; i++) {
        t += va_x(REGION(4) + 64 * i);
        delay();
    }

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(VA_STORE)

/*
 * a0 = base of the probed line, a1 = a far address (a load that misses keeps
 * everything younger from retiring), a2 = the value to store.  The store
 * address is the base plus 8 (the bytes the probed load reads) in va_conf and
 * the base plus 24 (the next line) in va_ctrl.
 */
__asm__(".text\n"
        ".balign 64\n"
        ".globl va_warm\nva_warm:\n\tld a3,0(a0)\n\tld a4,8(a0)\n\tret\n"
        ".size va_warm, .-va_warm\n"
        ".balign 16\n"
        ".globl va_conf\nva_conf:\n"
        "\tld t1,0(a1)\n"
        "\tsd a2,8(a0)\n"
        ".globl va_st\nva_st:\n\tld a0,8(a0)\n"
        "\tret\n"
        ".size va_conf, .-va_conf\n"
        ".balign 16\n"
        ".globl va_ctrl\nva_ctrl:\n"
        "\tld t1,0(a1)\n"
        "\tsd a2,24(a0)\n"
        ".globl va_ct\nva_ct:\n\tld a0,8(a0)\n"
        "\tret\n"
        ".size va_ctrl, .-va_ctrl\n");

extern long va_warm(const void *p);
extern long va_conf(const void *p, const void *far, long v);
extern long va_ctrl(const void *p, const void *far, long v);

#define N_ITER 16

void
_start(void)
{
    long t = 0;

    fill(REGION(0), 32 * 64);
    fill(REGION(1), 32 * 64);

    /* The control lines are 32 bytes further on: a different V$ index from
     * the conflict lines (index = (address >> 4) & 255) */
    for (int i = 0; i < N_ITER + 1; i++)
        t += va_warm(REGION(0) + 64 * i);
    for (int i = 0; i < N_ITER + 1; i++)
        t += va_warm(REGION(1) + 32 + 64 * i);
    delay();

    /* conflict: the store writes the bytes the load reads */
    for (int i = 0; i < N_ITER; i++) {
        t += va_conf(REGION(0) + 64 * i, farbuf + 64 * i, i + 1);
        delay();
    }
    /* control: the store writes the next line */
    for (int i = 0; i < N_ITER; i++) {
        t += va_ctrl(REGION(1) + 32 + 64 * i, farbuf + 64 * (N_ITER + i),
                     i + 1);
        delay();
    }

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(VA_DISCARD)

/*
 * a0 = the address (the same every time, so ASTRA predicts it once trained
 * and the V$ holds its bytes), a1 = iterations.  The branch is taken once, at
 * i == 15, and is then predicted wrongly (not taken): its fall-through, the
 * probed load va_disc, is looked up and decided on the wrong path and
 * discarded.  The condition is the end of a chain of multiplies so the branch
 * resolves late.
 */
__asm__(".text\n"
        ".balign 64\n"
        ".globl va_run\nva_run:\n"
        "\tli t4,0\n"
        "1:\n"
        "\taddi t0,t4,-15\n"
        "\tmul t0,t0,t0\n\tmul t0,t0,t0\n\tmul t0,t0,t0\n"
        "\tbeqz t0,2f\n"
        ".globl va_disc\nva_disc:\n\tld t1,8(a0)\n"
        "2:\n"
        "\tli t2,300\n"
        "3:\taddi t2,t2,-1\n\tbnez t2,3b\n"
        "\taddi t4,t4,1\n"
        "\tblt t4,a1,1b\n"
        "\tret\n"
        ".size va_run, .-va_run\n");

extern long va_run(const void *p, long n);

void
_start(void)
{
    long t;

    fill(REGION(0), 4 * 64);
    t = va_run(REGION(0), 24);

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(VA_FAULT)

#define FAULT_ADDR 0x20000000UL
#define FAULT_PAGE 4096
#define FAULT_PAGES 8
#define FAULT_PROT 3
#define FAULT_MAP_FLAGS 0x32 /* MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED */

__asm__(".text\n"
        ".balign 64\n"
        ".globl va_f\nva_f:\n\tld a0,0(a0)\n\tret\n"
        ".size va_f, .-va_f\n");

extern long va_f(const void *p);

void
_start(void)
{
    long t = 0;

    if (syscall6(SYS_mmap, FAULT_ADDR, FAULT_PAGES * FAULT_PAGE, FAULT_PROT,
                 FAULT_MAP_FLAGS, -1, 0) != (long)FAULT_ADDR) {
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    }

    /* Never touched: the first load from each page takes a translation
     * fault, and is executed again */
    for (int i = 0; i < FAULT_PAGES; i++) {
        t += va_f((const void *)(FAULT_ADDR + FAULT_PAGE * i));
        delay();
    }

    print_checksum(t);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#else
#error "define VA_CLASSIFY, VA_STORE, VA_DISCARD or VA_FAULT"
#endif
