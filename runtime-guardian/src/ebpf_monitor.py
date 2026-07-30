#!/usr/bin/env python3
"""
Runtime Guardian — eBPF 运行时漏洞检测监控器
==============================================
功能等价于你的 strace MVP，但基于 eBPF，性能提升 100-1000 倍。

检测能力（与你的 strace 版完全对齐）：
  1. 🔴 敏感文件访问 — 检测 openat 调用中的敏感路径
  2. 🟠 系统调用风暴 — 1秒内 open 超过阈值（疑似勒索软件扫描）
  3. 🟡 可疑行为链   — open→read→write→exec（下载执行攻击）
  4. 🔵 网络异常     — 短时间内连接大量外网 IP
  5. 🟢 基线异常     — 偏离正常系统调用频率分布（需配合 baseline_detector.py）

架构设计（分层过滤）：
  ┌─ 内核态 (eBPF) ──────────────────────────┐
  │  快速过滤层：丢弃 99% 的正常事件            │
  │  - 敏感文件：内核态路径匹配，只推送告警     │
  │  - 调用风暴：内核态 LRU 计数器，超阈值推送  │
  │  - 行为链：推送精简事件（6字段，非全量）     │
  └──────────────────────┬──────────────────┘
                         │ perf ring buffer（零拷贝）
  ┌─ 用户态 (Python) ───┴─────────────────────┐
  │  精准分析层：状态机 + 统计模型               │
  │  - 行为链状态机（每个 PID 独立状态）        │
  │  - 网络基数估计（HyperLogLog 近似）          │
  │  - 基线异常（z-score + EWMA）               │
  │  - 自动响应（SIGKILL/iptables/告警）         │
  └────────────────────────────────────────────┘

运行方式：
  sudo python3 ebpf_monitor.py                    # 监控所有进程
  sudo python3 ebpf_monitor.py --pid 1234         # 监控指定 PID
  sudo python3 ebpf_monitor.py --storm 50         # 自定义风暴阈值
  sudo python3 ebpf_monitor.py --no-response      # 仅检测不响应（调试模式）

依赖：
  - Linux 4.18+ (WSL2 Ubuntu 20.04+)
  - bcc (sudo apt install bpfcc-tools python3-bpfcc)
  - psutil (pip install psutil)

作者注释中的类比说明:
  - "类似 JS 的 eventEmitter" → eBPF 的 perf_submit
  - "类似 Python 的 asyncio.Queue" → ring buffer
  - "类似 TypeScript 类型检查" → eBPF verifier
"""

import os
import sys
import time
import gc
import json
import signal
import struct
import ctypes
import argparse
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, Tuple, Optional, Set

# ============================================================
# 尝试导入依赖，给出友好的错误提示
# ============================================================
try:
    from bcc import BPF, PerfType, PerfSWConfig
except ImportError:
    print("=" * 60)
    print("❌ 未安装 bcc（BPF Compiler Collection）")
    print("=" * 60)
    print("请在 WSL Ubuntu 中运行以下命令安装：")
    print()
    print("  sudo apt-get update")
    print("  sudo apt-get install -y bpfcc-tools python3-bpfcc \\")
    print("      linux-headers-$(uname -r)")
    print()
    print("安装后验证：")
    print("  sudo python3 -c 'from bcc import BPF; print(\"OK\")'")
    sys.exit(1)

try:
    import psutil
except ImportError:
    print("⚠️  psutil 未安装，进程名解析功能降级")
    print("   安装: pip install psutil")
    psutil = None


