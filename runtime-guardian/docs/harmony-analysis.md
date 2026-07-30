# 鸿蒙 OpenHarmony 安全监控能力全面分析

> 面向 Runtime Guardian 的鸿蒙适配评估 —— 从 eBPF 到分布式审计的完整方案

---

## 目录

1. [背景：Runtime Guardian 当前架构](#1-背景runtime-guardian-当前架构)
2. [OpenHarmony 系统类型速览](#2-openharmony-系统类型速览)
3. [eBPF 在 OpenHarmony 上的可行性分析](#3-ebpf-在-openharmony-上的可行性分析)
4. [替代方案全景图](#4-替代方案全景图)
5. [方案对比矩阵](#5-方案对比矩阵)
6. [推荐的分阶段迁移策略](#6-推荐的分阶段迁移策略)
7. [深度分析：各替代方案详解](#7-深度分析各替代方案详解)
8. [Python/JavaScript 类比参考](#8-pythonjavascript-类比参考)
9. [参考资源](#9-参考资源)

---

## 1. 背景：Runtime Guardian 当前架构

```
                    ┌──────────────────────────────────┐
                    │      用户态 Python 分析引擎        │
                    │  ┌──────────┐  ┌──────────────┐  │
                    │  │ 状态机    │  │ 基线异常检测  │  │
                    │  │ 检测引擎  │  │ (z-score)    │  │
                    │  └────┬─────┘  └──────┬───────┘  │
                    │       └───────┬───────┘          │
                    │    perf ring buffer               │
                    └───────────────┬──────────────────┘
                    ┌───────────────┴──────────────────┐
                    │       内核态 eBPF 程序 (C)         │
                    │  tracepoint/kprobe 挂钩            │
                    │  - sys_enter_openat               │
                    │  - sys_enter_read/write/execve    │
                    │  - sys_enter_connect              │
                    └──────────────────────────────────┘
```

**核心依赖**：Linux 内核 4.18+，`CONFIG_DEBUG_INFO_BTF=y`，BCC/libbpf 工具链。

**类比**（给 Python/JavaScript 开发者的直觉）：
- eBPF 程序 = 内核里的 **Service Worker**（拦截所有"请求"——系统调用）
- perf ring buffer = **asyncio.Queue** / **RxJS Subject**（内核→用户态事件流）
- eBPF Map = **SharedArrayBuffer** / **multiprocessing.SharedMemory**
- eBPF Verifier = **TypeScript 类型检查器** / **mypy 静态分析**（保证安全后才能注入内核）

---

## 2. OpenHarmony 系统类型速览

OpenHarmony 分为三类系统，**安全监控能力差异巨大**：

| 系统类型 | 内核 | 目标设备 | 类比 |
|---------|------|---------|------|
| **标准系统** (Standard) | Linux 5.10+ | 手机、平板、PC | 完整的 Linux 发行版（Ubuntu 级别） |
| **小型系统** (Small) | LiteOS-A | 路由器、摄像头、音箱 | 类似 FreeRTOS + MMU |
| **轻量系统** (Mini) | LiteOS-M | 传感器、灯泡等 IoT | 类似裸机 MCU 程序 |

> **关键洞察**：只有**标准系统**才有可能运行 eBPF。小型和轻量系统必须使用完全不同的策略。

---

## 3. eBPF 在 OpenHarmony 上的可行性分析

### 3.1 标准系统（Linux 内核）—— 理论支持，实操有门槛

OpenHarmony 标准系统使用 Linux 5.10+ 内核，从版本号看**完全满足** eBPF 要求（Linux 4.18+ 即可用大部分功能，5.10 已经非常成熟）。

但需要核实的核心前提：

```
# 关键内核配置项（缺一不可）
CONFIG_DEBUG_INFO_BTF=y          # ← 最关键！提供 BTF 类型信息，CO-RE 的基石
CONFIG_BPF=y                     # 启用 BPF 子系统
CONFIG_BPF_SYSCALL=y             # bpf() 系统调用
CONFIG_BPF_JIT=y                 # JIT 编译（否则 eBPF 是解释执行，性能差）
CONFIG_HAVE_EBPF_JIT=y           # 架构支持（ARM64 从 Linux 5.5+ 正式支持）
```

**现状评估**：

| 条件 | 状态 | 说明 |
|------|------|------|
| 内核版本 ≥ 4.18 | ✅ 满足 | 鸿蒙标准系统 Linux 5.10+ |
| `CONFIG_DEBUG_INFO_BTF=y` | ⚠️ 需验证 | **最大不确定因素**——取决于厂商编译配置 |
| ARM64 eBPF JIT | ✅ 支持 | Linux 5.5+ 的 ARM64 JIT 已成熟 |
| BCC 工具链可用 | ⚠️ 可能受限 | 鸿蒙可能未内置 LLVM/Clang，需要交叉编译 |
| libbpf + CO-RE | ✅ 推荐方案 | 预编译 + CO-RE 适配不同内核，比 BCC 更适合嵌入式 |

**华为自家扩展 —— hieBPF**：

华为在 AOSP 基础上开发了 **hieBPF**（HiSilicon eBPF），在部分 Kirin 芯片设备上增强了 eBPF 能力，包括：
- 硬件卸载 eBPF 程序到 NPU/DSP（性能极高）
- 扩展的 helper 函数（与鸿蒙安全子系统集成）

但 **公开文档极少**，且不确定在纯 OpenHarmony 开源版本中是否可用。对待 hieBPF 的策略：**有则用，无则回退**。

### 3.2 小型系统（LiteOS-A）—— 不支持 eBPF

LiteOS-A 是一个轻量级 POSIX 兼容内核，有自己的系统调用接口，但与 Linux 完全不同：
- 没有 `bpf()` 系统调用
- 没有 tracepoint/kprobe 机制
- 没有 BTF 类型系统

**结论：eBPF 在 LiteOS-A 上不可用，必须使用替代方案。**

### 3.3 轻量系统（LiteOS-M）—— 完全不支持

LiteOS-M 连 MMU 都没有，运行在物理地址空间，没有内核/用户态隔离。

**结论：传统意义上的"运行时监控"几乎不可能。只能依赖硬件层面的 TrustZone 隔离或外挂监控芯片。**

### 3.4 eBPF 可用性决策树

```
设备运行 OpenHarmony？
├── 标准系统 (Linux 内核)
│   ├── CONFIG_DEBUG_INFO_BTF=y？
│   │   ├── 是 → 🟢 使用 libbpf + CO-RE（最佳方案）
│   │   └── 否 → 🟡 回退方案：seccomp-bpf + LD_PRELOAD
│   └── 有 hieBPF？
│       └── 是 → 🟢 优先使用（但需评估锁定风险）
├── 小型系统 (LiteOS-A)
│   └── 🟠 使用 ptrace + IPC 权限监控
└── 轻量系统 (LiteOS-M)
    └── 🔴 仅硬件层面监控（超出 Runtime Guardian 范围）
```

---

## 4. 替代方案全景图

当 eBPF 不可用时，以下方案按可行性和推荐度排列：

### 4.1 seccomp-bpf（系统调用过滤）

**原理**：Linux 3.5+ 引入的安全机制，允许进程安装一个 BPF 程序来过滤系统调用。注意：这里的 "BPF" 是 **cBPF**（classic BPF），不是 eBPF，但内核会自动转换。

```
进程调用 syscall
    ↓
seccomp filter（cBPF 字节码）
    ├── SECCOMP_RET_ALLOW  → 放行
    ├── SECCOMP_RET_KILL   → 杀死进程
    ├── SECCOMP_RET_TRAP   → 发送 SIGSYS（用户态处理）
    └── SECCOMP_RET_ERRNO  → 返回错误码
    ↓
用户态 monitor 进程（通过 ptrace 或 SECCOMP_RET_TRAP 接收通知）
```

**类比**（给 JavaScript 开发者）：
```javascript
// seccomp-bpf 就像给每个进程安装一个中间件：
app.use((syscall, next) => {
  if (syscall.name === 'openat' && syscall.args.path === '/etc/shadow') {
    throw new SecurityError('Blocked');  // SECCOMP_RET_KILL
  }
  next();  // SECCOMP_RET_ALLOW
});
```

**优点**：
- ✅ 内置于 Linux 内核，无需额外工具链
- ✅ 性能极好（cBPF 在内核执行，开销 <1%）
- ✅ 鸿蒙标准系统一定支持（Linux 3.5+ 基础功能）

**限制**：
- ❌ 只能**事后过滤**，不能获取完整上下文（无调用栈、无 fd 关联）
- ❌ 无法监控已运行进程（只能在进程启动时安装）
- ❌ 对行为链检测支持弱（每个 syscall 独立判断，无法关联前后）

### 4.2 ptrace（进程追踪）

**原理**：传统的 Unix 调试接口，`strace` 就是基于它的。追踪进程在每个 syscall 入口/出口处暂停，追踪器检查参数和返回值。

```
被追踪进程              追踪器进程 (monitor)
    │                         │
    │ ── syscall ──→          │  PTRACE_SYSCALL（暂停）
    │  (被内核暂停)      ←──── │  读取寄存器、内存
    │                    ←──── │  判断是否可疑
    │                    ←──── │  PTRACE_SYSCALL（放行/注入错误）
    │ ── 继续执行 ──→          │
```

**类比**：
```python
# ptrace 就像在每个函数调用前后插入 await：
async def monitored_openat(path, flags):
    await inspector.check("openat", path=path)  # ← 暂停，等待检查
    result = await real_openat(path, flags)
    await inspector.report("openat", result=result)
    return result
```

**优点**：
- ✅ 每个 Linux 系统都有（包括鸿蒙标准系统）
- ✅ 能获取完整上下文（寄存器、内存、返回值）
- ✅ 无需内核模块或特殊配置

**限制**：
- ❌ **性能开销巨大**：每个 syscall 两次上下文切换，开销可达 50-90%
- ❌ 不适用于生产环境高频调用场景
- ❌ 容易被检测（反调试技术）
- ❌ 在 LiteOS-A/LiteOS-M 上不可用

### 4.3 LD_PRELOAD（用户态函数劫持）

**原理**：利用动态链接器的 `LD_PRELOAD` 环境变量，在目标进程加载前插入自定义的共享库（`.so`），拦截 glibc/musl 对系统调用的封装函数。

```
目标进程启动
    ↓
动态链接器 (ld.so) 加载共享库
    ├── libpreload.so  ← 我们先插入（LD_PRELOAD）
    │   ├── open()     ← 我们的"假的" open，先检查再调用真 open
    │   ├── read()     ← 同理
    │   └── execve()   ← ...
    └── libc.so        ← 真正的 libc
```

**类比**（给 JavaScript 开发者）：
```javascript
// LD_PRELOAD 就像猴子补丁 (Monkey Patching) libc：
const originalOpen = fs.open;  // 保存真正的 open
fs.open = function(path, flags, callback) {  // 替换
  if (path.startsWith('/etc/shadow')) {
    console.warn('Blocked!');
    return callback(new Error('EACCES'));
  }
  return originalOpen(path, flags, callback);  // 调用真正的
};
```

**类比**（给 Python 开发者）：
```python
# 等价于用 unittest.mock.patch 替换模块函数：
import os
_real_open = os.open
def _monitored_open(path, flags, *args, **kwargs):
    if path.startswith('/etc/shadow'):
        raise PermissionError("Blocked")
    return _real_open(path, flags, *args, **kwargs)
os.open = _monitored_open
```

**优点**：
- ✅ 纯用户态，无需 root（目标进程用户即可）
- ✅ 无需内核配置或特殊权限
- ✅ 能获取丰富的上下文（文件名、调用栈、参数）
- ✅ 可以对行为链做复杂分析（用户态无 eBPF 限制）

**限制**：
- ❌ **只能拦截 libc 封装函数**，绕过 libc 直接 syscall 的代码无法检测
- ❌ 静态链接的程序不受影响
- ❌ `LD_PRELOAD` 可以被检测和清除
- ❌ 需要重启目标进程
- ❌ 对 Go/Rust 等静态链接或使用自有运行时的程序无效

### 4.4 内核模块（LKM）

**原理**：编写 Linux 内核模块（`.ko`），直接在内核中挂钩系统调用表（`sys_call_table`）。

**类比**：
```python
# 内核模块就像直接修改 Python 解释器的 ceval.c：
# 在 CALL_FUNCTION 操作码处理中插入检查逻辑
# 比装饰器/decorator 深得多，但风险也大得多
```

**优点**：
- ✅ 与 eBPF 几乎相同的检测深度
- ✅ 不受 eBPF verifier 限制（可以做复杂逻辑）

**限制**：
- ❌ **鸿蒙标准系统对内核模块签名要求极其严格**
- ❌ 内核模块崩溃 = 整个系统崩溃
- ❌ 每次内核升级都需要重新编译
- ❌ 在生产设备上几乎不可能合法加载未签名模块
- ❌ 华为可能完全禁用第三方内核模块加载

**结论**：**不推荐**，作为最后手段记录但不纳入主要方案。

### 4.5 鸿蒙 IPC 权限监控（分布式能力）

**原理**：OpenHarmony 的核心特色是分布式能力。每个应用通过 **Ability** 框架运行，跨设备通信通过 **分布式软总线**（DSoftBus）。系统内置了 **访问控制** 和 **权限管理** 机制。

```
┌───────────────────────────────────────────┐
│          鸿蒙应用沙箱 (App Sandbox)          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Harmony  │  │  Web/JS  │  │  Native  │  │
│  │ Ability  │  │  Ability │  │  Ability │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       └──────────────┼──────────────┘       │
│              ┌───────┴───────┐              │
│              │  IPC 权限检查  │  ← 我们的监控点│
│              └───────┬───────┘              │
└──────────────────────┼──────────────────────┘
                       │
          ┌────────────┴────────────┐
          │   分布式软总线 (DSoftBus)  │
          │   跨设备 IPC 审计日志     │
          └─────────────────────────┘
```

**关键监控点**：

1. **Access Token（ATM）**—— 鸿蒙的权限管理核心
   - 每个应用有唯一的 Access Token
   - 所有 IPC 调用经过 ATM 验证
   - 可审计：谁调用了哪个 API，有没有权限

2. **IPC 通信审计**（LiteOS-A 和小型系统尤其重要）
   - 所有跨进程通信经过内核 IPC 层
   - 可以在此层记录调用链

3. **HUKS（鸿蒙统一密钥管理）审计**
   - 密钥的生成、存储、使用全生命周期审计
   - 适合检测凭证泄露类攻击

**类比**（给 JavaScript 开发者）：
```javascript
// 鸿蒙的 ATM 就像 OAuth2 的 Authorization Server：
// 每个 IPC 调用都要出示 token，ATM 验证后才放行
async function ipcCall(targetAbility, method, args) {
  const token = getAccessToken(currentAbility);  // 获取令牌
  await ATM.verify(token, targetAbility, method); // 验证权限
  // ← 在此记录审计日志
  return await DSoftBus.invoke(targetAbility, method, args);
}
```

**优点**：
- ✅ 鸿蒙原生能力，无需额外部署
- ✅ 覆盖所有类型系统（包括 LiteOS-A/LiteOS-M）
- ✅ 与鸿蒙安全模型深度集成
- ✅ 跨设备场景独有优势

**限制**：
- ❌ 只能监控 IPC 级别，不能监控系统调用级别
- ❌ API 和文档可能不完整（开源社区版本 vs 商用版本差异）
- ❌ 对纯 native 进程的监控能力有限
- ❌ 需要适配鸿蒙特有的编程模型

---

## 5. 方案对比矩阵

### 5.1 核心维度对比

| 方案 | 性能开销 | 兼容性 | 检测深度 | 部署难度 | 无需重启 | 绕过难度 |
|------|---------|--------|---------|---------|---------|---------|
| **eBPF (libbpf+CO-RE)** | 🟢 <1% | 🟡 需 BTF | 🟢 系统调用+网络+内核函数 | 🟡 需交叉编译 | 🟢 是 | 🟢 极难 |
| **hieBPF (华为扩展)** | 🟢 <1% | 🔴 仅华为设备 | 🟢 系统调用+硬件卸载 | 🔴 信息不透明 | 🟢 是 | 🟢 极难 |
| **seccomp-bpf** | 🟢 <1% | 🟢 Linux 3.5+ | 🟠 仅系统调用过滤 | 🟢 apt/yum 即可 | 🔴 需重启 | 🟠 中等 |
| **ptrace** | 🔴 50-90% | 🟢 所有 Linux | 🟢 完整上下文 | 🟢 内置命令 | 🟢 可附加 | 🟠 可检测 |
| **LD_PRELOAD** | 🟡 5-15% | 🟠 仅动态链接 | 🟠 仅 libc 调用 | 🟢 纯用户态 | 🔴 需重启 | 🔴 可绕过 |
| **内核模块** | 🟢 <1% | 🔴 需签名 | 🟢 完整内核访问 | 🔴 极高风险 | 🔴 需加载 | 🟢 极难 |
| **鸿蒙 IPC 审计** | 🟢 内置 | 🟢 鸿蒙原生 | 🟠 IPC 层面 | 🟢 天然集成 | 🟢 是 | 🟢 框架级 |

### 5.2 系统类型适用性矩阵

| 方案 | 标准系统 | 小型系统 (LiteOS-A) | 轻量系统 (LiteOS-M) |
|------|---------|-------------------|-------------------|
| eBPF (libbpf/BCC) | ✅ 最佳 | ❌ | ❌ |
| hieBPF | ✅ (华为设备) | ❌ | ❌ |
| seccomp-bpf | ✅ | ❌ | ❌ |
| ptrace | ✅ (谨慎) | ⚠️ 部分支持 | ❌ |
| LD_PRELOAD | ✅ 标准系统 | ⚠️ (若有动态链接) | ❌ |
| 内核模块 | ⚠️ 签名限制 | ❌ | ❌ |
| 鸿蒙 IPC 审计 | ✅ | ✅ 推荐 | ✅ 唯一可用 |
| HUKS 审计 | ✅ | ✅ | ✅ |

### 5.3 检测能力覆盖对比

| 检测类型 | eBPF | seccomp-bpf | ptrace | LD_PRELOAD | 鸿蒙 IPC |
|---------|------|------------|--------|-----------|----------|
| 敏感文件访问 | ✅ 全路径匹配 | ✅ 仅过滤 | ✅ 完整信息 | ✅ 完整信息 | ⚠️ 间接 |
| 系统调用风暴 | ✅ 内核聚合 | ⚠️ 需用户态统计 | ✅ 可统计 | ⚠️ 仅 libc | ❌ |
| 可疑行为链 | ✅ 内核+用户态 | ❌ 无上下文 | ✅ 完整链 | ✅ 完整链 | ⚠️ IPC 链 |
| 网络异常 | ✅ connect 跟踪 | ⚠️ 仅过滤 | ✅ 完整信息 | ✅ 完整信息 | ⚠️ Socket 层 |
| 基线异常 | ✅ 统计采样 | ❌ | ✅ 可采样 | ✅ 可采样 | ❌ |

---

## 6. 推荐的分阶段迁移策略

### 总体思路

```
            ┌──────────────────────────────────┐
            │  抽象检测接口（Adapter Pattern）    │
            │  Runtime Guardian Core Engine      │
            │  ┌──────────┐  ┌───────────────┐  │
            │  │ 状态机   │  │ 基线异常检测  │  │
            │  │ 检测引擎 │  │ (统计模型)    │  │
            │  └────┬─────┘  └──────┬────────┘  │
            │       └───────┬───────┘           │
            │     ┌─────────┴─────────┐         │
            │     │  检测后端适配器     │         │
            │     │  (DetectionBackend) │         │
            │     └───┬───┬───┬───┬───┘         │
            └─────────┼───┼───┼───┼─────────────┘
                      │   │   │   │
        ┌─────────────┘   │   │   └─────────────┐
        ▼                 ▼   ▼                 ▼
   eBPF Backend    seccomp   LD_PRELOAD    鸿蒙 IPC
  (标准系统优先)   Backend    Backend      Backend
```

**设计原则**：Python 的 **ABC (Abstract Base Class)** 或 TypeScript 的 **Interface** —— 定义统一接口，运行时选择具体实现：

```python
# 类比：统一的检测后端接口
from abc import ABC, abstractmethod

class DetectionBackend(ABC):
    """检测后端抽象基类 —— 类比 TypeScript 的 interface"""
    @abstractmethod
    def start_monitoring(self, target_pids: list[int]) -> None: ...
    @abstractmethod
    def stop_monitoring(self) -> None: ...
    @abstractmethod
    def get_events(self) -> asyncio.Queue: ...
    @abstractmethod
    def get_capabilities(self) -> dict: ...  # 查询后端能力

# 各后端实现：
class EbpfBackend(DetectionBackend): ...       # 阶段 1
class SeccompBackend(DetectionBackend): ...     # 阶段 2 回退
class PreloadBackend(DetectionBackend): ...     # 阶段 2 补充
class HarmonyIPCBackend(DetectionBackend): ...  # 阶段 3
```

### 阶段 1：标准系统 eBPF 优先（3-5 周）

**目标**：在鸿蒙标准系统上运行 eBPF 监控，保持与 Linux 版功能一致。

```
Week 1-2: 环境验证
├── 获取鸿蒙标准系统 SDK/镜像
├── 验证内核配置：zcat /proc/config.gz | grep -E 'BPF|BTF'
├── 尝试加载最小的 libbpf 程序（minimal.bpf.o）
└── 确认 perf ring buffer 工作正常

Week 3-4: 适配现有 ebpf_monitor.py
├── 将 BCC Python 绑定改为 libbpf + CO-RE
│   （BCC 重量级，不适合嵌入式/鸿蒙）
├── 交叉编译 eBPF 字节码为 ARM64
└── 替换 Linux 特有头文件为鸿蒙兼容版本

Week 5: hieBPF 评估（如果有）
├── 调研 hieBPF 文档和 API
├── 评估锁定风险（是否依赖华为闭源组件）
└── 决定是否采用或保持标准 eBPF
```

**交付物**：
- `ebpf_backend_harmony.py` —— 鸿蒙标准系统 eBPF 后端
- 兼容性检测脚本 `check_harmony_bpf.sh`
- 交叉编译 Makefile

### 阶段 2：不可用时的回退方案（2-4 周）

**触发条件**：`CONFIG_DEBUG_INFO_BTF=n` 或 eBPF 加载失败。

**2A. seccomp-bpf 方案（主回退）**

```
seccomp-bpf 监控架构：

目标进程启动时:
  1. fork() + 安装 seccomp filter (SECCOMP_RET_TRAP)
  2. 父进程 ptrace(PTRACE_SEIZE) 附加
  3. 当目标调用被过滤的系统调用 → 内核发送 SIGSYS
  4. 父进程捕获 SIGSYS → 记录事件 → 放行/阻止

关键：seccomp filter 只做"粗筛"（哪些 syscall 需要关注），
      真正的分析在用户态完成。这样可以避免 ptrace 的每个
      syscall 都暂停的开销。
```

**类比**：
```python
# seccomp-bpf 就像给进程设置"防火墙规则"：
firewall_rules = [
    ("openat", "/etc/shadow", BLOCK),
    ("connect", "*", AUDIT),      # 记录所有网络连接
    ("execve", "*", AUDIT),       # 记录所有 exec
    ("*", "*", ALLOW),            # 其余放行
]
# 只有匹配 AUDIT 规则的才会通知用户态分析器
```

**2B. LD_PRELOAD 方案（补充）**

针对动态链接的 C/C++ 应用，补充 seccomp 无法获取的上下文：

```c
// preload_hook.c —— 编译为 libguardian_preload.so
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>

// 保存真正的 open 函数指针
static int (*real_open)(const char *, int, ...) = NULL;

// 我们的"假的" open
int open(const char *pathname, int flags, ...) {
    if (!real_open) {
        real_open = dlsym(RTLD_NEXT, "open");  // 查找真正的 open
    }

    // 敏感文件检查
    if (pathname && strstr(pathname, "/etc/shadow")) {
        // 通过 Unix socket 通知监控进程
        notify_monitor("SENSITIVE_FILE", pathname);
        // 仍然放行（由监控进程决定是否 kill）
    }

    return real_open(pathname, flags);
}

// 同理覆盖 openat, fopen, read, write, connect, execve...
```

```bash
# 使用方式
LD_PRELOAD=/opt/guardian/libguardian_preload.so ./target_app
```

**交付物**：
- `seccomp_backend.py` —— seccomp-bpf + ptrace 混合后端
- `libguardian_preload.so` —— LD_PRELOAD 共享库
- `config/harmony_fallback.yaml` —— 回退方案配置

### 阶段 3：鸿蒙分布式跨设备监控（3-6 周）

**目标**：利用鸿蒙独有的分布式能力，实现 Linux 上不可能做到的跨设备行为链追踪。

```
┌───────────────────────────────────────────────────┐
│              鸿蒙分布式审计架构                       │
│                                                    │
│  手机                   平板                       │
│  ┌──────────┐          ┌──────────┐               │
│  │ Ability A│──IPC──→│ Ability B│                │
│  │ 读文件   │ (跨设备) │ 发送网络  │                │
│  └────┬─────┘          └────┬─────┘               │
│       │   分布式软总线        │                     │
│       └───────┬──────────────┘                     │
│               │                                    │
│       ┌───────┴───────┐                            │
│       │  Guardian Hub  │  ← 中央分析引擎             │
│       │  (跨设备关联)   │                            │
│       └───────────────┘                            │
│                                                    │
│   攻击链：                                           │
│   手机：敏感文件读取 ─→ IPC → 平板：外网数据传输       │
│   单个设备看起来"正常"，跨设备看"可疑"                 │
└───────────────────────────────────────────────────┘
```

**关键监控接口**：

```python
# 鸿蒙 IPC 审计适配器（伪代码）
class HarmonyIPCAuditBackend(DetectionBackend):
    """
    利用鸿蒙 Access Token (ATM) 和分布式软总线审计能力。

    类比：
    - ATM = OAuth2 Authorization Server
    - DSoftBus = gRPC with built-in audit
    - 跨设备行为链 = Distributed Tracing (Jaeger/Zipkin)
    """
    def start_monitoring(self, target_pids):
        # 1. 注册 IPC 审计回调
        ATM.register_audit_callback(self._on_ipc_call)
        # 2. 订阅分布式软总线事件
        DSoftBus.subscribe_events(self._on_cross_device_event)
        # 3. 启用 HUKS 审计（可选）
        HUKS.enable_audit()

    def _on_ipc_call(self, event):
        """
        事件示例：
        {
            "source_ability": "com.example.reader",
            "target_ability": "com.example.network",
            "method": "sendData",
            "token": "0x...",
            "timestamp": 1699000000000,
            "device_id": "device_A"
        }
        """
        self.event_queue.put(event)
```

**交付物**：
- `harmony_ipc_backend.py` —— 鸿蒙 IPC 审计后端
- `harmony_distributed_analyzer.py` —— 跨设备行为链分析
- `docs/harmony_deployment.md` —— 鸿蒙设备部署指南

### 迁移路线图总览

```
Month 1          Month 2          Month 3          Month 4+
─────┼───────────────┼───────────────┼───────────────┼────→

阶段1: eBPF 优先        ████████░░
├── 环境验证            ████░░░░░░
├── libbpf 移植         ░░░░████░░
└── hieBPF 评估         ░░░░░░███░

阶段2: 回退方案               ████████████░░
├── seccomp-bpf                ████████░░░░
├── LD_PRELOAD                 ░░░░████░░░░
└── 混合模式整合                ░░░░░░░░████

阶段3: 鸿蒙分布式                      ████████████████
├── IPC 审计接入                      ████████░░░░░░░░
├── 跨设备行为链                       ░░░░████████░░░░
└── HUKS 集成                         ░░░░░░░░░░██████

长期维护                                    ██████████████
├── 多后端自动切换                                   ░░
├── 性能基准测试                                     ░░
└── 鸿蒙安全认证                                     ░░
```

---

## 7. 深度分析：各替代方案详解

### 7.1 seccomp-bpf 深度分析

**seccomp 与 eBPF 的关系**（容易混淆的点）：

```
历史上的 BPF（Berkeley Packet Filter）：
  └── cBPF (classic BPF)  → 用于 seccomp、tcpdump
  └── eBPF (extended BPF) → 本项目的核心技术

seccomp 使用的是 cBPF，但内核内部会自动将 cBPF 转为 eBPF 执行。
所以 seccomp-bpf 的"BPF"和 Runtime Guardian 的 eBPF 是同一个技术家族的两个分支。
```

**seccomp 在鸿蒙中的可用性**：

```bash
# 验证 seccomp 是否可用
# 鸿蒙标准系统（Linux 内核）一定支持
cat /boot/config-* | grep SECCOMP
# 预期输出：CONFIG_SECCOMP=y

# 验证 seccomp-bpf 是否可用
zcat /proc/config.gz | grep SECCOMP_FILTER
# 预期输出：CONFIG_SECCOMP_FILTER=y
```

**seccomp-bpf 程序示例**（过滤 openat 访问 `/etc/shadow`）：

```python
import seccomp  # python3-seccomp 库

# 创建 seccomp filter
f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)

# 规则：如果 openat 的第二个参数（文件名）是 /etc/shadow → 杀死进程
# 注意：seccomp 只能检查 syscall 号和参数值（数字），不能读字符串！
# 所以实际使用中，seccomp 用于"粗筛"——把所有 openat 标记为需要审计
# 然后由用户态 ptrace 获取文件名。
f.add_rule(seccomp.ERRNO(seccomp.resolve_syscall("openat"), errno.EACCES),
           seccomp.Arg(0, seccomp.EQ, AT_FDCWD))  # 仅示例

f.load()
```

**重要限制**：seccomp 无法读取字符串参数（如文件名），因为它只能比较数值参数。这意味着实际使用时必须配合 ptrace 或 user_notif（Linux 5.0+ 的 seccomp user notification）。

### 7.2 ptrace 深度分析

**性能问题的根源**：

```
正常系统调用流程（无监控）：
  用户态 → 内核态(执行) → 用户态
  开销：~100ns（一次模式切换）

ptrace 监控下的系统调用流程：
  用户态 → 内核态(pause#1) → 通知 monitor → monitor读数据
  → monitor决定放行 → 内核态(执行) → 内核态(pause#2)
  → 通知 monitor → monitor读返回值 → monitor放行 → 用户态
  开销：~100μs（两次上下文切换 + 多次模式切换）
  性能损失：~1000x
```

**适用场景**：
- ✅ 开发调试
- ✅ 偶尔触发的安全事件分析
- ❌ 持续生产监控
- ❌ 高频系统调用场景（如数据库、Web 服务器）

### 7.3 LD_PRELOAD 深度分析

**覆盖范围有限**：

```c
// 以下这些调用 LD_PRELOAD 可以拦截：
int fd = open("/etc/passwd", O_RDONLY);       // ✅ 拦截成功
FILE *fp = fopen("/etc/passwd", "r");          // ✅ 拦截成功（内部调用 open）

// 以下这些调用 LD_PRELOAD 无法拦截：
int fd = syscall(__NR_openat, AT_FDCWD, "/etc/passwd", O_RDONLY);  // ❌ 绕过！
// Go 程序：直接使用 syscall.RawSyscall
// Rust 程序：使用内联汇编直接触发 syscall
```

**可拦截性矩阵**：

| 语言/运行时 | LD_PRELOAD 可用 | 原因 |
|------------|----------------|------|
| C (动态链接) | ✅ | 使用 libc |
| C++ (动态链接) | ✅ | 使用 libc |
| Python | ✅ | CPython 调用 libc |
| Node.js | ✅ | libuv 调用 libc |
| Go (默认) | ❌ | 静态链接 + 自有 syscall |
| Rust (静态) | ❌ | 静态链接 |
| Java (JNI) | ⚠️ 部分 | JVM 部分可拦截 |

### 7.4 鸿蒙 HUKS 审计能力

**HUKS（Harmony Universal KeyStore）** 是鸿蒙的统一密钥管理服务，提供：

```
HUKS 能力矩阵：
┌──────────────────────────────────────────────┐
│ 密钥生命周期管理                               │
│ ├── 密钥生成 (GenerateKey)                    │
│ ├── 密钥导入 (ImportKey)                      │
│ ├── 密钥导出 (ExportKey) — 受限               │
│ ├── 密钥删除 (DeleteKey)                      │
│ └── 密钥证明 (AttestKey) — 证明密钥在 TEE 中   │
│                                                │
│ 密码学操作                                     │
│ ├── 加密/解密 (Encrypt/Decrypt)                │
│ ├── 签名/验证 (Sign/Verify)                   │
│ ├── HMAC (MAC)                                │
│ └── 密钥协商 (AgreeKey) — ECDH                │
│                                                │
│ 审计能力（我们关注的重点）                       │
│ ├── 访问日志：哪个应用、何时、使用了哪个密钥     │
│ ├── 操作日志：生成、导入、导出、删除操作记录    │
│ └── 异常检测：未授权访问、频繁失败等异常行为    │
└──────────────────────────────────────────────┘
```

**HUKS 在安全监控中的定位**：

```python
# HUKS 审计可以补充 eBPF 无法覆盖的场景：
class HUKSAuditIntegrator:
    """
    eBPF 监控 → 检测"进程有没有读敏感文件"
    HUKS 审计 → 检测"进程有没有盗用加密密钥"

    两者互补，覆盖不同攻击面。
    """
    def on_huks_event(self, event):
        # 例如：一个从未使用过加密的应用突然调用了 HUKS 签名
        if self.is_anomalous(event.source_app, event.operation):
            self.alert("HUKS_ANOMALY", {
                "app": event.source_app,
                "key_alias": event.key_alias,
                "operation": event.operation,
            })
```

---

## 8. Python/JavaScript 类比参考

### 8.1 各方案一页纸类比

| 安全监控方案 | JavaScript 类比 | Python 类比 |
|------------|----------------|-------------|
| **eBPF** | 浏览器 Service Worker（拦截所有 fetch 事件） | `sys.settrace()` 但是在内核里（极快） |
| **seccomp-bpf** | Express 中间件 `app.use((req,res,next)=>{...})` | `@contextlib.contextmanager` 包装函数调用 |
| **ptrace** | 在每个函数前加 `debugger;` 断点 | 每个函数调用前插入 `pdb.set_trace()` |
| **LD_PRELOAD** | `fs.open = function(...) { ...; return original.apply(this, arguments); }` | `os.open = some_decorator(os.open)` |
| **内核模块** | 修改 V8 引擎的 C++ 源码编译自定义 Node | 修改 CPython 的 `ceval.c` 重新编译 |
| **鸿蒙 IPC 审计** | OAuth2 middleware + distributed tracing (Jaeger) | Django middleware + OpenTelemetry |
| **HUKS 审计** | WebCrypto API 的 wrapper 加日志 | `cryptography` 库的 hook |

### 8.2 检测后端适配器的 TypeScript 版概念

```typescript
// TypeScript 版适配器接口（概念演示，不是真实运行代码）
interface DetectionBackend {
  start(targets: number[]): Promise<void>;
  stop(): Promise<void>;
  onEvent(handler: (e: SecurityEvent) => void): void;
  readonly capabilities: BackendCapabilities;
}

interface BackendCapabilities {
  supportsSyscallTrace: boolean;
  supportsFilePath: boolean;
  supportsNetworkTracking: boolean;
  supportsCrossDevice: boolean;
  performanceOverhead: number;  // 0-1
}

// 运行时选择后端
async function createBackend(): Promise<DetectionBackend> {
  if (await hasEbpf()) {
    return new EbpfBackend();       // 阶段 1
  }
  if (await hasSeccomp()) {
    return new SeccompBackend();    // 阶段 2A
  }
  if (isHarmonyOS()) {
    return new HarmonyIPCBackend(); // 阶段 3
  }
  return new PreloadBackend();      // 阶段 2B
}
```

### 8.3 行为链检测的 Python 状态机类比

```python
# 行为链检测在所有后端中通用（后端只负责提供原始事件）
# 这是 Runtime Guardian 核心引擎的价值 —— 与后端解耦

class ProcessStateMachine:
    """
    监测系统调用序列。

    类比：这就像正则引擎，把系统调用序列当作字符串来匹配模式。
    "open→read→write→exec" 这个序列就像正则 /open.*read.*write.*exec/
    匹配了这条链就触发告警 —— 典型"下载→写文件→执行"攻击模式。
    """
    def transition(self, syscall_nr, filename, timestamp):
        """
        每次系统调用时调用此方法。
        返回 None 表示正常，返回字符串表示检测到可疑链。
        """
        # 逻辑与现有的 ProcessStateMachine 一致
        # 适配所有后端（eBPF/seccomp/ptrace/LD_PRELOAD）
        ...
```

---

## 9. 参考资源

| 主题 | 资源 | 说明 |
|------|------|------|
| OpenHarmony 标准系统 | [OpenHarmony Docs](https://docs.openharmony.cn/) | 官方文档，标准系统基于 Linux 5.10 |
| eBPF 在 ARM64 | [eBPF on ARM64](https://lwn.net/Articles/811631/) | ARM64 JIT 从 Linux 5.5 正式稳定 |
| seccomp user_notif | [LWN: seccomp user notification](https://lwn.net/Articles/776035/) | Linux 5.0+，比纯 ptrace 高效得多 |
| 鸿蒙 HUKS | [HUKS 开发指导](https://developer.harmonyos.com/cn/docs/documentation/doc-guides/security-huks-overview-0000001281042418) | 统一密钥管理开发文档 |
| 鸿蒙 Access Token | [ATM 开发指导](https://developer.harmonyos.com/cn/docs/documentation/doc-guides/security-accesstoken-overview-0000001281042418) | 权限管理开发文档 |
| libbpf + CO-RE | [libbpf-bootstrap](https://github.com/libbpf/libbpf-bootstrap) | 生产级 eBPF 应用模板 |
| BCC 到 libbpf 迁移 | [BCC to libbpf conversion](https://github.com/iovisor/bcc/blob/master/docs/kernel-versions.md) | 官方迁移指南 |

---

## 附录 A：快速兼容性检查脚本

```bash
#!/bin/bash
# check_harmony_bpf.sh —— 鸿蒙 eBPF 兼容性一键检查
# 在鸿蒙标准系统设备上运行

echo "=== 鸿蒙 eBPF 兼容性检查 ==="
echo

# 1. 检查内核版本
echo "1. 内核版本："
uname -r
KVER=$(uname -r | cut -d. -f1,2)
echo "   需要 4.18+，当前 $KVER"

# 2. 检查 BPF 相关内核配置
echo
echo "2. BPF 内核配置："
for cfg in CONFIG_BPF CONFIG_BPF_SYSCALL CONFIG_BPF_JIT \
           CONFIG_HAVE_EBPF_JIT CONFIG_DEBUG_INFO_BTF CONFIG_SECCOMP; do
    if zcat /proc/config.gz 2>/dev/null | grep -q "${cfg}=y"; then
        echo "   ✅ $cfg=y"
    elif grep -q "${cfg}=y" /boot/config-* 2>/dev/null; then
        echo "   ✅ $cfg=y (from /boot)"
    else
        echo "   ❌ $cfg NOT FOUND"
    fi
done

# 3. 检查 BTF 信息
echo
echo "3. BTF 信息："
if [ -f /sys/kernel/btf/vmlinux ]; then
    echo "   ✅ BTF vmlinux 存在"
    ls -lh /sys/kernel/btf/vmlinux
else
    echo "   ❌ 无 BTF 信息 — libbpf CO-RE 将不可用"
fi

# 4. 检查 seccomp
echo
echo "4. seccomp 支持："
if [ -f /proc/sys/kernel/seccomp/actions_avail ]; then
    echo "   ✅ seccomp 可用"
    echo "   可用动作: $(cat /proc/sys/kernel/seccomp/actions_avail)"
else
    echo "   ❌ seccomp 不可用"
fi

# 5. 检查是否为鸿蒙系统
echo
echo "5. 鸿蒙系统检测："
if [ -f /system/etc/ohos.para ]; then
    echo "   ✅ 鸿蒙系统 (OpenHarmony)"
    cat /system/etc/ohos.para | head -5
else
    echo "   ⚠️ 未检测到鸿蒙特征文件"
fi

echo
echo "=== 总结 ==="
echo "根据以上结果："
echo "  - 有 BTF → 使用 libbpf + CO-RE（阶段 1）"
echo "  - 无 BTF 但有 seccomp → 使用 seccomp-bpf（阶段 2A）"
echo "  - 鸿蒙系统 → 额外启用 IPC 审计（阶段 3）"
```

---

## 附录 B：术语对照表

| 缩写/术语 | 全称 | 说明 |
|----------|------|------|
| eBPF | extended Berkeley Packet Filter | 内核虚拟机，可在内核中运行沙箱程序 |
| BTF | BPF Type Format | 内核类型信息，CO-RE 的前提条件 |
| CO-RE | Compile Once, Run Everywhere | 一次编译、到处运行的 eBPF 技术 |
| BCC | BPF Compiler Collection | Python/C 的 eBPF 开发框架 |
| cBPF | classic BPF | 经典 BPF，用于 seccomp 和 tcpdump |
| HUKS | Harmony Universal KeyStore | 鸿蒙统一密钥管理服务 |
| ATM | Access Token Manager | 鸿蒙权限管理核心 |
| DSoftBus | Distributed Soft Bus | 鸿蒙分布式软总线 |
| TEE | Trusted Execution Environment | 可信执行环境（如 ARM TrustZone） |
| LKM | Loadable Kernel Module | 可加载内核模块 |
| LOS | LiteOS | 鸿蒙自研轻量级内核 |
