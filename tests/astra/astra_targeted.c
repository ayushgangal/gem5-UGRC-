/*
 * Workloads for the targeted ASTRA tests of run_astra_targeted.sh (see
 * tests/astra/README.md).  One program per -D selector:
 *
 *  TG_COLD    First-use behaviour.  cold_a is the first load of the program,
 *             then cold_b (another PC, another LAT entry) runs for the first
 *             time, then each runs once more.  Delays between the loads let
 *             each retire (and train) before the next is looked up.
 *  TG_ALIAS   LAT aliasing.  ld_a and ld_b are 2048 bytes apart, so
 *             (pc >> 1) & 1023 is the same for both when the tables have 1024
 *             entries (and different when they have 2048).  ld_a is trained
 *             with a constant stride, then ld_b runs, then ld_a, then ld_b.
 *  TG_CONF    Destructive SS aliasing with the pipeline lag removed.  ld_x
 *             and ld_y run alternately with a delay after each, so every load
 *             retires before the next is looked up.  ld_x has stride 64 and
 *             ld_y has stride YSTRIDE: 1088 (64 + 1024) gives both the same
 *             SS index in a 1024-entry table, 128 does not (the control).
 *  TG_SQUASH  Squashed loads.  Four blocks, each a branch that is always
 *             taken but is cold (so it is predicted not taken), whose
 *             fall-through is a load: that load is fetched and issued on the
 *             wrong path and discarded when the branch resolves.  Each block
 *             also has a load that really executes.
 *  TG_FAULT   Faulting loads.  Two pages are mapped (mmap) and never touched;
 *             the first load from each takes a translation fault that the SE
 *             page-fault handler resolves by allocating the page, and the
 *             load is then executed again.
 *
 * Every load is a single `ld a0,0(a0)` (or `ld t1/t2,0(a0)`) in assembly, so
 * the loads and their PCs are exactly known; the runner checks the
 * disassembly (no other load anywhere).  The delay loops are register-only.
 * Freestanding like astra.c: _start and Linux system calls only; the data
 * are written by stores (no initialised data, see tests/lvp/ldld.c on -n).
 */

#define SYS_write 64
#define SYS_exit_group 94
#define SYS_mmap 222

#define BUF_BYTES 131072
#define BUF_LONGS (BUF_BYTES / 8)

#ifndef DELAY
#define DELAY 200
#endif
#ifndef CONF_N
#define CONF_N 48
#endif
#ifndef YSTRIDE
#define YSTRIDE 1088
#endif
#ifndef ALIAS_TRAIN
#define ALIAS_TRAIN 8
#endif

#define FAULT_ADDR 0x20000000UL
#define FAULT_PAGES 2
#define FAULT_PAGE 4096
#define FAULT_PROT 3
#define FAULT_MAP_FLAGS 0x32 /* MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED */

static long bufA[BUF_LONGS];
static long bufB[BUF_LONGS];

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

    buf[--pos] = '\n';
    do {
        buf[--pos] = '0' + value % 10;
        value /= 10;
    } while (value != 0);

    /* "checksum " followed by the number, as astra.c prints it */
    syscall6(SYS_write, 1, (long)"checksum ", 9, 0, 0, 0);
    syscall6(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos, 0, 0, 0);
}

/* Lets the loads before it retire and train the table before the next load
 * is issued and looks the table up (lookup is at issue, training at
 * retirement) */