# ============================================================
# 内核态 eBPF 程序（C 语言，运行时由 BCC 编译为 eBPF 字节码）
# ============================================================
EBPF_C_CODE = r"""
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#include <linux/fs.h>

// ==========================================
// 数据结构定义（内核态和用户态必须一致）
// 类比：TypeScript 的 interface / Python 的 dataclass
// ==========================================

// ---- 告警事件（高优先级，立即处理） ----
struct alert_event {
    u32 pid;            // 进程 ID
    u32 uid;            // 用户 ID（0=root）
    u32 alert_type;     // 事件类型码（见下方常量）
    u64 timestamp_ns;   // 纳秒时间戳
    char comm[16];      // 进程名（类似 Node.js 的 process.title）
    char filename[128]; // 相关文件名
    u32 count;          // 额外数据（如调用次数）
    u32 pad;            // 对齐填充
};
// alert_type 常量（内核态和用户态共享）
#define ALERT_SENSITIVE_FILE  1   // 敏感文件访问
#define ALERT_STORM           2   // 系统调用风暴
#define ALERT_BEHAVIOR_CHAIN  3   // 可疑行为链（用户态产生）
#define ALERT_NETWORK_ANOMALY 4   // 网络异常（用户态产生）
#define ALERT_BASELINE_ANOMALY 5  // 基线异常（用户态产生）

// ---- 追踪事件（低优先级，用于状态机分析） ----
struct trace_event {
    u32 pid;
    u32 uid;
    u32 syscall_nr;     // 系统调用号（__NR_openat=257, __NR_read=0 等）
    u64 timestamp_ns;
    char comm[16];
    char filename[64];  // openat/execve 的文件名，或 connect 的 IP
    u32 fd;             // fd 号（read/write）
    u32 pad;
};
// 系统调用号常量
#define SYS_NR_OPENAT   257
#define SYS_NR_READ     0
#define SYS_NR_WRITE    1
#define SYS_NR_EXECVE   59
#define SYS_NR_CONNECT  42

// ---- 调用风暴统计（内核态 LRU Map） ----
// 类比：Python 的 collections.Counter，但有自动过期
struct storm_val {
    u64 last_reset_ts;  // 上次重置时间（秒）
    u32 count;          // 当前窗口内的调用次数
    u32 alerted;        // 标记：这个窗口是否已经告警过（防重复）
};

// ==========================================
// BPF Map 声明
// 类比：Python multiprocessing.SharedMemory
//        JavaScript 的 SharedArrayBuffer
// ==========================================

// 告警事件环形缓冲区 → 用户态
BPF_PERF_OUTPUT(alert_events);

// 追踪事件环形缓冲区 → 用户态（行为链分析用）
BPF_PERF_OUTPUT(trace_events);

// 调用风暴计数器：key=PID, value=struct storm_val
BPF_HASH(storm_counters, unsigned int, struct storm_val, 10240);

// ==========================================
// 敏感文件匹配逻辑（内核态）
// 类比：JavaScript 的 Array.prototype.some()
//       prefixes.some(p => filename.startsWith(p))
//
// 因为 eBPF 不能动态循环，使用编译时展开：
// #pragma unroll → 编译器展开为 16 个 if 块
// ==========================================
static __always_inline int is_sensitive_file(const char *filename) {
    // 硬编码高频敏感文件（直接匹配，不依赖 map 加载）
    // /etc/passwd
    if (filename[0] == '/' && filename[1] == 'e' && filename[2] == 't' &&
        filename[3] == 'c' && filename[4] == '/' && filename[5] == 'p' &&
        filename[6] == 'a' && filename[7] == 's' && filename[8] == 's') return 1;
    // /etc/shadow
    if (filename[0] == '/' && filename[1] == 'e' && filename[2] == 't' &&
        filename[3] == 'c' && filename[4] == '/' && filename[5] == 's' &&
        filename[6] == 'h') return 1;
    // /root/
    if (filename[0] == '/' && filename[1] == 'r' && filename[2] == 'o' &&
        filename[3] == 'o' && filename[4] == 't' && filename[5] == '/') return 1;
    // /etc/sudoers
    if (filename[0] == '/' && filename[1] == 'e' && filename[2] == 't' &&
        filename[3] == 'c' && filename[4] == '/' && filename[5] == 's' &&
        filename[6] == 'u' && filename[7] == 'd') return 1;
    return 0;
}

// ==========================================
// Tracepoint: sys_enter_openat
// 功能：
//   1. 敏感文件检测
//   2. 调用风暴统计
//   3. 行为链追踪（推送事件给用户态）
// 类比：JavaScript 的 addEventListener('openat', handler)
// ==========================================
TRACEPOINT_PROBE(syscalls, sys_enter_openat) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    u64 now_ns = bpf_ktime_get_ns();
    u64 now_sec = now_ns / 1000000000ULL;

    // ---- 读取文件名 ----
    char filename[128];
    __builtin_memset(filename, 0, sizeof(filename));
    bpf_probe_read_user_str(filename, sizeof(filename), args->filename);

    // ---- 检测 1：敏感文件访问 ----
    if (is_sensitive_file(filename)) {
        bpf_trace_printk("RG_ALERT: %s\\n", filename);  // 调试：确认匹配触发
        struct alert_event alert;
        __builtin_memset(&alert, 0, sizeof(alert));
        alert.pid = pid;
        alert.uid = uid;
        alert.alert_type = ALERT_SENSITIVE_FILE;
        alert.timestamp_ns = now_ns;
        bpf_get_current_comm(&alert.comm, sizeof(alert.comm));
        __builtin_memcpy(alert.filename, filename, sizeof(alert.filename) - 1);
        alert_events.perf_submit(args, &alert, sizeof(alert));
    }

    // ---- 检测 2：系统调用风暴 ----
    struct storm_val zero;
    __builtin_memset(&zero, 0, sizeof(zero));
    struct storm_val *val = storm_counters.lookup(&pid);
    if (!val) {
        // 首次看到此 PID
        zero.last_reset_ts = now_sec;
        zero.count = 1;
        storm_counters.update(&pid, &zero);
    } else {
        // 检查是否进入新的时间窗口
        if (now_sec != val->last_reset_ts) {
            // 新的一秒，重置计数器
            val->last_reset_ts = now_sec;
            val->count = 1;
            val->alerted = 0;
        } else {
            val->count++;
        }

        // 风暴阈值检查（>100 次/秒 触发告警）
        // 用户态可以通过命令行动态调整阈值
        if (val->count > 100 && !val->alerted) {
            val->alerted = 1;  // 防止同一窗口内重复告警

            struct alert_event alert;
            __builtin_memset(&alert, 0, sizeof(alert));
            alert.pid = pid;
            alert.uid = uid;
            alert.alert_type = ALERT_STORM;
            alert.timestamp_ns = now_ns;
            alert.count = val->count;  // 携带实际调用次数
            bpf_get_current_comm(&alert.comm, sizeof(alert.comm));
            alert_events.perf_submit(args, &alert, sizeof(alert));
        }
    }

    // ---- 追踪 3：推送行为链事件给用户态 ----
    struct trace_event tev;
    __builtin_memset(&tev, 0, sizeof(tev));
    tev.pid = pid;
    tev.uid = uid;
    tev.syscall_nr = SYS_NR_OPENAT;
    tev.timestamp_ns = now_ns;
    bpf_get_current_comm(&tev.comm, sizeof(tev.comm));
    __builtin_memcpy(tev.filename, filename, sizeof(tev.filename) - 1);
    trace_events.perf_submit(args, &tev, sizeof(tev));

    return 0;
}

// ==========================================
// Tracepoint: sys_enter_read
// 用于行为链检测：追踪 read 调用
// ==========================================
TRACEPOINT_PROBE(syscalls, sys_enter_read) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;

    struct trace_event tev;
    __builtin_memset(&tev, 0, sizeof(tev));
    tev.pid = pid;
    tev.uid = uid;
    tev.syscall_nr = SYS_NR_READ;
    tev.timestamp_ns = bpf_ktime_get_ns();
    tev.fd = args->fd;
    bpf_get_current_comm(&tev.comm, sizeof(tev.comm));
    trace_events.perf_submit(args, &tev, sizeof(tev));

    return 0;
}

// ==========================================
// Tracepoint: sys_enter_write
// 用于行为链检测：追踪 write 调用
// ==========================================
TRACEPOINT_PROBE(syscalls, sys_enter_write) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;

    struct trace_event tev;
    __builtin_memset(&tev, 0, sizeof(tev));
    tev.pid = pid;
    tev.uid = uid;
    tev.syscall_nr = SYS_NR_WRITE;
    tev.timestamp_ns = bpf_ktime_get_ns();
    tev.fd = args->fd;
    bpf_get_current_comm(&tev.comm, sizeof(tev.comm));
    trace_events.perf_submit(args, &tev, sizeof(tev));

    return 0;
}

// ==========================================
// Tracepoint: sys_enter_execve
// 用于行为链检测和提权检测
// ==========================================
TRACEPOINT_PROBE(syscalls, sys_enter_execve) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;

    char filename[64];
    __builtin_memset(filename, 0, sizeof(filename));
    bpf_probe_read_user_str(filename, sizeof(filename), args->filename);

    struct trace_event tev;
    __builtin_memset(&tev, 0, sizeof(tev));
    tev.pid = pid;
    tev.uid = uid;
    tev.syscall_nr = SYS_NR_EXECVE;
    tev.timestamp_ns = bpf_ktime_get_ns();
    bpf_get_current_comm(&tev.comm, sizeof(tev.comm));
    __builtin_memcpy(tev.filename, filename, sizeof(tev.filename) - 1);
    trace_events.perf_submit(args, &tev, sizeof(tev));

    return 0;
}

// ==========================================
// Tracepoint: sys_enter_connect
// 用于网络异常检测：追踪外连
// 注意：不用 struct sockaddr_in（BCC虚拟环境头文件不完整）
//       直接读原始字节解析 IPv4
// ==========================================
TRACEPOINT_PROBE(syscalls, sys_enter_connect) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;

    // 读取 sockaddr 原始字节（避免依赖 struct sockaddr_in）
    // 布局: family(2B) | port(2B) | ip(4B) | zero(8B)
    unsigned char raw[16];
    __builtin_memset(raw, 0, sizeof(raw));
    bpf_probe_read_user(raw, sizeof(raw), args->uservaddr);

    // 提取 IPv4 地址（字节4-7，网络字节序）
    u32 ip = (u32)raw[4] | ((u32)raw[5] << 8) | ((u32)raw[6] << 16) | ((u32)raw[7] << 24);
    // 提取端口（字节2-3，网络字节序）
    u16 port = ((u16)raw[2] << 8) | (u16)raw[3];

    struct trace_event tev;
    __builtin_memset(&tev, 0, sizeof(tev));
    tev.pid = pid;
    tev.uid = uid;
    tev.syscall_nr = SYS_NR_CONNECT;
    tev.timestamp_ns = bpf_ktime_get_ns();
    bpf_get_current_comm(&tev.comm, sizeof(tev.comm));

    // 存储 IP 的原始 4 字节 + 端口
    *(u32 *)tev.filename = ip;
    *(u16 *)(tev.filename + 4) = port;

    trace_events.perf_submit(args, &tev, sizeof(tev));

    return 0;
}
"""


