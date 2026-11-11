
/*
内核 UDP 下行抓包（移植自 enkernel udp_capture.c/.h）

kprobe 挂 udp_queue_rcv_one_skb（个别 KMI 被内联时回退 udp_queue_rcv_skb，参数布局相同：
regs->regs[0]=sk, regs->regs[1]=skb），UID 过滤与载荷长度窗口照搬 enkernel；
传输层从 ioctl 换成共享页直写：环区在 LS_UDP_RING_OFFSET（request_obj 之后的独立区，
见 io_struct.h），内核自旋锁内生产、用户态直读，消费推进走 request_op_udp_read。

上下文约束：
    pre_handler 运行在软中断上下文：锁内只做 memcpy，不做任何可能睡眠的事；
    生产路径全部函数 NOKPROBE 注解（照 enkernel）；
    init/read/stop 运行在 DispatchThreadFunction（kthread，可睡眠），注册/注销 kprobe、清环都在这里。
消费语义（照 enkernel「丢新保旧」）：
    写追上未 ack 的读（write_idx - reader_ack == LS_UDP_SLOT_COUNT）时 dropped++ 并丢弃新包，
    不覆盖未读槽；reader_ack 只由内核在 request_op_udp_read 里推进。
本文件 header-only，仅被 lsdriver.c include 一次。
*/

#ifndef UDP_CAPTURE_H
#define UDP_CAPTURE_H
#include <linux/cred.h>
#include <linux/if_ether.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/kprobes.h>
#include <linux/ktime.h>
#include <linux/mutex.h>
#include <linux/percpu.h>
#include <linux/pid.h>
#include <linux/sched.h>
#include <linux/skbuff.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/udp.h>
#include <linux/uidgid.h>
#include <net/sock.h>
#include "io_version/io_struct.h"
#include "klog/lsdriver_log.h"

#define LS_UDP_FLAG_TRUNC 0x1 // bit0 = 截断（对齐契约 udp_ring_slot.flags）

// 每个 CPU 一份解析暂存：skb 解析/拷贝在锁外完成，锁内只做一次 memcpy 进共享页环
struct udp_capture_scratch
{
    uint64_t ts_ns;
    uint32_t src_ip, dst_ip; // 网络序
    uint16_t sport, dport;
    uint16_t uid;
    uint16_t flags;
    uint16_t len; // 实际拷贝的载荷长度
    uint8_t payload[LS_UDP_SLOT_PAYLOAD];
};

static DEFINE_MUTEX(udp_capture_init_lock);
static DEFINE_SPINLOCK(udp_capture_lock);
static DEFINE_PER_CPU(struct udp_capture_scratch, udp_capture_scratch_pool);
static struct kprobe udp_capture_kp;
static bool udp_capture_hooked;                       // kprobe 已注册
static bool udp_capture_enabled;                      // 生产开关（pre_handler 快速路径）
static uid_t udp_capture_uid;                         // udp_init 解析出的目标进程 UID
static uint16_t udp_capture_min_len;                  // 载荷下限过滤
static uint16_t udp_capture_max_len;                  // 载荷上限过滤
static uint8_t *udp_capture_shared_base = NULL;       // 共享页 vmap 基址（request_obj 起点）

// 环头（共享页基址 + LS_UDP_RING_OFFSET）
static inline struct udp_ring_header *udp_capture_ring_header(void)
{
    uint8_t *base = READ_ONCE(udp_capture_shared_base);

    if (!base) return NULL;
    return (struct udp_ring_header *)(base + LS_UDP_RING_OFFSET);
}

// 第 index 个槽的地址（步长 LS_UDP_SLOT_SIZE，槽数为 2 的幂，索引用掩码）
static inline struct udp_ring_slot *udp_capture_ring_slot(struct udp_ring_header *header, uint32_t index)
{
    return (struct udp_ring_slot *)((uint8_t *)header + LS_UDP_RING_HEADER_SIZE + (size_t)(index & (LS_UDP_SLOT_COUNT - 1)) * LS_UDP_SLOT_SIZE);
}

