/*
 * Load-to-load microbenchmark for the Minor LVP prototype.
 *
 * sum_slots() loads a pointer from an array whose elements are one cache
 * line apart (so every load misses the L1 data cache), and then loads a
 * value through that pointer:
 *
 *      ld   a5, 0(a4)      # producer: the pointer, always the same value
 *      ld   a5, 24(a5)     # consumer: its address is the producer's value
 *
 * A last-value predictor becomes confident about the producer after a few
 * iterations, and the consumer can then be sent to the memory system before
 * the producer's data returns.  The generated assembly must be checked
 * (run_lvp_stageb.sh check-asm) before relying on this.
 *
 * By default this is the stable-pointer case: every slot holds the same
 * pointer.  With -DLVP_PHASE it is the phase-change case: the pointer
 * alternates between two nodes in blocks of PHASE_BLOCK slots, so the same
 * producer PC returns a different pointer at the start of every block and
 * a confident prediction made from the previous block is wrong.  The code
 * of sum_slots() is the same in both cases, and the slots are filled with
 * arithmetic only, so there is no load other than the two in sum_slots().
 *
 * The cross toolchain used for gem5 tests has no C library, so this is a
 * freestanding program: it starts at _start and uses Linux system calls
 * directly.  That also keeps all the code that runs small and known.
 */

#define NUM_SLOTS 16384

#define SYS_write 64
#define SYS_exit_group 94

struct node
{
    long v[4];
};

/* One slot per 64 byte cache line */
struct slot
{
    struct node *volatile p;
    char pad[56];
};

#ifdef LVP_PHASE
#define PHASE_BLOCK 64
static struct node node_b;
#endif
static struct node node;
static struct slot slots[NUM_SLOTS];

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

/* Its address range is used to confine address speculation to this loop */
__attribute__((noinline)) long
sum_slots(struct slot *s, long n)
{
    long sum = 0;

    for (long i = 0; i < n; i++) {
        struct node *p = s[i].p;
        sum += p->v[3];
    }

    return sum;
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
    node.v[3] = 7;
#ifdef LVP_PHASE
    node_b.v[3] = 11;

    for (long i = 0; i < NUM_SLOTS; i++)
        slots[i].p = ((i / PHASE_BLOCK) & 1) ? &node_b : &node;
#else
    for (long i = 0; i < NUM_SLOTS; i++)
        slots[i].p = &node;
#endif

    print_checksum(sum_slots(slots, NUM_SLOTS));

    syscall3(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}