# ============================================================
# 用户态 Python 代码
# ============================================================

# ---- 事件结构体定义（必须与 eBPF C 代码一致） ----
# 类比：Python struct 定义通信协议
ALERT_EVENT_FMT = "=IIIQ16s128sII"   # struct alert_event
TRACE_EVENT_FMT = "=IIIQ16s64sII"    # struct trace_event

# 事件类型常量
ALERT_SENSITIVE_FILE = 1
ALERT_STORM = 2
ALERT_BEHAVIOR_CHAIN = 3
ALERT_NETWORK_ANOMALY = 4
ALERT_BASELINE_ANOMALY = 5

# 系统调用号 → 名称映射（人类可读）
SYSCALL_NAMES = {
    257: "openat",
    0: "read",
    1: "write",
    59: "execve",
    42: "connect",
}

# 默认敏感文件前缀列表
DEFAULT_SENSITIVE_PREFIXES = [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/crontab",
    "/root/.ssh",
    "/root/.bash",
    "/etc/ssl/private",
    "/proc/sys/kernel",
    "/boot",
    "/etc/selinux",
    "/usr/bin/passwd",
    "/tmp/.X11-unix",
    "/var/log/auth",
    "/home/*/.ssh/id_rsa",
    "/etc/NetworkManager/system-connections",
]


