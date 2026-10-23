/*
 * Workloads for the two predictor-policy tests of run_lvp_stageb.sh
 * ("confidence" and "alias"); see tests/lvp/README.md.  Neither uses
 * address speculation: they only exercise the last-value table.
 *
 * POLICY_CONF: one load instruction in read_n() reads a volatile cell that
 * holds 1000 for CONF_N reads, then 2000 for CONF_N reads, then 1000 again.
 * The table entry of that load is trained to saturation, hit by one value
 * change, and trained again.  A decrementing 2-bit counter goes 3 -> 2 on
 * the change and is confident again after one more matching observation;
 * a counter that is reset on a mismatch would go 3 -> 0 and need three.
 *
 * POLICY_ALIAS: two load instructions, ld_a() and ld_b(), 2048 bytes apart
 * (two instructions at pc and pc + 2048 have the same index of a table of
 * 1024 entries, index = (pc >> 1) & 1023).  ld_a is trained to saturation
 * with 111, then ld_b loads 222, then ld_a loads 111 again.  With an
 * untagged table ld_b is predicted from the entry that ld_a trained, and
 * its mismatch lowers the confidence that ld_a then sees.  With a tagged
 * table neither happens.
 *
 * The programs are freestanding, like ldld.c: they start at _start and use
 * Linux system calls only.  The delay loops are register-only so that the
 * only loads are the ones named above (the runner checks this).
 */

#define SYS_write 64
#define SYS_exit_group 94

#ifndef CONF_N
#define CONF_N 48
#endif
#ifndef ALIAS_TRAIN
#define ALIAS_TRAIN 12
#endif
#ifndef READ_DELAY
#define READ_DELAY 40
#endif
#ifndef DELAY
#define DELAY 200
#endif

static long
syscall3(long num, long arg0, long arg1, long arg2)
{
    register long a7 __asm__("a7") = num;
    register long a0 __asm__("a0") = arg0;
    register long a1 __asm__("a1") = arg1;
    register long a2 __asm__("a2") = arg2;

    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2)
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

    /* "checksum " followed by the number, as ldld.c prints it */
    syscall3(SYS_write, 1, (long)"checksum ", 9);
    syscall3(SYS_write, 1, (long)(buf + pos), sizeof(buf) - pos);
}

/* In read_n(): lets each load commit and train the table before the next
 * load is issued and looks the table up, so that every lookup sees the
 * result of all the earlier trainings */
static inline void
delay_per_read(void)
{
    long n = READ_DELAY;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

/* Lets the loads before it commit and train the table before the next one
 * is issued (the table is read at issue and trained at commit) */
static inline void
delay(void)
{
    long n = DELAY;

    __asm__ volatile("1: addi %0,%0,-1\n\tbnez %0,1b" : "+r"(n));
}

#if defined(POLICY_CONF)

static volatile long cell;

__attribute__((noinline)) long
read_n(long n)
{
    long sum = 0;

    for (long i = 0; i < n; i++) {
        sum += cell;
        delay_per_read();
    }

    return sum;
}

void
_start(void)
{
    long total = 0;

    cell = 1000;
    total += read_n(CONF_N);
    cell = 2000;
    total += read_n(CONF_N);
    cell = 1000;
    total += read_n(CONF_N);

    print_checksum(total);
    syscall3(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}

#elif defined(POLICY_ALIAS)

static long va;
static long vb;

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

extern long ld_a(long *p);
extern long ld_b(long *p);

void
_start(void)
{
    long total = 0;

    va = 111;   /* stores, not initialised data: see ldld.c on -n */
    vb = 222;
    for (int i = 0; i < ALIAS_TRAIN; i++)
        total += ld_a(&va);
    delay();
    total += ld_b(&vb);     /* its own entry in a tagged table */
    delay();
    for (int i = 0; i < ALIAS_TRAIN; i++)
        total += ld_a(&va);

    print_checksum(total);
    syscall3(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}

#else
#error "define POLICY_CONF or POLICY_ALIAS"
#endif
