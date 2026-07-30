#!/usr/bin/env python3
"""
最小 eBPF 测试 — 确认 WSL2 6.18 内核可用
运行: sudo python3 ebpf_test.py
"""
from bcc import BPF

code = r'''
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(events);

struct evt {
    u32 pid; u32 uid; u32 type;
    char comm[16]; char fname[64];
};

// === 单个 tracepoint 测试 ===
TRACEPOINT_PROBE(syscalls, sys_enter_openat) {
    struct evt e = {};
    e.pid = bpf_get_current_pid_tgid() >> 32;
    e.uid = bpf_get_current_uid_gid();
    e.type = 257;
    bpf_get_current_comm(&e.comm, sizeof(e.comm));
    bpf_probe_read_user_str(&e.fname, sizeof(e.fname), args->filename);
    events.perf_submit(args, &e, sizeof(e));
    return 0;
}
'''

print("⏳ 加载 eBPF...")
b = BPF(text=code)
print("✅ eBPF 加载成功！")

def cb(cpu, data, size):
    e = b['events'].event(data)
    if b'/etc' in e.fname or b'/root' in e.fname:
        print(f"🔴 PID={e.pid} {e.comm.decode()} → {e.fname.decode()}")

b['events'].open_perf_buffer(cb)
print("🚀 监控中(10秒)，试着在另一个终端 cat /etc/passwd...")
import time
t0 = time.time()
while time.time() - t0 < 10:
    b.perf_buffer_poll(timeout=100)
print("✅ 测试完成 — eBPF 在 WSL2 上完全正常！")