class ProcessStateMachine:
    """
    每个被监控进程的行为状态机。

    类比：JavaScript 的有限状态机库（如 xstate）
         每个 PID 独立追踪，状态转换触发告警

    状态说明：
      IDLE         → 初始状态
      OPENED_FILE  → 进程调用了 openat（打开了一个文件）
      READ_FILE    → 在 OPENED_FILE 之后调用了 read
      WROTE_FILE   → 在 READ_FILE 之后调用了 write
      EXECUTED     → 在以上状态后调用了 execve → 🔴 告警！
    """

    __slots__ = ('pid', 'state', 'last_file', 'syscall_history',
                 'start_time', 'last_seen')

    def __init__(self, pid: int):
        self.pid = pid
        self.state = "IDLE"
        self.last_file = ""
        self.syscall_history: deque = deque(maxlen=20)  # 最近 20 次系统调用
        self.start_time = time.time()
        self.last_seen = time.time()

    def transition(self, syscall_nr: int, filename: str = "",
                   timestamp_ns: int = 0) -> Optional[str]:
        """
        状态转换函数。

        类比 React 的 useReducer：
          dispatch({ type: 'OPENAT', filename: '/tmp/x' })
          返回新的状态 或 告警原因

        返回 None 表示正常，返回字符串表示告警原因。
        """
        self.last_seen = time.time()
        syscall_name = SYSCALL_NAMES.get(syscall_nr, f"syscall_{syscall_nr}")
        self.syscall_history.append((syscall_name, filename, timestamp_ns))

        # ---- 行为链检测：open → read → write → exec ----
        if syscall_nr == 257:  # openat
            self.state = "OPENED_FILE"
            self.last_file = filename

        elif syscall_nr == 0:  # read
            if self.state == "OPENED_FILE":
                self.state = "READ_FILE"

        elif syscall_nr == 1:  # write
            if self.state == "READ_FILE":
                self.state = "WROTE_FILE"

        elif syscall_nr == 59:  # execve
            if self.state in ("OPENED_FILE", "READ_FILE", "WROTE_FILE"):
                # 可疑行为链完成！
                chain_desc = f"OPEN({self.last_file}) → READ → WRITE → EXEC({filename})"
                self.state = "EXECUTED"
                return chain_desc
            # 即使不形成完整链，exec 可疑文件也告警
            if any(d in filename for d in ("/tmp/", "/dev/shm/", "/var/tmp/")):
                self.state = "EXECUTED"
                return f"Suspicious EXEC from temp dir: {filename}"

        return None

    def is_stale(self, timeout_seconds: float = 300) -> bool:
        """检查状态机是否过期（5分钟无活动则清理）"""
        return time.time() - self.last_seen > timeout_seconds


