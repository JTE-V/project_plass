# Runtime Guardian — 运行时漏洞检测与主动防御系统

> **核心理念**：不看代码像不像漏洞，而是监控程序**实际做了什么**。

## 架构概览

```
┌─────────────────────────────────────────────────────┐
│                    用户态 (Python)                    │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ 状态机    │  │ 基线异常检测  │  │  自动响应      │  │
│  │ 检测引擎  │  │ (统计模型)   │  │ (SIGKILL等)   │  │
│  └────┬─────┘  └──────┬───────┘  └───────┬───────┘  │
│       │               │                  │          │
│  ┌────┴───────────────┴──────────────────┴────┐     │
│  │         perf ring buffer 事件流             │     │
│  └────────────────────┬───────────────────────┘     │
├───────────────────────┼─────────────────────────────┤
│                  内核态 (eBPF)                       │
│  ┌────────────────────┴───────────────────────┐     │
│  │  tracepoint/kprobe 挂钩                    │     │
│  │  - sys_enter_openat  (敏感文件)            │     │
│  │  - sys_enter_read    (行为链)              │     │
│  │  - sys_enter_write   (行为链)              │     │
│  │  - sys_enter_execve  (行为链)              │     │
│  │  - sys_enter_connect (网络异常)            │     │
│  └────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────┘
```

### 检测能力

| 检测类型 | 检测内容 | 原理 |
|---------|---------|------|
| 🔴 敏感文件访问 | 尝试打开 /etc/passwd, /etc/shadow, /root/.ssh 等 | eBPF 在 openat 调用时检查路径 |
| 🟠 系统调用风暴 | 1秒内 open 调用超过阈值 | eBPF 内核态统计 + 用户态二次验证 |
| 🟡 可疑行为链 | open → read → write → exec 序列 | 用户态状态机追踪进程行为序列 |
| 🟢 基线异常 | 偏离正常系统调用频率分布 | 统计学 z-score + EWMA 自适应 |
| 🔵 网络异常 | 短时间内连接大量外网 IP | eBPF connect 跟踪 + 基数估计 |

### 与 strace MVP 的对比

| 维度 | strace 方案 | eBPF 方案（本项目） |
|------|-----------|-------------------|
| 原理 | ptrace 暂停目标进程 | 内核态 hook，不暂停进程 |
| 性能开销 | 每个 syscall 两次上下文切换 | 几乎零开销（JIT 编译的 eBPF 字节码） |
| 过滤能力 | 用户态过滤，事件已产生 | **内核态过滤，99% 事件直接丢弃** |
| 可移植性 | 仅 Linux | Linux 4.x+（WSL2/鸿蒙标准系统均支持） |
| 生产可用 | ❌ 开销太大 | ✅ Facebook/Netflix 同款方案 |

## 快速开始

### 环境要求

- **OS**: Linux 4.18+（WSL2 Ubuntu 20.04+ 完美支持）
- **Python**: 3.8+
- **bcc**: BPF Compiler Collection

### 安装

```bash
# 1. 安装 bcc 工具链
sudo apt-get update
sudo apt-get install -y bpfcc-tools python3-bpfcc linux-headers-$(uname -r)

# 2. 验证 eBPF 可用
sudo python3 -c "from bcc import BPF; print('eBPF ready')"

# 3. 安装 Python 依赖
pip install numpy psutil
```

### 运行

```bash
# 终端1：启动监控器（需要 root）
cd src
sudo python3 ebpf_monitor.py

# 终端2：启动目标进程
node app.js

# 终端3：模拟攻击测试
cd tests
bash simulate_attacks.sh
```

## 项目结构

```
runtime-guardian/
├── README.md                         # 本文件
├── docs/
│   ├── learning-roadmap.md           # eBPF 学习路线图
│   └── harmony-analysis.md           # 鸿蒙安全监控能力分析
├── src/
│   ├── ebpf_monitor.py               # ★ eBPF 监控器（Python+bcc）
│   ├── baseline_detector.py          # ★ 基线异常检测算法
│   └── responder.py                  # 自动响应模块
├── tests/
│   └── simulate_attacks.sh           # 攻击模拟脚本
└── config/
    └── rules.yaml                    # 检测规则配置
```

## 技术栈类比（给 JavaScript/Python 开发者）

| eBPF 概念 | JavaScript/Node.js 类比 | Python 类比 |
|-----------|------------------------|-------------|
| eBPF 程序 | 浏览器 Service Worker | asyncio 协程 |
| eBPF Map | SharedArrayBuffer | multiprocessing.SharedMemory |
| perf ring buffer | EventEmitter / RxJS | asyncio.Queue |
| kprobe/tracepoint | addEventListener('click') | signal.signal() |
| bpf_probe_read | 跨 iframe postMessage | pickle.loads |
| eBPF verifier | TypeScript 类型检查器 | mypy 静态检查 |