// 清环：停止抓包并清 ring（生产者已停，process 上下文直接清；magic 保留供用户校验布局）
static inline void udp_capture_ring_clear(void)
{
    struct udp_ring_header *header = udp_capture_ring_header();

    if (!header) return;
    memset((uint8_t *)header, 0, LS_UDP_RING_BYTES);
    WRITE_ONCE(header->magic, LS_UDP_RING_MAGIC);
}

// pid → uid（照抄 enkernel udp_uid_from_pid）
static inline uid_t udp_capture_uid_from_pid(pid_t pid)
{
    struct task_struct *task;
    uid_t uid = 0;

    if (pid <= 0) return 0;

    rcu_read_lock();
    task = pid_task(find_vpid(pid), PIDTYPE_PID);
    if (task) uid = from_kuid(&init_user_ns, task_uid(task));
    rcu_read_unlock();
    return uid;
}

// sk->sk_uid 与目标 UID 比对（照抄 enkernel udp_sock_uid_ok，覆盖 5.10..6.12 的 kuid_t 表示）
static inline bool udp_capture_sock_uid_ok(const struct sock *sk)
{
    uid_t sock_uid;

    if (!sk) return false;
    sock_uid = from_kuid_munged(&init_user_ns, sk->sk_uid);
    return sock_uid == READ_ONCE(udp_capture_uid);
}

// 解析 skb 并填 per-CPU 暂存（照抄 enkernel udp_fill_packet 的过滤与截断语义）
static int udp_capture_fill_packet(struct sk_buff *skb, struct udp_capture_scratch *out)
{
    const struct iphdr *ip;
    const struct udphdr *udp;
    int off;
    int plen;
    int copy;

    if (!skb || skb->protocol != htons(ETH_P_IP)) return -EINVAL;

    ip = ip_hdr(skb);
    udp = udp_hdr(skb);
    if (!ip || !udp || ip->version != 4 || ip->protocol != IPPROTO_UDP) return -EINVAL;

    plen = ntohs(udp->len);
    if (plen < (int)sizeof(*udp)) return -EINVAL;
    plen -= (int)sizeof(*udp);
    if (plen < (int)READ_ONCE(udp_capture_min_len)) return -EINVAL;

    out->src_ip = ip->saddr;
    out->dst_ip = ip->daddr;
    out->sport = ntohs(udp->source);
    out->dport = ntohs(udp->dest);
    out->flags = 0;
    out->uid = (uint16_t)udp_capture_uid; // 槽字段 16 位，Android app uid（10xxx-29xxx）可容纳
    out->ts_ns = ktime_get_ns();
    copy = plen;
    if (copy > (int)READ_ONCE(udp_capture_max_len))
    {
        copy = READ_ONCE(udp_capture_max_len);
        out->flags |= LS_UDP_FLAG_TRUNC;
    }
    if (copy > (int)LS_UDP_SLOT_PAYLOAD)
    {
        copy = LS_UDP_SLOT_PAYLOAD;
        out->flags |= LS_UDP_FLAG_TRUNC;
    }
    off = skb_transport_offset(skb) + (int)sizeof(*udp);
    if (skb_copy_bits(skb, off, out->payload, copy)) return -EFAULT;
    out->len = (uint16_t)copy;
    return 0;
}