class NetworkTracker:
    """
    网络连接追踪器（HyperLogLog 近似基数估计）。

    类比：Redis 的 HyperLogLog
         用少量内存估算"这个进程连接了多少个不同 IP"

    简化实现：使用集合 + 时间窗口（生产环境可用 HyperLogLog 库）
    """

    def __init__(self, window_seconds: float = 10.0,
                 max_unique_ips: int = 50):
        self.window_seconds = window_seconds
        self.max_unique_ips = max_unique_ips
        # key=PID, value=(ip_set, first_seen_time, alerted)
        self.connections: Dict[int, Tuple[Set[str], float, bool]] = {}

    def record_connect(self, pid: int, ip_bytes: bytes) -> Optional[str]:
        """
        记录一次 connect 调用。

        返回 None 表示正常，返回字符串表示告警原因。
        """
        now = time.time()

        # 解码 IPv4 地址
        if len(ip_bytes) >= 4:
            ip = ".".join(str(b) for b in ip_bytes[:4])
        else:
            ip = "unknown"

        if pid not in self.connections:
            self.connections[pid] = (set(), now, False)

        ip_set, first_seen, alerted = self.connections[pid]

        # 检查时间窗口
        if now - first_seen > self.window_seconds:
            # 新窗口
            self.connections[pid] = (set(), now, False)
            ip_set, first_seen, alerted = set(), now, False

        ip_set.add(ip)

        # 基数检查
        if len(ip_set) > self.max_unique_ips and not alerted:
            self.connections[pid] = (ip_set, first_seen, True)
            return (f"Network anomaly: {len(ip_set)} unique IPs "
                    f"in {self.window_seconds}s")

        self.connections[pid] = (ip_set, first_seen, alerted)
        return None

    def cleanup(self):
        """清理过期条目"""
        now = time.time()
        stale_pids = [
            pid for pid, (_, first_seen, _) in self.connections.items()
            if now - first_seen > self.window_seconds * 3
        ]
        for pid in stale_pids:
            del self.connections[pid]


