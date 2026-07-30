# 从 strace 到 eBPF：渐进学习路线图

> 目标读者：熟悉 Python/JavaScript，用过 strace，想升级到 eBPF 但被 C 语言吓到的开发者。

---

## 🗺️ 总览：五阶段路线图

```
阶段1: eBPF 心智模型      (1天)   ← 你现在在这里
阶段2: bpftrace 体验       (2天)   ← 像"eBPF 的 awk"，一行命令搞定
阶段3: BCC Python 绑定     (1周)   ← Python 写用户态，C 写内核态
阶段4: libbpf + CO-RE     (2周)   ← 生产级方案，一次编译到处运行
阶段5: 深入内核           (持续)   ← 看内核源码，写自己的 eBPF 程序
```

---

## 阶段 1：eBPF 心智模型（类比学习）

### 1.1 把 eBPF 理解为一个"内核里的 Service Worker"

如果你是前端开发者，这样理解：

| 概念 | 前端 Service Worker | eBPF |
|------|-------------------|------|
| 运行位置 | 浏览器后台线程 | Linux 内核 |
| 触发方式 | fetch 事件、push 事件 | 系统调用、网络包、函数入口 |
| 通信方式 | postMessage | eBPF Map (共享内存) |
| 安全沙箱 | 同源策略 | eBPF Verifier（验证器） |
| 生命周期 | 独立于页面 | 独立于进程 |

**核心类比**：就像 Service Worker 可以拦截浏览器的所有网络请求（`fetch` 事件），eBPF 可以拦截内核的所有系统调用（`sys_enter_*` 事件）。

### 1.2 Python 类比：eBPF = 内核里的 `@tracepoint_decorator`

```python
# 如果你用 Python 写过装饰器，这就是 eBPF 的本质：

# Python 版（概念演示，并非真实代码）
@hook_kernel_function("sys_openat")       # ← 挂载点：内核函数名
def my_eBPF_program(ctx):                  # ← ctx = 上下文（寄存器、参数）
    filename = read_user_memory(ctx.arg2)  # ← 从用户空间内存读取参数
    if filename == "/etc/passwd":
        send_alert_to_userspace(pid=ctx.pid, file=filename)
    return ALLOW  # 或 BLOCK（取决于挂载点类型）
```

**关键差异**：
- Python 装饰器工作在函数调用层 → eBPF 工作在**系统调用层**
- Python 可以写任意循环 → eBPF **循环必须编译器可证明会终止**
- Python 有 GC → eBPF **无动态内存分配**（只能用预分配的 Map）

### 1.3 理解三条关键限制（这就是为什么需要学习曲线）

| 限制 | 原因 | 绕过方式 |
|------|------|---------|
| 🔒 不能随意循环 | 验证器必须保证程序在有限时间内退出 | 编译器展开（`#pragma unroll`） |
| 🔒 栈只有 512 字节 | 内核上下文切换不允许大栈 | 用 Map 存储大数据 |
| 🔒 不能调用任意内核函数 | 防止破坏内核稳定性 | 只用 `bpf_helper` 白名单函数 |

---

## 阶段 2：bpftrace —— "eBPF 的 awk"

### 2.1 安装

```bash
sudo apt-get install bpftrace
```

### 2.2 一行命令体验 eBPF 威力

```bash
# 1. 监控所有 openat 调用（你的 strace 版本需要几百行 Python）
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_openat { printf("%s %s\n", comm, str(args->filename)); }'

# 2. 统计每个进程的系统调用次数（类似你的"调用风暴检测"）
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_* { @[comm, probe] = count(); }'

# 3. 检测谁在访问 /etc/passwd（类似你的"敏感文件检测"）
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_openat /str(args->filename) == "/etc/passwd"/ { printf("ALERT: %s (PID %d) accessing shadow!\n", comm, pid); }'

# 4. 追踪进程的 exec 调用链（类似你的"行为链检测"）
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_execve { printf("%s (parent:%d) exec: %s\n", comm, pid, str(args->filename)); }'
```

### 2.3 bpftrace 脚本版（保存为 .bt 文件）

