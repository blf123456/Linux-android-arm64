/* Tests the real session functions, with no device, module load or exit hooks. */
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdint.h>

#define TASK_COMM_LEN 16
#define PAGE_SIZE 4096
#define GFP_KERNEL 0
#define FOLL_WRITE 1
#define VM_MAP 0
#define PAGE_KERNEL 0
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define DIV_ROUND_UP(a,b) (((a)+(b)-1)/(b))
#define DEFINE_MUTEX(name) int name
#define READ_ONCE(x) (x)
#define atomic_read(x) (*(x))
#define smp_store_release(p,v) (*(p)=(v))
#define ls_log_always_tag(...) ((void)0)

struct signal_struct { int live; };
struct task_struct {
    char comm[TASK_COMM_LEN];
    int tgid, refs;
    bool alive;
    unsigned long start_time;
    struct signal_struct *signal;
    struct task_struct *next;
};
struct request_obj { bool user; char payload[8200]; };
struct page { int unused; };
struct mm_struct { int unused; };
static struct mm_struct fake_mm;
static struct page fake_page;
static struct task_struct *tasks;
static int rcu_depth, locked, pins, mappings, mm_refs, allocations, cleanups;
static int iterations, gup_result = -999;
static bool fail_alloc, fail_vmap, exit_during_vmap;
static struct task_struct *mapping_task;

static void mutex_lock(int *m) { assert(!locked); locked = 1; }
static void mutex_unlock(int *m) { assert(locked); locked = 0; }
static void rcu_read_lock(void) { ++rcu_depth; }
static void rcu_read_unlock(void) { --rcu_depth; }
#define for_each_process(task) for ((task)=tasks; (task); (task)=(task)->next)
static bool pid_alive(struct task_struct *task) { return task->alive; }
static void get_task_comm(char *name, struct task_struct *task) { strcpy(name, task->comm); }
static void get_task_struct(struct task_struct *task) { assert(task->refs > 0); ++task->refs; }
static void put_task_struct(struct task_struct *task) { assert(!rcu_depth); assert(task->refs > 1); --task->refs; }
static struct mm_struct *get_task_mm(struct task_struct *task) {
    assert(!rcu_depth && locked); mapping_task = task;
    if (!task->signal->live) return NULL;
    ++mm_refs; return &fake_mm;
}
static void mmput(struct mm_struct *mm) { --mm_refs; }
static void mmap_read_lock(struct mm_struct *mm) { assert(!rcu_depth && locked); }
static void mmap_read_unlock(struct mm_struct *mm) {}
static void *kmalloc_array(int count, size_t size, int flags) {
    if (fail_alloc) return NULL;
    ++allocations; return calloc(count, size);
}
static void kfree(void *p) { if (p) { --allocations; free(p); } }
static int get_user_pages_remote(struct mm_struct *mm, uint64_t addr, int n, int flags, struct page **pages, ...) {
    assert(!rcu_depth && locked && mm_refs == 1);
    int result = gup_result == -999 ? n : gup_result;
    for (int i = 0; i < result; ++i) pages[i] = &fake_page;
    if (result > 0) pins += result;
    return result;
}
static void release_gup_pages(struct page **pages, int n) { assert(n >= 0); pins -= n; assert(pins >= 0); }
static void *vmap(struct page **pages, int n, int flags, int prot) {
    assert(!rcu_depth && locked);
    if (exit_during_vmap) mapping_task->signal->live = 0;
    if (fail_vmap) return NULL;
    ++mappings; return calloc(1, sizeof(struct request_obj));
}
static void vunmap(void *p) { assert(locked); --mappings; free(p); }
static void v_touch_destroy(void) { assert(locked); ++cleanups; }
#define v_gnss_destroy() ((void)0)
#define v_gyro_destroy() ((void)0)
#define remove_process_hwbp() ((void)0)
#define remove_process_ptebp() ((void)0)
#define remove_process_dptdbg() ((void)0)
#define remove_process_stepbp() ((void)0)
#define syscall_monitor_remove_all() ((void)0)
#define cntvct_monitor_remove_all() ((void)0)
#define hide_task_remove(pid) ((void)0)
#define hide_kgsl_remove(pid) ((void)0)
#define hide_task_install(pid) ((void)0)
#define hide_kgsl_install(pid) ((void)0)
#define send_sig(...) abort() /* A reconnect must never send a signal. */
static bool kthread_should_stop(void) { return iterations-- <= 0; }
static void msleep(int ms) { assert(!locked && !rcu_depth); }

#include "session_under_test.h"

static void poll_once(void) { iterations = 1; ConnectThreadFunction(NULL); }
static void clear_session(void) {
    mutex_lock(&session_lock); ls_disconnect_locked(); mutex_unlock(&session_lock);
    assert(!req && !ls_process_task && !session_pages);
    assert(pins == 0 && mappings == 0 && allocations == 0 && mm_refs == 0);
}

int main(void) {
    struct signal_struct old_signal = {1}, next_signal = {1};
    struct task_struct old = {.comm="LS", .tgid=100, .refs=1, .alive=true, .start_time=1, .signal=&old_signal};
    struct task_struct next = {.comm="LS", .tgid=101, .refs=1, .alive=true, .start_time=2, .signal=&next_signal};

    /* Connect without any taskstats/exit hook; the session owns all references. */
    tasks = &old;
    poll_once();
    assert(ls_process_task == &old && req->user && old.refs == 2);
    assert(pins == 3 && mappings == 1 && mm_refs == 0);

    /* A second process must neither replace nor kill an active client. */
    old.next = &next;
    struct request_obj *original_mapping = req;
    poll_once();
    assert(ls_process_task == &old && req == original_mapping && next.refs == 1);

    /* Leader exit alone is not group exit; children still own the session. */
    old_signal.live = 1;
    poll_once();
    assert(ls_process_task == &old && cleanups == 0);

    /* Reproduce the incident: old group exited and no hook ran. Polling must
     * drop the old task/pages before attaching the new client, without SIGKILL. */
    old_signal.live = 0;
    poll_once();
    assert(ls_process_task == &next && old.refs == 1 && next.refs == 2);
    assert(cleanups == 1 && pins == 3 && mappings == 1);
    next_signal.live = 0;
    poll_once();
    assert(next.refs == 1 && cleanups == 2);
    clear_session();

    /* All failed attachments release GUP/mapping/mm/task resources. */
    tasks = &old; old.next = NULL; old_signal.live = 1;
    gup_result = -EFAULT; poll_once(); clear_session(); assert(old.refs == 1);
    gup_result = 2; poll_once(); clear_session(); assert(old.refs == 1);
    gup_result = -999; fail_alloc = true; poll_once(); clear_session(); assert(old.refs == 1);
    fail_alloc = false; fail_vmap = true; poll_once(); clear_session(); assert(old.refs == 1);
    fail_vmap = false; exit_during_vmap = true; poll_once(); clear_session(); assert(old.refs == 1);
    puts("PASS: client lifetime, no forced takeover, hook-independent cleanup, partial GUP and exit races");
    return 0;
}
