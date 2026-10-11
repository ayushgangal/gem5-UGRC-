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
 * With -DLVP_PHASE -DLVP_FANOUT (fan-out needs the phase-change case) one
 * producer has two dependent loads at different PCs, from the same base
 * register and at different offsets:
 *
 *      ld   a4, 0(a5)      # producer: the pointer
 *      ld   a3, 16(a4)     # consumer 1: v[2]
 *      ld   a2, 24(a4)     # consumer 2: v[3]
 *
 * Neither consumer may overwrite the producer's register, so both depend
 * on the producer itself.  The empty asm statement in sum_slots() is what
 * guarantees that: the pointer and both loaded values must all be in
 * registers at the same time, in different ones.  It emits no instruction.
 * The node values are chosen so that v[2] and v[3] contribute to the
 * checksum independently: it is the sum of the v[3] values plus the sum
 * of the v[2] values shifted left by 32.
 *
 * With -DLVP_FAULT the loop of sum_slots() is the stable one, but it is
 * called in 32 cycles, each of which makes a speculative consumer use a
 * stale prediction that points into a page which is no longer mapped:
 *
 *      mmap page A (0x20000000, fixed), store v[3] = 7 in the node in it
 *      sum_slots() over 8 slots that point to A: trains the producer
 *      store to a large array: the next slot loads miss in the data cache
 *      munmap page A
 *      sum_slots() over 3 slots that point to node B (v[3] = 11, mapped)
 *
 * The first load of the producer's PC in the last call is predicted, with
 * confidence, to be the pointer into A, which is now unmapped.  Its
 * consumer can be sent to the memory system with that pointer before the
 * producer's data returns, and its address translation faults.  The fault
 * must not be taken: the wrong prediction is found out, the consumer is
 * discarded, and it is fetched again and loads from B.  The 3 loads of B
 * are too few for the producer to be confident of B's pointer, so the next
 * cycle's first call starts without a prediction.  The program checks the
 * results of the system calls: a failed mmap or munmap ends it with exit
 * status 1.  Like the other cases it has no load outside sum_slots(): the
 * addresses it uses are constants, and it only stores to memory.
 *
 * The cross toolchain used for gem5 tests has no C library, so this is a
 * freestanding program: it starts at _start and uses Linux system calls
 * directly.  That also keeps all the code that runs small and known.
 */

#ifdef LVP_FANOUT
#ifndef LVP_PHASE
#error "LVP_FANOUT needs LVP_PHASE"
#endif
#endif

#ifdef LVP_FAULT
#if defined(LVP_PHASE) || defined(LVP_FANOUT)
#error "LVP_FAULT is not combined with LVP_PHASE or LVP_FANOUT"
#endif
#endif

#define NUM_SLOTS 16384

#define SYS_write 64
#define SYS_exit_group 94

#ifdef LVP_FAULT
#define SYS_munmap 215
#define SYS_mmap 222

/* The page that is mapped and unmapped: the checker has these too */
#define FAULT_ADDR 0x20000000UL
#define FAULT_PAGE 4096
#define FAULT_CYCLES 32
#define FAULT_TRAIN 8   /* loads through pointers into the page */
#define FAULT_USE 3     /* loads through the pointer to node B */

/* PROT_READ | PROT_WRITE */
#define FAULT_PROT 3
/* MAP_PRIVATE (0x02) | MAP_FIXED (0x10) | MAP_ANONYMOUS (0x20) */
#define FAULT_MAP_FLAGS 0x32

/* A store to every cache line of this evicts the lines of the slots from
 * the data cache (twice its default size) */
#define SWEEP_BYTES (256 * 1024)
#endif

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
#endif
#if defined(LVP_PHASE) || defined(LVP_FAULT)
static struct node node_b;
#endif
#ifdef LVP_FAULT
static volatile char sweep[SWEEP_BYTES];
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

#ifdef LVP_FAULT
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

/* Ends the program with exit status 1 and a message on stderr */
__attribute__((noreturn)) static void
fail(const char *msg, long len)
{
    syscall3(SYS_write, 2, (long)msg, len);
    syscall3(SYS_exit_group, 1, 0, 0);
    for (;;)
        ;
}

#define FAIL(msg) fail(msg "\n", sizeof(msg))
#endif

/* Its address range is used to confine address speculation to this loop */
__attribute__((noinline)) long
sum_slots(struct slot *s, long n)
{
    long sum = 0;

    for (long i = 0; i < n; i++) {
        struct node *p = s[i].p;
#ifdef LVP_FANOUT
        long hi = p->v[2];
        long lo = p->v[3];

        /* p, hi and lo are all live here: no load may write p's register */
        __asm__ volatile("" : "+r"(hi), "+r"(lo) : "r"(p));
        sum += lo + (hi << 32);
#else
        sum += p->v[3];
#endif
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
#ifdef LVP_FAULT
    struct node *a = (struct node *)FAULT_ADDR;
    long total = 0;

    node_b.v[3] = 11;
    for (long i = 0; i < FAULT_TRAIN; i++)
        slots[i].p = a;
    for (long i = 0; i < FAULT_USE; i++)
        slots[FAULT_TRAIN + i].p = &node_b;

    for (long cycle = 0; cycle < FAULT_CYCLES; cycle++) {
        if (syscall6(SYS_mmap, FAULT_ADDR, FAULT_PAGE, FAULT_PROT,
                     FAULT_MAP_FLAGS, -1, 0) != (long)FAULT_ADDR) {
            FAIL("mmap of the page failed");
        }

        /* The first store to the page allocates it */
        a->v[3] = 7;

        total += sum_slots(&slots[0], FAULT_TRAIN);

        for (long i = 0; i < SWEEP_BYTES; i += 64)
            sweep[i] = 1;

        if (syscall6(SYS_munmap, FAULT_ADDR, FAULT_PAGE, 0, 0, 0, 0) != 0)
            FAIL("munmap of the page failed");

        total += sum_slots(&slots[FAULT_TRAIN], FAULT_USE);
    }

    print_checksum(total);
#else
    node.v[3] = 7;
#ifdef LVP_PHASE
    node_b.v[3] = 11;
#ifdef LVP_FANOUT
    node.v[2] = 5;
    node_b.v[2] = 2;
#endif

    for (long i = 0; i < NUM_SLOTS; i++)
        slots[i].p = ((i / PHASE_BLOCK) & 1) ? &node_b : &node;
#else
    for (long i = 0; i < NUM_SLOTS; i++)
        slots[i].p = &node;
#endif

    print_checksum(sum_slots(slots, NUM_SLOTS));
#endif

    syscall3(SYS_exit_group, 0, 0, 0);
    for (;;)
        ;
}