```bash
# 保存为 detect_sensitive_files.bt
# 功能：完全等价于你的 strace 版敏感文件检测，但性能高 100 倍

BEGIN {
    printf("===== 敏感文件访问监控启动 =====\n");
    // 敏感文件列表（用 BPF 的硬编码方式，因为 bpftrace 不支持动态数组）
    @sensitive["/etc/passwd"] = 1;
    @sensitive["/etc/shadow"] = 1;
    @sensitive["/etc/sudoers"] = 1;
    @sensitive["/root/.ssh/id_rsa"] = 1;
    @sensitive["/etc/crontab"] = 1;
}

tracepoint:syscalls:sys_enter_openat
{
    $fname = str(args->filename);
    if (@sensitive[$fname] == 1) {
        time("%H:%M:%S ");
        printf("🔴 敏感文件访问! PID=%d COMM=%s FILE=%s\n",
               pid, comm, $fname);
    }
}

END {
    clear(@sensitive);
}
```

运行：`sudo bpftrace detect_sensitive_files.bt`

### 2.4 本阶段里程碑

- [ ] 能用 bpftrace 一行命令替换 strace 的监控功能
- [ ] 理解 `tracepoint:syscalls:sys_enter_*` 的命名规则
- [ ] 理解 `args->filename` 等参数访问方式
- [ ] 能用 bpftrace 的 `@map[key] = count()` 做聚合统计

---

## 阶段 3：BCC Python 绑定 —— "生产级 strace 替代品"

### 3.1 心智模型

```
┌──────────────────────────────────────────┐
│          你的 Python 代码                  │
│  ┌────────────────────────────────────┐   │
│  │  b = BPF(text=eBPF_C_CODE)         │   │  ← Python 字符串里嵌入 C 语言 eBPF 代码
│  │  b.attach_tracepoint(...)          │   │  ← 挂载到内核事件
│  │  b["events"].open_perf_buffer(...) │   │  ← 打开环形缓冲区
│  │  b.perf_buffer_poll()              │   │  ← 事件循环（类似 asyncio.run()）
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
                    ↕
┌──────────────────────────────────────────┐
│         eBPF C 代码（内核态运行）           │
│  ┌────────────────────────────────────┐   │
│  │  BPF_PERF_OUTPUT(events);          │   │  ← 声明输出通道
│  │  int on_openat(struct tracepoint...)│   │  ← 事件处理器
│  │  {                                  │   │
│  │      bpf_probe_read_user_str(...);  │   │  ← 安全读取用户内存
│  │      events.perf_submit(ctx, ...);  │   │  ← 推送到用户态
│  │      return 0;                      │   │
│  │  }                                  │   │
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

### 3.2 C 语言速成（仅限 eBPF 需要的部分）

**如果你会 TypeScript，C 的类型声明只是换个写法：**

```typescript
// TypeScript
let count: number = 0;
let name: string = "hello";
let flags: number = O_RDONLY;
const MAX_SIZE: number = 256;

function check_file(pid: number, filename: string): number {
    if (filename === "/etc/passwd") return 1;
    return 0;
}
```

```c
// C 语言（eBPF 中）
u32 count = 0;                          // u32 = 无符号 32 位整数
char name[256] = "hello";               // 没有 string 类型，只有 char 数组
int flags = O_RDONLY;                   // int = 有符号 32 位整数
#define MAX_SIZE 256                     // 宏定义，类似 const（但不占内存）

int check_file(u32 pid, const char *filename) {
    // 没有 === ，没有 .equals()
    // 需要逐个字符比较（或使用 __builtin_memcmp）
    if (filename[0] == '/' && filename[1] == 'e')  // 简化示例
        return 1;
    return 0;
}
```

**关键差异表：**

| 操作 | Python | C (eBPF) |
|------|--------|----------|
| 字符串比较 | `s == "/etc/passwd"` | 逐字符比较或 `__builtin_memcmp` |
| 数组 | `arr = [1,2,3]` | `int arr[3] = {1,2,3};` |
| 字典 | `d = {"a": 1}` | 用 `BPF_HASH` Map |
| 打印 | `print(x)` | `bpf_trace_printk("fmt", x)` |
| 读用户内存 | `os.read(fd)` | `bpf_probe_read_user_str(buf, sizeof(buf), ptr)` |

### 3.3 BCC 最小可运行示例

```python
#!/usr/bin/env python3
"""最小的 BCC eBPF 程序 —— 监控所有 openat 调用"""
from bcc import BPF