/*
kprobe pre_handler（软中断上下文）：
    1. enabled 快速开关（acquire，与 init 的 release 配对）；
    2. UID 过滤 + 锁外解析（per-CPU 暂存，kprobe 内不可迁移，暂存私有）；
    3. 自旋锁内：满则丢新保旧，否则写槽（一次 memcpy）→ seq 最后发布 → write_idx 递增发布。
共享页环只有内核写、用户读，读侧以 write_idx（release）/槽内 seq（release）为界。
*/
static int udp_capture_rcv_pre(struct kprobe *p, struct pt_regs *regs)
{
    struct sock *sk;
    struct sk_buff *skb;
    struct udp_ring_header *header;
    struct udp_ring_slot *slot;
    struct udp_capture_scratch *scratch;
    uint8_t *base;
    unsigned long flags;
    uint32_t write_idx;
    uint32_t reader_ack;
    uint64_t seq;

    (void)p;
    if (!smp_load_acquire(&udp_capture_enabled)) return 0;

    sk = (struct sock *)regs->regs[0];
    skb = (struct sk_buff *)regs->regs[1];
    if (!udp_capture_sock_uid_ok(sk)) return 0;

    scratch = this_cpu_ptr(&udp_capture_scratch_pool);
    if (udp_capture_fill_packet(skb, scratch)) return 0;

    base = READ_ONCE(udp_capture_shared_base);
    if (!base) return 0;
    header = (struct udp_ring_header *)(base + LS_UDP_RING_OFFSET);

    spin_lock_irqsave(&udp_capture_lock, flags);
    if (!udp_capture_enabled)
    {
        spin_unlock_irqrestore(&udp_capture_lock, flags);
        return 0;
    }
    write_idx = header->write_idx;
    reader_ack = header->reader_ack;
    if (write_idx - reader_ack >= LS_UDP_SLOT_COUNT) // ring 满：丢新保旧（enkernel 语义）
    {
        header->dropped++;
        spin_unlock_irqrestore(&udp_capture_lock, flags);
        return 0;
    }

    slot = udp_capture_ring_slot(header, write_idx);
    memcpy(slot->payload, scratch->payload, scratch->len); // 锁内唯一的大动作（≤ 一次 memcpy）
    slot->ts_ns = scratch->ts_ns;
    slot->len = scratch->len;
    slot->flags = scratch->flags;
    slot->src_ip = scratch->src_ip;
    slot->dst_ip = scratch->dst_ip;
    slot->sport = scratch->sport;
    slot->dport = scratch->dport;
    slot->uid = scratch->uid;
    slot->reserved = 0;
    seq = header->write_seq + 1;
    smp_store_release(&slot->seq, seq); // seq 最后写：槽内 seq 非 0 即载荷完整
    WRITE_ONCE(header->write_seq, seq);
    write_idx++;
    smp_store_release(&header->write_idx, write_idx); // 发布：读侧可见界
    spin_unlock_irqrestore(&udp_capture_lock, flags);
    return 0;
}
NOKPROBE_SYMBOL(udp_capture_fill_packet);
NOKPROBE_SYMBOL(udp_capture_rcv_pre);

// 停生产 + 排空在途回调并注销 kprobe；调用方必须持有 udp_capture_init_lock
static void udp_capture_disable_and_unregister(void)
{
    unsigned long flags;

    spin_lock_irqsave(&udp_capture_lock, flags);
    WRITE_ONCE(udp_capture_enabled, false);
    spin_unlock_irqrestore(&udp_capture_lock, flags);
    if (udp_capture_hooked)
    {
        unregister_kprobe(&udp_capture_kp);
        udp_capture_hooked = false;
    }
}