static inline void
delay(void)
{
    long n = DELAY;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

static void
init_buffers(void)
{
    /* Stores only: the value at long index k is 7k+3 in bufA and 11k+5 in
     * bufB */
    for (long i = 0; i < BUF_LONGS; i++) {
        bufA[i] = i * 7 + 3;
        bufB[i] = i * 11 + 5;
    }
}

#if defined(TG_COLD)

__asm__(".text\n"
        ".balign 64\n"
        ".globl cold_a\n"
        "cold_a:\n\tld a0,0(a0)\n\tret\n"
        ".size cold_a, .-cold_a\n"
        ".balign 16\n"
        ".globl cold_b\n"
        "cold_b:\n\tld a0,0(a0)\n\tret\n"
        ".size cold_b, .-cold_b\n"
        ".balign 4\n");

extern long cold_a(const long *p);
extern long cold_b(const long *p);

void
_start(void)
{
    long total = 0;

    init_buffers();
    total += cold_a(&bufA[8]);      /* the first load of the program */
    delay();
    total += cold_b(&bufB[8]);      /* first use of another LAT entry */
    delay();
    total += cold_a(&bufA[24]);     /* second use, stride 128 bytes */
    delay();
    total += cold_b(&bufB[24]);

    print_checksum(total);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(TG_ALIAS)

/* ld_a at a 4096-byte boundary, ld_b 2048 bytes after it */
__asm__(".text\n"
        ".balign 4096\n"
        ".globl ld_a\n"
        "ld_a:\n\tld a0,0(a0)\n\tret\n"
        ".size ld_a, .-ld_a\n"
        ".balign 2048\n"
        ".globl ld_b\n"
        "ld_b:\n\tld a0,0(a0)\n\tret\n"
        ".size ld_b, .-ld_b\n"
        ".balign 4\n");

extern long ld_a(const long *p);
extern long ld_b(const long *p);

void
_start(void)
{
    long total = 0;

    init_buffers();
    for (int i = 0; i < ALIAS_TRAIN; i++) {
        total += ld_a(&bufA[8 * i]);        /* stride 64 bytes */
        delay();
    }
    total += ld_b(&bufB[0]);
    delay();
    total += ld_a(&bufA[8 * ALIAS_TRAIN]);
    delay();
    total += ld_b(&bufB[64]);

    print_checksum(total);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(TG_CONF)

__asm__(".text\n"
        ".balign 64\n"
        ".globl ld_x\n"
        "ld_x:\n\tld a0,0(a0)\n\tret\n"
        ".size ld_x, .-ld_x\n"
        ".balign 16\n"
        ".globl ld_y\n"
        "ld_y:\n\tld a0,0(a0)\n\tret\n"
        ".size ld_y, .-ld_y\n"
        ".balign 4\n");

extern long ld_x(const char *p);
extern long ld_y(const char *p);

void
_start(void)
{
    long total = 0;
    const char *x = (const char *)bufA;
    const char *y = (const char *)bufB;

    init_buffers();
    for (long i = 0; i < CONF_N; i++) {
        total += ld_x(x);
        delay();
        total += ld_y(y);
        delay();
        x += 64;
        y += YSTRIDE;
    }

    print_checksum(total);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(TG_SQUASH)

/*
 * Each block: t0 = 1 after a chain of dependent multiplies, so the branch
 * resolves late; the branch is always taken (t0 != 0).  Its fall-through,
 * sq_wrongN, is a load that is never architecturally executed.  The next
 * load, sq_realN, is.  a0 is the pointer (advanced 64 bytes per block), a2
 * the sum.
 */
#define SQ_BLOCK(n)                                                       \
    "\tli t3,1\n"                                                         \
    "\tmul t0,t3,t3\n\tmul t0,t0,t0\n\tmul t0,t0,t0\n\tmul t0,t0,t0\n"    \
    "\tbnez t0,1" #n "f\n"                                                \
    ".globl sq_wrong" #n "\n"                                             \
    "sq_wrong" #n ":\n\tld t1,0(a0)\n"                                    \
    "1" #n ":\n"                                                          \
    ".globl sq_real" #n "\n"                                              \
    "sq_real" #n ":\n\tld t2,0(a0)\n"                                     \
    "\tadd a2,a2,t2\n\taddi a0,a0,64\n"

__asm__(".text\n"
        ".balign 64\n"
        ".globl sq_run\n"
        "sq_run:\n\tli a2,0\n"
        SQ_BLOCK(0) SQ_BLOCK(1) SQ_BLOCK(2) SQ_BLOCK(3)
        "\tmv a0,a2\n\tret\n"
        ".size sq_run, .-sq_run\n");

extern long sq_run(const long *p);

void
_start(void)
{
    long total;

    init_buffers();
    total = sq_run(&bufA[8]);

    print_checksum(total);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(TG_FAULT)

__asm__(".text\n"
        ".balign 64\n"
        ".globl ft_load\n"
        "ft_load:\n\tld a0,0(a0)\n\tret\n"
        ".size ft_load, .-ft_load\n"
        ".balign 4\n");

extern long ft_load(const long *p);

void
_start(void)
{
    long total = 0;

    init_buffers();
    if (syscall6(SYS_mmap, FAULT_ADDR, FAULT_PAGES * FAULT_PAGE, FAULT_PROT,
                 FAULT_MAP_FLAGS, -1, 0) != (long)FAULT_ADDR) {
        syscall6(SYS_exit_group, 1, 0, 0, 0, 0, 0);
    }

    /* Never touched: the first load from each page takes a translation
     * fault, which the SE page fault handler resolves; the load is then
     * executed again.  Pages are zero filled, so every value is 0 */
    total += ft_load((const long *)FAULT_ADDR);
    delay();
    total += ft_load((const long *)(FAULT_ADDR + FAULT_PAGE));
    delay();
    total += ft_load((const long *)(FAULT_ADDR + 8));   /* no fault now */
    total += 1;

    print_checksum(total);
    syscall6(SYS_exit_group, 0, 0, 0, 0, 0, 0);
    for (;;)
        ;
}

#else
#error "define TG_COLD, TG_ALIAS, TG_CONF, TG_SQUASH or TG_FAULT"
#endif