# ===== 内核态 eBPF 代码（C 语言，字符串形式） =====
bpf_c_code = """
#include <uapi/linux/ptrace.h>

// 声明一个 perf ring buffer，用于向用户态推送事件
BPF_PERF_OUTPUT(events);

// 定义事件数据结构（内核态和用户态必须一致）
struct event_data {
    u32 pid;          // 进程 ID
    u32 uid;          // 用户 ID
    char comm[16];    // 进程名（类似 Node.js 的 process.title）
    char filename[256]; // 文件名
};

// 这是"事件处理器"：每次有进程调用 openat() 时，内核会调用这个函数
TRACEPOINT_PROBE(syscalls, sys_enter_openat) {
    struct event_data data = {};

    // 填充基础信息
    data.pid = bpf_get_current_pid_tgid() >> 32;  // 高32位是PID
    data.uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    bpf_get_current_comm(&data.comm, sizeof(data.comm));

    // 安全地读取用户空间的文件名字符串
    // 类比：Python 的 os.read(fd)，但带边界检查
    bpf_probe_read_user_str(&data.filename, sizeof(data.filename),
                             args->filename);

    // 把事件推送到用户态的 ring buffer
    // 类比：JavaScript 的 eventEmitter.emit('openat', data)
    events.perf_submit(args, &data, sizeof(data));
    return 0;
}
"""

# ===== 用户态 Python 代码 =====
def handle_event(cpu, data, size):
    """事件回调函数 —— 类比 JavaScript 的 eventHandler"""
    event = b["events"].event(data)
    print(f"[PID={event.pid}] {event.comm.decode()} → {event.filename.decode()}")

# 加载 eBPF 程序（编译 + 验证 + 注入内核）
b = BPF(text=bpf_c_code)

# 注册回调函数（类比 addEventListener）
b["events"].open_perf_buffer(handle_event)

print("eBPF 监控已启动，按 Ctrl+C 退出...")
while True:
    try:
        b.perf_buffer_poll()  # 事件循环（类比 asyncio.get_event_loop().run_forever()）
    except KeyboardInterrupt:
        break
```

### 3.4 本阶段里程碑

- [ ] 能在 WSL2 中成功运行上述示例
- [ ] 理解 `struct event_data` 是内核-用户态的"通信协议"
- [ ] 理解 `bpf_probe_read_user_str` 为什么必须用（防止内核崩溃）
- [ ] 能用 BCC 改写你的 strace MVP 的三个检测功能

---

## 阶段 4：libbpf + CO-RE —— 生产级方案

### 4.1 为什么需要这个阶段

| 问题 | BCC 方案 | libbpf + CO-RE 方案 |
|------|---------|-------------------|
| 依赖 | 运行时需要 LLVM/Clang 编译 | 预编译，无需编译器 |
| 内存 | Python 运行时 ~50MB | 纯 C，~2MB |
| 启动速度 | 编译 eBPF 代码 2-5秒 | 直接加载，<100ms |
| 跨内核 | 依赖当前内核头文件 | CO-RE 自适应不同内核版本 |
| 适用场景 | 开发/调试 | **生产部署 / 鸿蒙设备** |

### 4.2 学习路径

```bash
# 1. 安装 libbpf 开发包
sudo apt-get install libbpf-dev

# 2. 克隆官方示例（最佳学习材料）
git clone https://github.com/libbpf/libbpf-bootstrap
cd libbpf-bootstrap/examples/c