/*
request_op_udp_init：解析 pid→uid、注册 kprobe（幂等：重入时先注销再注册）、清环、active=1。
shared_base/shared_bytes 由分发层传入（vmap 基址与覆盖字节数）；
用户没映射环区页（旧版布局）时这里直接拒绝，不影响其余请求。
*/
static int udp_capture_init(uint8_t *shared_base, size_t shared_bytes, struct udp_capture_ctrl *ctrl)
{
    struct udp_ring_header *header;
    uid_t uid;
    unsigned long flags;
    int ret;

    if (!shared_base || !ctrl) return -EINVAL;
    if (ctrl->pid <= 0) return -EINVAL;
    if (LS_UDP_RING_OFFSET + LS_UDP_RING_BYTES > shared_bytes) return -ENOMEM; // 环区页未映射

    uid = udp_capture_uid_from_pid(ctrl->pid);
    if (!uid) return -ESRCH;

    mutex_lock(&udp_capture_init_lock);
    // 先停回调再改配置：unregister_kprobe 排空在途回调后，UID/长度/环才能安全重置
    udp_capture_disable_and_unregister();

    WRITE_ONCE(udp_capture_shared_base, shared_base);
    udp_capture_ring_clear();

    memset(&udp_capture_kp, 0, sizeof(udp_capture_kp));
    udp_capture_kp.symbol_name = "udp_queue_rcv_one_skb";
    udp_capture_kp.pre_handler = udp_capture_rcv_pre;
    ret = register_kprobe(&udp_capture_kp);
    if (ret)
    {
        // 个别 KMI 该静态函数被内联而不在 kallsyms；udp_queue_rcv_skb 参数布局相同，兜底
        memset(&udp_capture_kp, 0, sizeof(udp_capture_kp));
        udp_capture_kp.symbol_name = "udp_queue_rcv_skb";
        udp_capture_kp.pre_handler = udp_capture_rcv_pre;
        ret = register_kprobe(&udp_capture_kp);
    }
    if (ret)
    {
        WRITE_ONCE(udp_capture_shared_base, NULL);
        mutex_unlock(&udp_capture_init_lock);
        ls_log_always_tag("udp", "register_kprobe 失败，错误码: %d\n", ret);
        return ret;
    }
    udp_capture_hooked = true;

    spin_lock_irqsave(&udp_capture_lock, flags);
    WRITE_ONCE(udp_capture_uid, uid);
    WRITE_ONCE(udp_capture_min_len, ctrl->min_payload ? (uint16_t)ctrl->min_payload : 16); // 0 → enkernel 默认 16
    WRITE_ONCE(udp_capture_max_len, ctrl->max_payload ? (uint16_t)ctrl->max_payload : LS_UDP_SLOT_PAYLOAD);
    if (udp_capture_max_len > LS_UDP_SLOT_PAYLOAD) udp_capture_max_len = LS_UDP_SLOT_PAYLOAD; // 上限钳制
    smp_store_release(&udp_capture_enabled, true);
    header = udp_capture_ring_header();
    WRITE_ONCE(header->active, 1);
    spin_unlock_irqrestore(&udp_capture_lock, flags);

    mutex_unlock(&udp_capture_init_lock);
    ls_log_always_tag("udp", "UDP 下行抓包启动 uid=%u min=%u max=%u\n", uid, udp_capture_min_len, udp_capture_max_len);
    return 0;
}

/*
request_op_udp_read：把 ctrl->consume_count 累加进 reader_ack（不允许越过生产位置），
回填 out_write_seq/out_dropped/out_active。
*/
static int udp_capture_read(struct udp_capture_ctrl *ctrl)
{
    unsigned long flags;

    if (!ctrl) return -EINVAL;

    spin_lock_irqsave(&udp_capture_lock, flags);
    if (udp_capture_ring_header())
    {
        struct udp_ring_header *header = udp_capture_ring_header();
        uint32_t reader_ack = header->reader_ack;
        uint32_t write_idx = header->write_idx;
        uint32_t consume = ctrl->consume_count;

        if (consume > write_idx - reader_ack) consume = write_idx - reader_ack; // 钳制：ack 不能越过生产者
        header->reader_ack = reader_ack + consume;
        ctrl->out_write_seq = header->write_seq;
        ctrl->out_dropped = header->dropped;
    }
    else
    {
        ctrl->out_write_seq = 0;
        ctrl->out_dropped = 0;
    }
    ctrl->out_active = smp_load_acquire(&udp_capture_enabled) ? 1 : 0;
    spin_unlock_irqrestore(&udp_capture_lock, flags);
    return 0;
}

// request_op_udp_stop：注销 kprobe、active=0、清 ring
static int udp_capture_stop(void)
{
    mutex_lock(&udp_capture_init_lock);
    udp_capture_disable_and_unregister();
    udp_capture_ring_clear();
    mutex_unlock(&udp_capture_init_lock);
    ls_log_always_tag("udp", "UDP 下行抓包停止\n");
    return 0;
}

// 模块卸载 / 用户进程退出清理：必须 unregister_kprobe；环随共享页失效，不再触碰
static void udp_capture_exit(void)
{
    mutex_lock(&udp_capture_init_lock);
    udp_capture_disable_and_unregister();
    WRITE_ONCE(udp_capture_shared_base, NULL);
    mutex_unlock(&udp_capture_init_lock);
}

#endif // UDP_CAPTURE_H