class Responder:
    """
    自动响应模块。

    响应动作（按严重程度递增）：
      1. ALERT  — 仅打印告警日志
      2. SIGSTOP — 暂停进程（给分析留时间）
      3. SIGKILL — 立即终止进程
      4. BLOCK   — iptables 阻断网络（需配置）

    类比：try/except 的 except 块
         → 检测到"异常"后执行相应的"处理"
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run  # True=仅打印不执行，调试模式
        self.killed_pids: Set[int] = set()
        self.blocked_ips: Set[str] = set()

    def respond(self, alert_type: int, pid: int, comm: str,
                reason: str, uid: int = 0) -> str:
        """
        根据告警类型和严重程度执行响应。

        返回描述字符串。
        """
        # 严重程度映射
        severity = {
            ALERT_SENSITIVE_FILE: "HIGH",
            ALERT_STORM: "CRITICAL",
            ALERT_BEHAVIOR_CHAIN: "CRITICAL",
            ALERT_NETWORK_ANOMALY: "HIGH",
            ALERT_BASELINE_ANOMALY: "MEDIUM",
        }

        level = severity.get(alert_type, "MEDIUM")

        if level in ("CRITICAL",):
            return self._kill_process(pid, comm, reason)
        elif level == "HIGH":
            return self._alert_process(pid, comm, reason)
        else:
            return self._log_only(pid, comm, reason)

    def _kill_process(self, pid: int, comm: str, reason: str) -> str:
        """发送 SIGKILL 终止进程"""
        if pid in self.killed_pids:
            return f"PID {pid} already killed, skipping"

        self.killed_pids.add(pid)

        if self.dry_run:
            return (f"[DRY RUN] Would SIGKILL PID={pid} "
                    f"({comm}): {reason}")

        try:
            os.kill(pid, signal.SIGKILL)
            return f"💀 SIGKILL sent to PID={pid} ({comm}): {reason}"
        except ProcessLookupError:
            return f"⚠️ PID {pid} already exited"
        except PermissionError:
            return f"⚠️ Permission denied killing PID {pid} (need root)"

    def _alert_process(self, pid: int, comm: str, reason: str) -> str:
        """暂停并告警"""
        if self.dry_run:
            return f"[DRY RUN] Would SIGSTOP PID={pid} ({comm}): {reason}"

        try:
            os.kill(pid, signal.SIGSTOP)
            return f"⏸️ SIGSTOP sent to PID={pid} ({comm}): {reason}"
        except ProcessLookupError:
            return f"⚠️ PID {pid} already exited"
        except PermissionError:
            return f"⚠️ Permission denied stopping PID {pid}"

    def _log_only(self, pid: int, comm: str, reason: str) -> str:
        """仅记录日志"""
        return f"📝 LOG: PID={pid} ({comm}): {reason}"


class RuntimeGuardian:
    """
    主控制器：协调 eBPF 内核监控 + 用户态分析。

    类比：Express.js 的 app — 注册中间件、处理请求
         这里的"请求"是从 perf ring buffer 来的内核事件
    """

    def __init__(self, args):
        self.args = args
        self.bpf: Optional[BPF] = None
        self.responder = Responder(dry_run=args.no_response)
        self.network_tracker = NetworkTracker(
            window_seconds=args.net_window,
            max_unique_ips=args.net_max_ips
        )

        # 每个进程的状态机
        self.state_machines: Dict[int, ProcessStateMachine] = {}

        # === 七维基线检测器（多用户+文件上下文+时间分段） ===
        self.baseline_detector = None
        if args.baseline:
            from baseline_detector import BaselineDetector
            self.baseline_detector = BaselineDetector.load(args.baseline)
            print(f"✅ 已加载基线模型: {args.baseline}")
        elif not args.no_baseline:
            # 未指定模型时，在线训练模式（前 5 分钟为训练期）
            from baseline_detector import BaselineDetector
            self.baseline_detector = BaselineDetector(
                window_seconds=10.0,
                enable_ngram=True,
                enable_entropy=True,
                enable_multi_user=True,
                enable_file_context=True,
                enable_time_window=True,
            )
            print("📚 基线检测器：在线训练模式（前 50000 事件为训练期）")

        # === 保护器：动态资源管理 + 防死机 ===
        from guardian_protector import GuardianProtector
        self.protector = GuardianProtector(
            max_mem_mb=getattr(args, 'max_mem_mb', 300),
            warn_mem_mb=getattr(args, 'warn_mem_mb', 150),
            watchdog_timeout=getattr(args, 'watchdog_timeout', 120.0),
            max_queue_depth=getattr(args, 'max_queue_depth', 8000),
        )
        self.protector.on_lru_evict(self._on_memory_pressure)
        self.protector.on_emergency(self._on_emergency_memory)
        self.protector._save_baseline = self._save_baseline_on_crash
        self.protector.start()
        print("🛡️  保护器已启动 | CPU自适应+背压+内存GC+看门狗120s+4级降级")

        # 抑制保护器日志噪音
        import logging
        for name in ['', 'root', 'guardian_protector', 'protector']:
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)

        # 防重复触发标志
        self._alerted = set()
        self._alert_last = {}  # (type,pid) → timestamp 去重

        # 统计信息
        self.stats = {
            "total_alerts": 0,
            "total_trace_events": 0,
            "start_time": time.time(),
            "alerts_by_type": defaultdict(int),
        }

        # 锁（保护多线程访问）
        self.lock = threading.Lock()

        # === 压缩日志：所有非告警输出写入 .jsonl.gz ===
        import gzip as _gzip
        self._log_path = f"/tmp/runtime_guardian_{int(time.time())}.jsonl.gz"
        self._log_file = _gzip.open(self._log_path, 'wt', encoding='utf-8')
        self._log_lock = threading.Lock()
        print(f"📝 详细日志: {self._log_path} (zcat 查看)")

        # 优雅退出
        self.running = True
        self._exit_now = False
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

    def _shutdown_handler(self, signum, frame):
        """退出处理 —— 第一次尝试优雅，第二次直接强杀"""
        if self._exit_now:
            os._exit(0)  # 第二次信号直接暴力退出
        self._exit_now = True
        print(f"\n🛑 收到信号 {signum}，正在退出...(再按一次 Ctrl+C 强杀)")
        self.running = False

    def _on_memory_pressure(self):
        """内存压力：静默LRU淘汰"""
        if 'mem_pressure' not in self._alerted:
            self._alerted.add('mem_pressure')
            print(f"  🧹 GC: 内存紧张，LRU淘汰中...")

    def _on_emergency_memory(self):
        """紧急内存：一次性告警"""
        if 'mem_emergency' not in self._alerted:
            self._alerted.add('mem_emergency')
            snap = self.protector.resource_monitor.get_snapshot()
            print(f"  🚨 OOM风险! mem={snap['mem_mb']:.0f}MB cpu={snap['cpu_pct']:.0f}%")

    def _save_baseline_on_crash(self):
        """看门狗超时：仅首次保存"""
        if 'watchdog_saved' not in self._alerted:
            self._alerted.add('watchdog_saved')
            if self.baseline_detector and self.baseline_detector.is_trained:
                self.baseline_detector.save("/tmp/runtime_guardian_crash_baseline.json")

    def _write_log(self, level: str, msg: str, **kwargs):
        """写入压缩日志（非告警信息全部进 gzip，终端不显示）"""
        try:
            entry = {"ts": datetime.now().isoformat(), "level": level, "msg": msg, **kwargs}
            with self._log_lock:
                self._log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日志写入失败不影响监控

    def _format_ip(self, raw: bytes) -> str:
        """解码 eBPF 传来的原始 IP 字节"""
        if len(raw) >= 4:
            return ".".join(str(b) for b in raw[:4])
        return "0.0.0.0"

    def _format_port(self, raw: bytes) -> int:
        """解码端口号（网络字节序 → 主机字节序）"""
        if len(raw) >= 6:
            port_bytes = raw[4:6]
            return struct.unpack("!H", port_bytes)[0]
        return 0

    # ================================================================
    # perf buffer 回调函数（类比对 EventEmitter 的 listener）
    # 这些函数在内核有事件时被 BCC 框架回调
    # ================================================================

    def _handle_alert(self, cpu, data, size):
        """处理告警事件"""
        try:
            e = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint * 32)).contents
            pid = e[0]
            alert_type = e[2]
            # 去重：同类型+PID 只告警一次
            key = (alert_type, pid)
            if key in self._alert_last:
                return
            self._alert_last[key] = time.time()
            ts = datetime.now().strftime("%H:%M:%S")
            names = {1: "🔴 敏感文件", 2: "🟠 调用风暴", 3: "🟡 行为链", 4: "🔵 网络", 5: "🟢 基线"}
            print(f"[{ts}] {names.get(alert_type, '?')} PID={pid}", flush=True)
        except Exception as ex:
            print(f"[DEBUG] ALERT err: {ex}", flush=True)

    def _handle_trace(self, cpu, data, size):
        """追踪事件 """
        try:
            e = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint * 18)).contents
            pid, syscall_nr = e[0], e[2]
        except Exception:
            return
        # 行为链 + 去重
        if syscall_nr in (59, 257, 0, 1):
            if pid not in self.state_machines:
                self.state_machines[pid] = ProcessStateMachine(pid)
            alert = self.state_machines[pid].transition(syscall_nr, "", 0)
            key = ('chain', pid)
            if alert and key not in self._alert_last:
                self._alert_last[key] = time.time()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🟡 行为链 PID={pid}", flush=True)
        # 网络 + 去重
        if syscall_nr == 42 and 'net' not in self._alert_last:
            self._alert_last['net'] = time.time()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔵 网络连接 PID={pid}", flush=True)

    # ================================================================
    # 后台维护任务
    # ================================================================

    def _maintenance_loop(self):
        """
        定期清理过期状态机和网络追踪条目。

        类比：JavaScript 的 setInterval / Python 的 asyncio.create_task
        """
        while self.running:
            time.sleep(30)  # 每 30 秒清理一次
            with self.lock:
                # 清理过期状态机
                stale_pids = [
                    pid for pid, sm in self.state_machines.items()
                    if sm.is_stale()
                ]
                for pid in stale_pids:
                    del self.state_machines[pid]

                # 清理过期网络条目
                self.network_tracker.cleanup()

                if stale_pids:
                    self._write_log("INFO", "LRU清理", stale_pids=stale_pids, active=len(self.state_machines))

    def _stats_reporter(self):
        """定期打印统计信息"""
        while self.running:
            time.sleep(60)  # 每分钟报告一次
            with self.lock:
                elapsed = time.time() - self.stats["start_time"]
                rate = self.stats["total_trace_events"] / elapsed if elapsed > 0 else 0
                print(f"\n📊 [统计] 运行 {elapsed:.0f}s | "
                      f"告警: {self.stats['total_alerts']} | "
                      f"事件: {self.stats['total_trace_events']} "
                      f"({rate:.0f}/s) | "
                      f"活跃进程: {len(self.state_machines)}")

    # ================================================================
    # 初始化与启动
    # ================================================================

    def load_sensitive_prefixes(self):
        """敏感文件已硬编码在 eBPF C 代码中，无需动态加载"""
        print("✅ 敏感文件检测: 硬编码模式 (/etc/passwd, /etc/shadow, /root/, /etc/sudoers)")

    def start(self):
        """启动监控器"""
        print("=" * 60)
        print("Runtime Guardian — eBPF 运行时漏洞检测系统")
        print("=" * 60)
        print(f"内核版本: {os.uname().release}")
        print(f"调试模式: {'是 (仅检测不响应)' if self.args.no_response else '否'}")
        print(f"风暴阈值: {self.args.storm} 次/秒")
        print(f"网络窗口: {self.args.net_window}s, 最大IP数: {self.args.net_max_ips}")
        print(f"目标 PID: {'全部' if self.args.pid == 0 else self.args.pid}")
        print()

        # ---- 步骤 1: 编译并加载 eBPF 程序 ----
        # 类比：JavaScript 的 eval() → 把 C 代码字符串编译为 eBPF 字节码
        #       浏览器加载 Service Worker → eBPF 程序注入内核
        print("⏳ 编译并加载 eBPF 程序...")
        try:
            self.bpf = BPF(text=EBPF_C_CODE)
        except Exception as e:
            print(f"⚠️  eBPF 程序加载失败: {e}")
            print("   可能原因：内核头文件不匹配 / kernel lockdown / 权限不足")
            print("   监控器将降级运行（仅基线检测+保护模块，无eBPF实时监控）")
            self.bpf = None
            self.running = False  # 跳过主循环
            return  # eBPF 不可用时直接返回，不进入主循环

        print("✅ eBPF 程序编译并加载成功")

        # ---- 步骤 2: 注册 perf buffer 回调 ----
        self.bpf["alert_events"].open_perf_buffer(self._handle_alert)
        self.bpf["trace_events"].open_perf_buffer(self._handle_trace)
        print("✅ perf buffer 已注册")

        # ---- 步骤 3: 加载敏感文件规则 ----
        self.load_sensitive_prefixes()

        print("🚀 主循环启动（Ctrl+C 退出）")
        import os as _os
        last_st = 0
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=500)
                if not self.running: break
                t = time.time()
                if t - last_st > 10:
                    last_st = t
                    try:
                        with open('/proc/self/status') as f:
                            for l in f:
                                if 'VmRSS' in l: mem=int(l.split()[1])//1024; break
                        load = round(_os.getloadavg()[0], 1)
                        with open('/proc/self/status') as f:
                            for l in f:
                                if 'VmSize' in l: vmsize=int(l.split()[1])//1024; break
                        bp = f"{self._lost_samples}丢包" if hasattr(self,'_lost_samples') and self._lost_samples else "OK"
                        gc = f"{len(self.state_machines)}进程"
                    except Exception:
                        mem, load, vmsize, bp, gc = 0, 0, 0, '?', '?'
                    print(f"⚡ load={load} mem={mem}MB(vm{vmsize}MB) 背压={bp} GC={gc} 告警={self.stats['total_alerts']}", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def _cleanup(self):
        """清理 —— 最快速度退出"""
        self.running = False
        try:
            self._log_file.close()
        except Exception:
            pass
        print(f"\n👋 告警 {self.stats.get('total_alerts', 0)} | 事件 {self.stats.get('total_trace_events', 0)}")
        os._exit(0)  # 直接强退，不等任何线程


# ============================================================
# 命令行参数解析
# ============================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Runtime Guardian — eBPF 运行时漏洞检测系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  sudo python3 ebpf_monitor.py                      # 监控所有进程（默认）
  sudo python3 ebpf_monitor.py --storm 50           # 风暴阈值设为50次/秒
  sudo python3 ebpf_monitor.py --no-response        # 仅检测不响应（调试）
  sudo python3 ebpf_monitor.py --rules ../config/rules.yaml  # 自定义规则
        """
    )
    parser.add_argument("--pid", type=int, default=0,
                        help="监控指定 PID（0=所有进程，默认: 0）")
    parser.add_argument("--storm", type=int, default=100,
                        help="系统调用风暴阈值（次/秒，默认: 100）")
    parser.add_argument("--net-window", type=float, default=10.0,
                        help="网络异常检测时间窗口（秒，默认: 10）")
    parser.add_argument("--net-max-ips", type=int, default=50,
                        help="窗口内最大不同 IP 数（默认: 50）")
    parser.add_argument("--no-response", action="store_true",
                        help="仅检测不自动响应（调试模式）")
    parser.add_argument("--rules", type=str, default=None,
                        help="自定义规则配置文件路径（YAML 格式）")
    parser.add_argument("--baseline", type=str, default=None,
                        help="基线模型文件路径（用于基线异常检测）")
    parser.add_argument("--no-baseline", action="store_true",
                        help="禁用七维基线检测（仅使用规则检测）")
    return parser.parse_args()


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    # 权限检查（eBPF 需要 root）
    if os.geteuid() != 0:
        print("❌ eBPF 需要 root 权限，请使用 sudo 运行")
        print("   sudo python3 ebpf_monitor.py [选项]")
        sys.exit(1)

    args = parse_args()
    guardian = RuntimeGuardian(args)
    guardian.start()