# 3. 编译运行最小示例
make minimal
sudo ./minimal
```

### 4.3 与 BCC 的区别（代码对比）

**BCC 方式**（Python + 内嵌 C）：
```python
b = BPF(text=bpf_c_code)   # ← C 代码在 Python 字符串里
```

**libbpf 方式**（纯 C，编译为 .o 文件）：
```c
// ebpf_prog.c —— 编译为 ebpf_prog.bpf.o
SEC("tracepoint/syscalls/sys_enter_openat")
int handle_openat(struct trace_event_raw_sys_enter *ctx) {
    // ... 同样的逻辑
}
```

```c
// loader.c —— 用户态加载器
struct runtime_guardian_bpf *skel = runtime_guardian_bpf__open_and_load();
runtime_guardian_bpf__attach(skel);
// ... 从 ring buffer 读取
```

### 4.4 本阶段里程碑

- [ ] 能编译运行 libbpf-bootstrap 的 minimal 示例
- [ ] 理解 CO-RE（Compile Once, Run Everywhere）的原理
- [ ] 能将 BCC 版代码改写为 libbpf 版

---

## 阶段 5：深入 —— 进阶主题

### 5.1 推荐学习顺序

1. **Linux 内核源码阅读**（仅你需要了解的部分）
   - `fs/open.c` → `do_sys_openat2()` — 理解 openat 的实现
   - `include/linux/syscalls.h` — 系统调用声明
   - 不需要全读，只需要"按图索骥"

2. **eBPF 进阶技术**
   - `BPF_MAP_TYPE_LRU_HASH` — 自动淘汰旧数据的 Map（适合你的"调用风暴"统计）
   - `bpf_loop()` — Linux 5.17+ 支持的受限循环
   - `bpf_tail_call()` — eBPF 程序的"尾调用"，突破指令数限制
   - `BPF_LSM` — Linux Security Module hook，可以做**主动拦截**

3. **性能优化**
   - `BPF_RINGBUF_OUTPUT` — 新一代 ring buffer（比 perf buffer 快 2-4x）
   - 内核态聚合（在 eBPF 里做统计，减少用户态数据量）

### 5.2 关键内核源码位置（按需查阅）

```
kernel/
├── fs/open.c                    ← open/openat 实现
├── fs/read_write.c              ← read/write 实现
├── fs/exec.c                    ← execve 实现
├── net/socket.c                 ← connect/accept 实现
├── kernel/bpf/verifier.c        ← eBPF 验证器（理解为什么你的代码被拒绝）
├── include/uapi/linux/bpf.h     ← bpf_helper 函数列表
└── tools/lib/bpf/               ← libbpf 库源码
```

---

## 📋 你的具体迁移计划

### 第 1 周：bpftrace 复刻现有功能

```
Day 1-2: 安装 bpftrace，用一行命令复刻敏感文件检测
Day 3-4: 用 bpftrace 脚本复刻调用风暴检测
Day 5:   用 bpftrace 脚本复刻行为链检测
```

### 第 2 周：BCC Python 重写

```
Day 1-2: 运行阶段3的最小示例，理解每个函数的作用
Day 3-4: 将敏感文件检测改写为 BCC 版
Day 5-6: 将调用风暴检测改写为 BCC 版
Day 7:   将行为链检测改写为 BCC 版
```

### 第 3-4 周：扩展功能

```
Week 3: 网络异常检测（connect 跟踪 + HyperLogLog 基数估计）
Week 4: 基线异常检测（统计建模，参见 baseline_detector.py）
```

### 第 5-8 周：生产化和鸿蒙适配

```
Week 5-6: 迁移到 libbpf + CO-RE
Week 7-8: 鸿蒙标准系统适配测试
```

---

## 🛠️ 调试技巧

### eBPF 程序的"console.log"

```c
// eBPF 中的打印（会输出到 /sys/kernel/debug/tracing/trace_pipe）
bpf_trace_printk("PID=%d, file=%s\\n", pid, filename);

// 在另一个终端查看输出
sudo cat /sys/kernel/debug/tracing/trace_pipe
```

### 验证器错误的解读

```
# 如果 eBPF 程序加载失败，BCC 会输出验证器日志
# 常见错误:
# "invalid access to packet"  → 指针越界了
# "back-edge from insn X to Y" → 有循环，需要用 #pragma unroll
# "cannot call GPL-restricted function" → 用了非 GPL 的 helper

# 查看详细日志
sudo cat /sys/kernel/debug/tracing/trace_pipe  # 同时看 bpf_trace_printk 输出
```

---

## 📚 推荐资源

| 资源 | 适合阶段 | 说明 |
|------|---------|------|
| [bcc Reference Guide](https://github.com/iovisor/bcc/blob/master/docs/reference_guide.md) | 阶段3 | BCC Python API 完整参考 |
| [bpftrace One-Liners](https://github.com/iovisor/bpftrace#examples) | 阶段2 | 官方一行命令示例集 |
| [libbpf-bootstrap](https://github.com/libbpf/libbpf-bootstrap) | 阶段4 | 生产级 eBPF 应用模板 |
| [eBPF 入门实践](https://eunomia.dev/tutorials/) | 阶段1 | 中文教程，有在线 Playground |
| 《Linux内核观测技术BPF》| 阶段3-5 | David Calavera 著，有中文版 |
