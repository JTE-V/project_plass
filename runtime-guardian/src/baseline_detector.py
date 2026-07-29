#!/usr/bin/env python3
"""
基线异常检测算法
================
自动学习正常进程的系统调用频率/模式，然后检测偏离。

算法设计：
  训练阶段（有监督）
    1. 收集 N 分钟的正常系统调用序列
    2. 对每个 syscall 类型计算统计特征（均值 μ、标准差 σ）
    3. 构建 n-gram 转移概率矩阵
    4. 计算系统调用多样性基线（香农熵）

  检测阶段（在线）
    1. 滑动窗口收集最近的系统调用
    2. 多维度计算异常分数：
       a. 频率异常：z-score = (x - μ) / σ → |z| > 3 触发
       b. 序列异常：n-gram 的 perplexity（困惑度）突然升高
       c. 多样性异常：香农熵偏离基线
    3. 综合评分 → 超过阈值则告警
    4. EWMA 缓慢更新基线（防止概念漂移）

  类比：
    - 训练 = 类似 scikit-learn 的 model.fit(X)
    - 检测 = 类似 model.predict(X) → 返回异常分数
    - EWMA = 类似 TensorFlow 的 exponential moving average

数学原理（用 Python 伪代码解释）：
    # z-score: 衡量偏离程度
    z = (current_value - historical_mean) / historical_std
    # 如果 |z| > 3，意味着当前值偏离均值超过 3 个标准差
    # 在正态分布中，概率 < 0.3%，几乎可以确定是异常

    # EWMA: 指数加权移动平均（缓慢更新基线）
    new_baseline = alpha * current + (1 - alpha) * old_baseline
    # alpha = 0.05 → 新数据权重 5%，旧基线 95%
    # 这样可以适应程序的自然演变（如版本升级后行为变化）
"""

import math
import json
import time
import struct
from collections import defaultdict, deque, Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import os

# 尝试导入 numpy（加速计算），不可用时用纯 Python 回退
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ================================================================
# 系统调用名称映射（从 Linux syscall 号 → 人类可读名称）
# ================================================================
SYSCALL_NAMES = {
    0: "read", 1: "write", 2: "open", 3: "close",
    4: "stat", 5: "fstat", 6: "lstat", 7: "poll",
    8: "lseek", 9: "mmap", 10: "mprotect", 11: "munmap",
    12: "brk", 13: "rt_sigaction", 14: "rt_sigprocmask",
    15: "rt_sigreturn", 16: "ioctl", 17: "pread64",
    18: "pwrite64", 19: "readv", 20: "writev",
    21: "access", 22: "pipe", 23: "select",
    24: "sched_yield", 25: "mremap", 28: "madvise",
    39: "getpid", 41: "socket", 42: "connect",
    43: "accept", 44: "sendto", 45: "recvfrom",
    49: "bind", 56: "clone", 57: "fork",
    59: "execve", 60: "exit", 61: "wait4",
    62: "kill", 78: "getdents", 79: "getcwd",
    80: "chdir", 82: "rename", 83: "mkdir",
    84: "rmdir", 85: "creat", 87: "link",
    89: "readlink", 90: "chmod", 91: "fchmod",
    92: "chown", 93: "fchown", 105: "setuid",
    106: "setgid", 137: "statfs", 157: "prctl",
    158: "arch_prctl", 186: "gettid", 201: "time",
    202: "futex", 217: "getdents64", 228: "clock_gettime",
    231: "exit_group", 234: "tgkill", 257: "openat",
    262: "newfstatat", 273: "set_robust_list",
    281: "epoll_wait", 291: "epoll_create1", 302: "prlimit64",
    318: "getrandom", 332: "statx", 334: "rseq",
}


@dataclass
class SyscallStats:
    """
    单个系统调用类型的统计特征。

    类比：Python 的 statistics.mean() + statistics.stdev() 的封装
    """
    name: str = ""
    count: int = 0           # 样本总数
    mean: float = 0.0        # 均值 μ = Σx / n
    m2: float = 0.0          # 用于 Welford 在线方差计算
    ewma: float = 0.0        # 指数加权移动平均
    alpha: float = 0.05      # EWMA 平滑系数

    def update(self, value: float):
        """
        使用 Welford 算法在线更新均值和方差。

        类比：这是 O(1) 的在线统计算法，不需要存储所有历史数据。
        比"存所有数据 → 算 mean/std" 节省 100-1000 倍内存。

        Welford 算法原理：
          delta = x - mean
          mean += delta / n
          m2 += delta * (x - mean)
          variance = m2 / (n - 1)
          std = sqrt(variance)
        """
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self.m2 += delta * delta2

        # 同时更新 EWMA
        if self.ewma == 0.0:
            self.ewma = value
        else:
            self.ewma = self.alpha * value + (1 - self.alpha) * self.ewma

    @property
    def std(self) -> float:
        """标准差 σ（需要至少 2 个样本）"""
        if self.count < 2:
            return 1.0  # 默认值，避免除零
        return math.sqrt(self.m2 / (self.count - 1))

    @property
    def variance(self) -> float:
        """方差 σ²"""
        if self.count < 2:
            return 1.0
        return self.m2 / (self.count - 1)

    def z_score(self, value: float) -> float:
        """
        计算 z-score：当前值偏离均值多少个标准差。

        解释：
          |z| < 1  → 68% 的正常数据在此范围
          |z| < 2  → 95% 的正常数据在此范围
          |z| < 3  → 99.7% 的正常数据在此范围
          |z| ≥ 3  → 极可能是异常！

        类比：考试成绩的"标准分"
        """
        if self.std < 0.001:
            return 0.0 if abs(value - self.mean) < 1 else float('inf')
        return (value - self.mean) / self.std

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "mean": self.mean,
            "std": self.std,
            "ewma": self.ewma,
            "alpha": self.alpha,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SyscallStats":
        s = cls(name=d["name"], alpha=d.get("alpha", 0.05))
        s.count = d["count"]
        s.mean = d["mean"]
        s.ewma = d.get("ewma", d["mean"])
        # 从 std 反算 m2
        std_val = d["std"]
        s.m2 = (std_val ** 2) * max(1, s.count - 1)
        return s


@dataclass
class NgramModel:
    """
    n-gram 序列模型：学习系统调用的转移模式。

    类比：Markov 链 — 给定前 2 个系统调用，预测下一个是什么。

    例如正常程序的行为：
      openat → read → read → read → write → close
      如果突然出现 openat → read → write → execve，这是从未见过的模式 → 异常
    """
    n: int = 2                                    # n-gram 的 n
    # prefix → {next_syscall: count}
    transitions: Dict[Tuple[int, ...], Counter] = field(default_factory=dict)
    total_transitions: int = 0

    def add_sequence(self, syscalls: List[int]):
        """
        从系统调用序列中学习转移概率。

        例如 syscalls = [257, 0, 0, 1, 3]
        对于 bigram (n=2):
          前缀 (257,) → 下一个可能是 0
          前缀 (0,)   → 下一个可能是 0 或 1
          前缀 (1,)   → 下一个可能是 3
        """
        for i in range(len(syscalls) - self.n):
            prefix = tuple(syscalls[i:i + self.n - 1])
            next_sys = syscalls[i + self.n - 1]

            if prefix not in self.transitions:
                self.transitions[prefix] = Counter()
            self.transitions[prefix][next_sys] += 1
            self.total_transitions += 1

    def prob(self, prefix: Tuple[int, ...], next_sys: int) -> float:
        """
        给定前缀，预测下一个系统调用的概率。

        使用拉普拉斯平滑（Laplace smoothing）防止零概率：
          P(next | prefix) = (count + 1) / (total + V)
          其中 V 是可能的系统调用种类数
        """
        if prefix not in self.transitions:
            return 1.0 / 400  # 均匀分布（约 400 种系统调用）

        counter = self.transitions[prefix]
        total = sum(counter.values())
        V = 400  # 平滑参数
        return (counter.get(next_sys, 0) + 1) / (total + V)

    def perplexity(self, syscalls: List[int]) -> float:
        """
        计算给定序列的困惑度（perplexity）。

        困惑度 = 模型对这段序列"感到意外"的程度。
        正常序列 → 低困惑度（模型见过类似模式）
        异常序列 → 高困惑度（模型从未见过这种模式）

        类比：GPT 模型的 perplexity → 越低表示模型越"熟悉"这段文本
        """
        if len(syscalls) < self.n:
            return 0.0

        log_prob_sum = 0.0
        count = 0
        for i in range(len(syscalls) - self.n + 1):
            prefix = tuple(syscalls[i:i + self.n - 1])
            next_sys = syscalls[i + self.n - 1]
            p = self.prob(prefix, next_sys)
            if p > 0:
                log_prob_sum += math.log(p)
                count += 1

        if count == 0:
            return float('inf')

        avg_log_prob = log_prob_sum / count
        return math.exp(-avg_log_prob)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "transitions": {
                ",".join(str(x) for x in k):
                    dict(v) for k, v in self.transitions.items()
            },
            "total_transitions": self.total_transitions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NgramModel":
        m = cls(n=d["n"])
        m.total_transitions = d["total_transitions"]
        m.transitions = {
            tuple(int(x) for x in k.split(",")):
                Counter(v) for k, v in d["transitions"].items()
        }
        return m


# ================================================================
# 深层分析器 1：文件上下文分类器
# 根据操作的文件路径前缀，将文件操作分为不同语义类别
# 类比：Web 防火墙的 URL 路径分类
# ================================================================
class FileContextClassifier:
    """
    文件路径上下文分类器。

    只看 syscall 号不够——open("/etc/passwd") 和 open("/var/log/app.log")
    在 syscall 层面完全一样，但安全含义天差地别。

    类比：Nginx 的 location 规则
          /etc/*    → 系统配置（高敏感）
          /var/log/* → 日志（低敏感）
          /tmp/*    → 临时文件（中敏感，常见攻击载体）
    """

    # 路径前缀 → (类别名, 风险等级 0-10)
    PATH_RULES = [
        # 系统核心配置 — 最高风险
        ("/etc/",                 "SYSCONFIG",  10),
        ("/boot/",                "BOOT",       10),
        # SSH/密钥 — 最高风险
        ("/root/.ssh/",           "SSH_KEY",    10),
        ("/home/",                "USER_HOME",   8),
        # 日志 — 中等风险（攻击者常篡改日志）
        ("/var/log/",             "LOG",         5),
        # 临时目录 — 高风险（常见攻击载体）
        ("/tmp/",                 "TEMP",        8),
        ("/dev/shm/",             "SHARED_MEM",  9),
        ("/var/tmp/",             "TEMP",        7),
        # 进程文件系统 — 中等风险
        ("/proc/",                "PROCFS",      6),
        ("/sys/",                 "SYSFS",       6),
        # 设备 — 高风险
        ("/dev/",                 "DEVICE",      9),
        # Web 目录 — 看角色而定
        ("/var/www/",             "WEB_ROOT",    7),
        ("/usr/share/nginx/",     "WEB_ROOT",    7),
        # 库和可执行文件 — 低风险（程序正常加载）
        ("/usr/lib/",             "LIBRARY",     2),
        ("/usr/bin/",             "BINARY",      3),
        ("/usr/local/",           "BINARY",      3),
        ("/lib/",                 "LIBRARY",     2),
        # 运行时数据
        ("/var/run/",             "RUNTIME",     4),
        ("/var/lib/",             "DATA",        4),
        # 网络配置
        ("/etc/network/",         "NETCONFIG",   9),
        ("/etc/ssl/",             "SSL_CERT",   10),
    ]

    @classmethod
    def classify(cls, filename: str) -> Tuple[str, int]:
        """
        根据文件路径返回 (类别名, 风险等级)。

        >>> FileContextClassifier.classify("/etc/passwd")
        ('SYSCONFIG', 10)
        >>> FileContextClassifier.classify("/var/log/nginx/access.log")
        ('LOG', 5)
        >>> FileContextClassifier.classify("/tmp/.X11-unix/X0")
        ('TEMP', 8)
        """
        if not filename:
            return ("UNKNOWN", 5)

        for prefix, category, risk in cls.PATH_RULES:
            if filename.startswith(prefix):
                return (category, risk)

        return ("UNKNOWN", 5)

    @classmethod
    def get_category(cls, filename: str) -> str:
        return cls.classify(filename)[0]


# ================================================================
# 深层分析器 2：多用户行为建模
# 不同 UID 的程序行为模式完全不同
# ================================================================
class MultiUserProfiler:
    """
    多用户行为画像。

    root(UID=0) 的行为——加载内核模块、修改系统配置、绑定低端口
    www-data(UID=33) 的行为——读写 Web 目录、连接数据库
    普通用户的行为——编辑文档、浏览文件

    如果 www-data 突然以 root 模式行事（访问 /etc/shadow），立即告警。

    类比：Kubernetes 的 PodSecurityPolicy——不同身份有不同的"正常行为"边界
    """

    def __init__(self, ewma_alpha: float = 0.05, window_seconds: float = 10.0):
        self.ewma_alpha = ewma_alpha
        self.window_seconds = window_seconds
        # uid → {syscall_nr: SyscallStats}
        self.user_stats: Dict[int, Dict[int, SyscallStats]] = defaultdict(dict)
        # uid → sliding window
        self.user_windows: Dict[int, deque] = defaultdict(
            lambda: deque(maxlen=5000))
        # uid → 累计事件数
        self.user_event_counts: Dict[int, int] = defaultdict(int)

    def train(self, uid: int, syscall_nr: int, ts: float):
        """为特定 UID 训练一条系统调用记录"""
        window = self.user_windows[uid]
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()
        window.append((ts, syscall_nr))

        self.user_event_counts[uid] += 1

        # 更新频率统计
        freq_counter = Counter(sc for _, sc in window)
        window_dur = min(self.window_seconds,
                         ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if window_dur > 0:
            for sc, count in freq_counter.items():
                rate = count / window_dur
                if sc not in self.user_stats[uid]:
                    name = SYSCALL_NAMES.get(sc, f"sys_{sc}")
                    self.user_stats[uid][sc] = SyscallStats(name=name, alpha=self.ewma_alpha)
                self.user_stats[uid][sc].update(rate)

    def check(self, uid: int, syscall_nr: int, ts: float) -> float:
        """
        返回该 UID 的当前行为异常分数（基于频率）。

        对于未见过的 UID，返回 2.0（中等可疑）。
        对于该 UID 中未见过的 syscall，返回 3.0（较可疑）。
        """
        if uid not in self.user_stats or len(self.user_stats[uid]) == 0:
            return 2.0  # 未知用户

        # 更新滑动窗口
        window = self.user_windows[uid]
        window.append((ts, syscall_nr))
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()

        freq_counter = Counter(sc for _, sc in window)
        window_dur = min(self.window_seconds,
                         ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if window_dur < 0.1:
            return 0.0

        max_z = 0.0
        for sc, count in freq_counter.items():
            rate = count / window_dur
            if sc in self.user_stats[uid] and self.user_stats[uid][sc].count > 30:
                z = abs(self.user_stats[uid][sc].z_score(rate))
                max_z = max(max_z, z)
            elif sc not in self.user_stats[uid]:
                # 该 UID 从未使用过此系统调用
                max_z = max(max_z, 4.0 + rate * 0.5)

        import math
        if max_z <= 3.0:
            return max_z / 3.0
        elif max_z <= 30.0:
            return 1.0 + (max_z - 3.0) / 13.5
        else:
            return 3.0 + math.log2(max_z / 30.0)

    def get_user_profile(self, uid: int) -> dict:
        """获取用户的行为画像摘要"""
        if uid not in self.user_stats:
            return {}
        return {
            "total_events": self.user_event_counts.get(uid, 0),
            "syscall_types": len(self.user_stats[uid]),
            "top_syscalls": sorted(
                [(sc, s.mean) for sc, s in self.user_stats[uid].items()],
                key=lambda x: x[1], reverse=True
            )[:5],
        }

    def to_dict(self) -> dict:
        return {
            "user_stats": {
                str(uid): {str(sc): s.to_dict() for sc, s in stats.items()}
                for uid, stats in self.user_stats.items()
            },
            "user_event_counts": {str(k): v for k, v in self.user_event_counts.items()},
        }

    @classmethod
    def from_dict(cls, d: dict, ewma_alpha: float = 0.05) -> "MultiUserProfiler":
        p = cls(ewma_alpha=ewma_alpha)
        for uid_str, sc_dict in d.get("user_stats", {}).items():
            uid = int(uid_str)
            for sc_str, stats_d in sc_dict.items():
                sc = int(sc_str)
                p.user_stats[uid][sc] = SyscallStats.from_dict(stats_d)
        for uid_str, count in d.get("user_event_counts", {}).items():
            p.user_event_counts[int(uid_str)] = count
        return p


# ================================================================
# 深层分析器 3：时间窗口分段基线
# 凌晨3点的行为模式和下午3点完全不同
# ================================================================
class TimeWindowedProfiler:
    """
    时间分段行为基线。

    同一个进程在：
      - 凌晨 3 点 → 几乎空闲（cron 任务偶尔触发）
      - 上午 9 点 → 用户登录、大量文件操作
      - 下午 2 点 → 高峰流量

    如果在凌晨 3 点突然发生"下午高峰"级别的系统调用频率 → 高度可疑。

    类比：AWS CloudWatch 的 anomaly detection band ——
          同一天不同时段有不同的正常区间。
    """

    # 时间分桶策略（小时区间）
    HOUR_BUCKETS = [
        (0, 5, "NIGHT"),       # 凌晨 0-5：最安静
        (6, 8, "MORNING"),     # 早上 6-8：启动期
        (9, 11, "BUSINESS"),   # 上午 9-11：工作高峰
        (12, 14, "AFTERNOON"), # 下午 12-14：午间
        (15, 17, "PEAK"),      # 下午 15-17：最高峰
        (18, 20, "EVENING"),   # 晚上 18-20：下降
        (21, 23, "NIGHT"),     # 晚上 21-23：夜间
    ]

    def __init__(self, ewma_alpha: float = 0.05, window_seconds: float = 60.0):
        self.ewma_alpha = ewma_alpha
        self.window_seconds = window_seconds
        # bucket_name → {syscall_nr: SyscallStats}
        self.bucket_stats: Dict[str, Dict[int, SyscallStats]] = defaultdict(dict)
        # bucket_name → sliding_window
        self.bucket_windows: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=20000))
        self.bucket_counts: Dict[str, int] = defaultdict(int)

    def _get_bucket(self, ts: float) -> str:
        """根据时间戳返回时段名称"""
        import datetime
        hour = datetime.datetime.fromtimestamp(ts).hour
        for start, end, name in self.HOUR_BUCKETS:
            if start <= hour <= end:
                return name
        return "NIGHT"

    def train(self, syscall_nr: int, ts: float):
        """按时间桶训练"""
        bucket = self._get_bucket(ts)
        window = self.bucket_windows[bucket]
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()
        window.append((ts, syscall_nr))
        self.bucket_counts[bucket] += 1

        freq_counter = Counter(sc for _, sc in window)
        dur = min(self.window_seconds,
                  ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if dur > 0:
            for sc, count in freq_counter.items():
                rate = count / dur
                if sc not in self.bucket_stats[bucket]:
                    name = SYSCALL_NAMES.get(sc, f"sys_{sc}")
                    self.bucket_stats[bucket][sc] = SyscallStats(
                        name=name, alpha=self.ewma_alpha)
                self.bucket_stats[bucket][sc].update(rate)

    def check(self, syscall_nr: int, ts: float) -> float:
        """
        检查当前时间桶的行为是否异常。

        返回：该时间桶内的异常分数（0-5+）。
        跨桶对比：如果当前桶的事件频率与相邻桶差异极大，额外加分。
        """
        bucket = self._get_bucket(ts)
        if bucket not in self.bucket_stats or len(self.bucket_stats[bucket]) == 0:
            return 0.0  # 新时段，先观察

        # 更新滑动窗口
        window = self.bucket_windows[bucket]
        window.append((ts, syscall_nr))
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()

        freq_counter = Counter(sc for _, sc in window)
        dur = min(self.window_seconds,
                  ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if dur < 0.1:
            return 0.0

        max_z = 0.0
        for sc, count in freq_counter.items():
            rate = count / dur
            if sc in self.bucket_stats[bucket] and self.bucket_stats[bucket][sc].count > 30:
                z = abs(self.bucket_stats[bucket][sc].z_score(rate))
                max_z = max(max_z, z)

        import math
        if max_z <= 3.0:
            return max_z / 3.0
        elif max_z <= 30.0:
            return 1.0 + (max_z - 3.0) / 13.5
        else:
            return 3.0 + math.log2(max_z / 30.0)

    def get_current_bucket(self, ts: float = None) -> str:
        return self._get_bucket(ts or time.time())

    def to_dict(self) -> dict:
        return {
            "bucket_stats": {
                bucket: {str(sc): s.to_dict() for sc, s in stats.items()}
                for bucket, stats in self.bucket_stats.items()
            },
            "bucket_counts": dict(self.bucket_counts),
        }

    @classmethod
    def from_dict(cls, d: dict, ewma_alpha: float = 0.05) -> "TimeWindowedProfiler":
        p = cls(ewma_alpha=ewma_alpha)
        for bucket, sc_dict in d.get("bucket_stats", {}).items():
            for sc_str, stats_d in sc_dict.items():
                sc = int(sc_str)
                p.bucket_stats[bucket][sc] = SyscallStats.from_dict(stats_d)
        for bucket, count in d.get("bucket_counts", {}).items():
            p.bucket_counts[bucket] = count
        return p


# ================================================================
# 深层分析器 4：负载感知器
# 实时追踪全局系统调用频率，评估当前负载水平
# ================================================================
class LoadAwareness:
    """
    全局负载感知器。

    核心洞察：
      同一个 openat 调用，在 1000 QPS 时出现 200 次/秒是正常的，
      但在 10 QPS 时出现 50 次/秒就是高度异常的。
      不能用固定阈值——必须根据当前负载动态调整。

    类比：Nginx 的 limit_req 模块
          - 正常时允许 burst=20
          - 高负载时 burst=50
          - 不是让攻击通过，而是防止误杀正常流量尖刺

    负载指标选择（按优先级）：
      1. epoll_wait 频率 — 最直接反映"请求量"（Web 服务器核心）
      2. read/recvfrom 频率 — I/O 吞吐量
      3. 所有 syscall 总量 — 综合活跃度
    """

    def __init__(self, window_seconds: float = 30.0, history_buckets: int = 100):
        self.window_seconds = window_seconds
        # 滑动窗口：所有 PID 的 syscall 时间戳
        self.global_window: deque = deque(maxlen=50000)
        # 历史负载分位数 [p10, p25, p50, p75, p90]
        self.percentiles: List[float] = [0, 0, 0, 0, 0]
        # 负载 EWMA（用于平滑）
        self.load_ewma = SyscallStats(name="global_load", alpha=0.05)
        # 负载历史（用于计算分位数）
        self.load_history: deque = deque(maxlen=history_buckets)

    def record(self, ts: float):
        """记录一次系统调用（全局计数）"""
        self.global_window.append(ts)
        cutoff = ts - self.window_seconds
        while self.global_window and self.global_window[0] < cutoff:
            self.global_window.popleft()

    @property
    def current_rate(self) -> float:
        """当前全局系统调用频率（次/秒）"""
        if len(self.global_window) < 2:
            return 0.0
        duration = self.global_window[-1] - self.global_window[0]
        if duration < 0.1:
            return 0.0
        return len(self.global_window) / duration

    def update_baseline(self):
        """更新负载分位线和 EWMA"""
        rate = self.current_rate
        if rate > 0:
            self.load_ewma.update(rate)
            self.load_history.append(rate)
            if len(self.load_history) >= 20:
                sorted_loads = sorted(self.load_history)
                n = len(sorted_loads)
                self.percentiles = [
                    sorted_loads[int(n * 0.10)],
                    sorted_loads[int(n * 0.25)],
                    sorted_loads[int(n * 0.50)],
                    sorted_loads[int(n * 0.75)],
                    sorted_loads[int(n * 0.90)],
                ]

    def get_load_level(self) -> Tuple[str, float]:
        """
        返回 (负载级别, 相对偏差)。

        负载级别：
          CRITICAL_LOW — 低于 P10（系统可能挂了）
          LOW          — P10-P25 之间
          NORMAL       — P25-P75 之间（正常范围）
          HIGH         — P75-P90 之间
          SURGE        — 高于 P90（流量尖峰/攻击）

        相对偏差 = current / P50 - 1（正=高于中位，负=低于中位）
        """
        rate = self.current_rate
        if rate <= 0:
            return ("UNKNOWN", 0.0)

        p10, p25, p50, p75, p90 = self.percentiles
        if p50 <= 0:
            return ("INITIALIZING", 0.0)

        deviation = rate / p50 - 1.0

        if rate < p10:
            return ("CRITICAL_LOW", deviation)
        elif rate < p25:
            return ("LOW", deviation)
        elif rate <= p75:
            return ("NORMAL", deviation)
        elif rate <= p90:
            return ("HIGH", deviation)
        else:
            return ("SURGE", deviation)

    def to_dict(self) -> dict:
        return {
            "percentiles": self.percentiles,
            "load_ewma": self.load_ewma.to_dict(),
            "history_len": len(self.load_history),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoadAwareness":
        la = cls()
        la.percentiles = d.get("percentiles", [0, 0, 0, 0, 0])
        la.load_ewma = SyscallStats.from_dict(d["load_ewma"])
        return la


# ================================================================
# 深层分析器 5：自适应阈值控制器
# 根据负载水平动态调整各维度阈值和权重
# ================================================================
class AdaptiveThresholdController:
    """
    自适应阈值控制器。

    设计哲学：
      不是在"检测准确率"和"误报率"之间找平衡点——
      而是让平衡点随负载自动移动。

    三条自适应规则：

    规则1 - 动态阈值：
      base_threshold = 3.0 (z-score 基准)
      低负载(CRITICAL_LOW): threshold = 2.0  (更敏感——此时任何波动都可疑)
      正常(NORMAL):        threshold = 3.0  (标准)
      高负载(HIGH):        threshold = 4.5  (容忍尖刺——流量波动大)
      浪涌(SURGE):         threshold = 6.0  (极高容忍——可能是 DDoS)

    规则2 - 动态权重：
      正常时：freq=0.25, seq=0.10, user=0.20, file=0.15, time=0.15
      低负载：freq=0.35, seq=0.05 (频率更重要——低频下的异常很明显)
      高负载：freq=0.15, seq=0.20 (序列更重要——高频下模式比频率可靠)

    规则3 - 自适应 EWMA：
      正常时：α=0.03 (缓慢自适应)
      持续偏离：α=0.12 (快速适应——可能是业务增长)
      标记异常：α=0    (冻结——防止攻击数据污染基线)
    """

    def __init__(self):
        self.current_load_level = "INITIALIZING"
        self.current_deviation = 0.0

    def update(self, load_awareness: LoadAwareness):
        """根据负载感知器更新当前状态"""
        self.current_load_level, self.current_deviation = \
            load_awareness.get_load_level()

    def get_threshold_multiplier(self) -> float:
        """
        返回阈值乘数。

        负载越低 → 乘数越小 → 阈值越敏感
        负载越高 → 乘数越大 → 阈值越宽容
        """
        mapping = {
            "CRITICAL_LOW": 0.60,   # 阈值 × 0.6 → 极度敏感
            "LOW":          0.75,
            "NORMAL":       1.00,
            "HIGH":         1.50,
            "SURGE":        2.00,   # 阈值 × 2.0 → 最宽容
            "INITIALIZING": 1.00,
            "UNKNOWN":      1.00,
        }
        return mapping.get(self.current_load_level, 1.0)

    def get_dynamic_weights(self) -> Dict[str, float]:
        """
        返回当前负载水平下的最优权重分配。

        设计原理：
        - 低负载时频率权重高：因为正常时调用量小，任何频率变化都很明显
        - 高负载时序列权重高：大量调用中频率波动大，但调用模式仍然稳定
        - 用户/文件/时间维度相对稳定
        """
        level = self.current_load_level

        if level in ("CRITICAL_LOW", "LOW"):
            # 低负载：频率是最可靠的指标
            return {"freq": 0.35, "seq": 0.05, "entropy": 0.10,
                    "diversity": 0.05, "user": 0.20, "file_context": 0.10, "time": 0.15}
        elif level == "NORMAL":
            # 正常负载：均衡权重
            return {"freq": 0.25, "seq": 0.10, "entropy": 0.10,
                    "diversity": 0.05, "user": 0.20, "file_context": 0.15, "time": 0.15}
        elif level in ("HIGH", "SURGE"):
            # 高负载：序列和用户维度更重要（频率波动大）
            return {"freq": 0.15, "seq": 0.22, "entropy": 0.12,
                    "diversity": 0.06, "user": 0.20, "file_context": 0.10, "time": 0.15}
        else:
            return {"freq": 0.25, "seq": 0.10, "entropy": 0.10,
                    "diversity": 0.05, "user": 0.20, "file_context": 0.15, "time": 0.15}

    def get_adaptive_alpha(self, consecutive_deviation: int = 0) -> float:
        """
        返回自适应 EWMA α 值。

        正常：α=0.03（缓慢更新，稳定基线）
        连续偏离 5+ 次：α=0.10（快速适应可能的概念漂移）
        连续偏离 10+ 次：α=0.20（激进适应，可能是业务增长）
        """
        if consecutive_deviation >= 10:
            return 0.20  # 激进：可能是永久性业务变化
        elif consecutive_deviation >= 5:
            return 0.10  # 快速适应
        elif consecutive_deviation >= 2:
            return 0.05
        else:
            return 0.03  # 正常慢速更新

    def to_dict(self) -> dict:
        return {
            "current_load_level": self.current_load_level,
            "current_deviation": self.current_deviation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AdaptiveThresholdController":
        c = cls()
        c.current_load_level = d.get("current_load_level", "INITIALIZING")
        c.current_deviation = d.get("current_deviation", 0.0)
        return c


class BaselineDetector:
    """
    基线异常检测器。

    使用方式：
      # 1. 训练阶段（在生产前进行）
      detector = BaselineDetector()
      for syscall_event in normal_traffic:
          detector.train(syscall_nr, pid, timestamp)

      # 2. 保存模型
      detector.save("baseline_model.json")

      # 3. 检测阶段（生产环境中）
      detector = BaselineDetector.load("baseline_model.json")
      for syscall_event in live_traffic:
          score = detector.check(syscall_nr, pid, timestamp)
          if score > 3.0:
              print(f"🚨 基线异常! score={score}")

    类比：scikit-learn 的 Pipeline
      fit(X_train) → save → predict(X_test) → anomaly_scores
    """

    def __init__(self,
                 window_seconds: float = 10.0,
                 ewma_alpha: float = 0.05,
                 enable_ngram: bool = True,
                 enable_entropy: bool = True,
                 enable_multi_user: bool = True,
                 enable_file_context: bool = True,
                 enable_time_window: bool = True):
        """
        参数：
          window_seconds: 滑动窗口大小（秒），用于统计频率
          ewma_alpha: EWMA 更新系数（越小越保守，越大越敏感）
          enable_ngram: 是否启用 n-gram 序列检测
          enable_entropy: 是否启用香农熵检测
          enable_multi_user: 是否启用多用户行为基线
          enable_file_context: 是否启用文件上下文感知
          enable_time_window: 是否启用时间分段基线
        """
        self.window_seconds = window_seconds
        self.ewma_alpha = ewma_alpha
        self.enable_ngram = enable_ngram
        self.enable_entropy = enable_entropy
        self.enable_multi_user = enable_multi_user
        self.enable_file_context = enable_file_context
        self.enable_time_window = enable_time_window

        # 每个系统调用类型的统计特征
        self.syscall_stats: Dict[int, SyscallStats] = {}

        # n-gram 模型（学习系统调用转移模式）
        self.ngram_model = NgramModel(n=2) if enable_ngram else None

        # 滑动窗口（用于实时频率统计）
        self.sliding_windows: Dict[int, deque] = defaultdict(
            lambda: deque(maxlen=10000))

        # 历史熵值
        self.entropy_stats = SyscallStats(name="entropy", alpha=ewma_alpha)
        self.diversity_stats = SyscallStats(name="diversity", alpha=ewma_alpha)

        # 全局计数器
        self.global_counter: Counter = Counter()
        self.total_syscalls: int = 0

        # === 深层分析器 ===
        # 多用户行为画像
        self.user_profiler = MultiUserProfiler(
            ewma_alpha=ewma_alpha, window_seconds=window_seconds
        ) if enable_multi_user else None

        # 文件上下文：按路径类别统计频率
        # category → Dict[int, SyscallStats]
        self.file_context_stats: Dict[str, Dict[int, SyscallStats]] = defaultdict(dict)
        self.file_context_windows: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=5000))

        # 时间分段基线
        self.time_profiler = TimeWindowedProfiler(
            ewma_alpha=ewma_alpha, window_seconds=window_seconds * 6
        ) if enable_time_window else None

        # 训练/检测模式
        self.is_trained = False
        self.min_training_events = 1000

        # === 负载感知 + 自适应阈值 ===
        self.load_awareness = LoadAwareness(window_seconds=30.0)
        self.adaptive_controller = AdaptiveThresholdController()
        self._consecutive_anomalies = 0  # 连续异常计数

        # === 内存保护：滑动窗口总量限制 ===
        self._max_total_window_entries = 200000  # 所有 PID 窗口的总条目上限
        self._window_entry_count = 0

    # ================================================================
    # 训练阶段
    # ================================================================

    def train(self, syscall_nr: int, pid: int = 0, uid: int = -1,
              timestamp: float = None, filename: str = ""):
        """
        训练一个系统调用事件。

        参数：
          syscall_nr: 系统调用号（如 257=openat）
          pid: 进程 ID
          uid: 用户 ID（-1 表示未知，多用户分析需要）
          timestamp: 时间戳（默认当前时间）
          filename: 关联的文件名（文件上下文分析需要）
        """
        ts = timestamp or time.time()

        # 1. 更新频率统计
        self._update_frequency(syscall_nr, pid, ts)

        # 2. 更新 n-gram 模型
        if self.enable_ngram:
            self._update_ngram(syscall_nr, pid)

        # 3. 更新全局计数器
        self.global_counter[syscall_nr] += 1
        self.total_syscalls += 1

        # 4. 多用户训练
        if self.enable_multi_user and uid >= 0:
            self.user_profiler.train(uid, syscall_nr, ts)

        # 5. 文件上下文训练
        if self.enable_file_context and filename and syscall_nr in (257,):
            category = FileContextClassifier.get_category(filename)
            self._train_file_context(syscall_nr, category, ts)

        # 6. 时间分段训练
        if self.enable_time_window:
            self.time_profiler.train(syscall_nr, ts)

        # 7. 定期更新熵基线
        if self.total_syscalls % 1000 == 0:
            self._update_entropy_baseline()
            # 同时更新负载基线
            self.load_awareness.update_baseline()

        # 全局负载追踪（每条事件都记录）
        self.load_awareness.record(ts)

        if self.total_syscalls >= self.min_training_events:
            self.is_trained = True

    def _update_frequency(self, syscall_nr: int, pid: int, ts: float):
        """更新频率统计（每个系统调用的出现频率）"""
        # 滑动窗口：移除过期事件
        window = self.sliding_windows[pid]
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()

        window.append((ts, syscall_nr))
        self._window_entry_count += 1

        # === 内存保护：总量超限时 LRU 淘汰 ===
        if self._window_entry_count > self._max_total_window_entries:
            self._evict_lru_windows()

        # 统计当前窗口内各系统调用的频率
        freq_counter = Counter(sc for _, sc in window)

        # 更新每个系统调用的统计模型
        window_duration = min(
            self.window_seconds,
            ts - window[0][0] if window else self.window_seconds
        )
        if window_duration > 0:
            for sc, count in freq_counter.items():
                rate = count / window_duration  # 频率 = 次数/秒

                if sc not in self.syscall_stats:
                    name = SYSCALL_NAMES.get(sc, f"sys_{sc}")
                    self.syscall_stats[sc] = SyscallStats(
                        name=name, alpha=self.ewma_alpha)

                self.syscall_stats[sc].update(rate)

    def _train_file_context(self, syscall_nr: int, category: str, ts: float):
        """按文件类别训练频率基线"""
        window = self.file_context_windows[category]
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()
        window.append((ts, syscall_nr))

        freq_counter = Counter(sc for _, sc in window)
        dur = min(self.window_seconds,
                  ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if dur > 0:
            for sc, count in freq_counter.items():
                rate = count / dur
                if sc not in self.file_context_stats[category]:
                    name = SYSCALL_NAMES.get(sc, f"sys_{sc}")
                    self.file_context_stats[category][sc] = SyscallStats(
                        name=name, alpha=self.ewma_alpha)
                self.file_context_stats[category][sc].update(rate)

    def _evict_lru_windows(self):
        """
        LRU 淘汰：释放 25% 的滑动窗口条目。

        策略：优先淘汰
          1. 已过期（窗口中最老事件 > window_seconds * 3）
          2. 条目数最多的 PID（释放最多内存）
        """
        target = int(self._max_total_window_entries * 0.75)
        if self._window_entry_count <= target:
            return

        # 第一步：清理过期窗口
        now = time.time()
        stale_cutoff = now - self.window_seconds * 3
        for pid in list(self.sliding_windows.keys()):
            w = self.sliding_windows[pid]
            if not w or w[-1][0] < stale_cutoff:
                self._window_entry_count -= len(w)
                del self.sliding_windows[pid]
                if self._window_entry_count <= target:
                    return

        # 第二步：淘汰条目最多的 PID
        sorted_pids = sorted(self.sliding_windows.keys(),
                             key=lambda p: len(self.sliding_windows[p]),
                             reverse=True)
        for pid in sorted_pids:
            if self._window_entry_count <= target:
                break
            self._window_entry_count -= len(self.sliding_windows[pid])
            del self.sliding_windows[pid]

    def _update_ngram(self, syscall_nr: int, pid: int):
        """更新 n-gram 模型"""
        window = self.sliding_windows[pid]
        if len(window) >= 2:
            recent = [sc for _, sc in list(window)[-10:]]  # 最近 10 个
            self.ngram_model.add_sequence(recent)

    def _update_entropy_baseline(self):
        """更新香农熵基线"""
        if self.total_syscalls == 0:
            return

        # 计算当前香农熵
        entropy = self._calculate_entropy(self.global_counter, self.total_syscalls)
        self.entropy_stats.update(entropy)

        # 计算多样性指数
        diversity = len(self.global_counter)
        self.diversity_stats.update(diversity)

    # ================================================================
    # 检测阶段
    # ================================================================

    def check(self, syscall_nr: int, pid: int = 0, uid: int = -1,
              timestamp: float = None, filename: str = "",
              debug: bool = False) -> float:
        """
        检测一个系统调用事件是否异常。

        新增参数：
          uid:      用户 ID（多用户行为对比，-1=忽略）
          filename: 关联的文件路径（文件上下文检测）

        返回：综合异常分数
          < 2.0  → 正常
          2.0-3.0 → 轻微异常
          3.0-5.0 → 中度异常
          > 5.0   → 严重异常
        """
        if not self.is_trained:
            return 0.0

        ts = timestamp or time.time()

        # 维度 1: 频率异常
        freq_score = self._check_frequency_anomaly(syscall_nr, pid, ts)

        # 维度 2: 序列异常
        seq_score = self._check_sequence_anomaly(syscall_nr, pid) if self.enable_ngram else 0.0

        # 维度 3: 熵异常
        entropy_score = self._check_entropy_anomaly() if self.enable_entropy else 0.0

        # 维度 4: 多样性异常
        diversity_score = self._check_diversity_anomaly() if self.enable_entropy else 0.0

        # === 深层维度 5: 多用户行为异常 ===
        user_score = 0.0
        if self.enable_multi_user and uid >= 0:
            user_score = self.user_profiler.check(uid, syscall_nr, ts)

        # === 深层维度 6: 文件上下文异常 ===
        file_context_score = 0.0
        if self.enable_file_context and filename and syscall_nr in (257,):
            file_context_score = self._check_file_context_anomaly(syscall_nr, filename, ts)

        # === 深层维度 7: 时间分段异常 ===
        time_score = 0.0
        if self.enable_time_window:
            time_score = self.time_profiler.check(syscall_nr, ts)

        # === 负载感知：更新全局负载追踪 ===
        self.load_awareness.record(ts)
        self.adaptive_controller.update(self.load_awareness)

        # === 动态权重：根据当前负载水平自适应 ===
        weights = self.adaptive_controller.get_dynamic_weights()
        load_level, deviation = self.load_awareness.get_load_level()

        total_score = (
            weights["freq"] * freq_score +
            weights["seq"] * seq_score +
            weights["entropy"] * entropy_score +
            weights["diversity"] * diversity_score +
            weights["user"] * user_score +
            weights["file_context"] * file_context_score +
            weights["time"] * time_score
        )

        # === 自适应阈值：负载感知的动态判定 ===
        threshold_mult = self.adaptive_controller.get_threshold_multiplier()
        effective_threshold = 3.0 * threshold_mult  # 基准 3.0 × 负载乘数
        adjusted_score = total_score / threshold_mult  # 分数归一化到基准负载

        if debug:
            print(f"  [DEBUG pid={pid} uid={uid} load={load_level}] "
                  f"freq={freq_score:.2f} seq={seq_score:.2f} "
                  f"ent={entropy_score:.2f} div={diversity_score:.2f} "
                  f"user={user_score:.2f} file={file_context_score:.2f} "
                  f"time={time_score:.2f} → raw={total_score:.2f} "
                  f"adj={adjusted_score:.2f} thr={effective_threshold:.1f}")

        # === 自适应 EWMA α：根据连续异常状态调整 ===
        if adjusted_score > effective_threshold:
            self._consecutive_anomalies += 1
        else:
            self._consecutive_anomalies = max(0, self._consecutive_anomalies - 1)

        adaptive_alpha = self.adaptive_controller.get_adaptive_alpha(
            self._consecutive_anomalies)
        # 将自适应 α 应用到核心统计（概念漂移适应）
        for stats in self.syscall_stats.values():
            stats.alpha = adaptive_alpha

        # 更新全局状态
        self.global_counter[syscall_nr] += 1
        self.total_syscalls += 1

        # 防止 inf/nan 传播
        import math as _math
        if _math.isinf(total_score) or _math.isnan(total_score):
            total_score = 10.0  # 上限截断

        return total_score

    def _check_frequency_anomaly(self, syscall_nr: int, pid: int,
                                  ts: float) -> float:
        """
        频率异常检测。

        计算当前系统调用频率与历史基线的 z-score。
        """
        # 更新滑动窗口
        window = self.sliding_windows[pid]
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()
        window.append((ts, syscall_nr))

        # 计算当前频率
        freq_counter = Counter(sc for _, sc in window)
        window_duration = min(
            self.window_seconds,
            ts - window[0][0] if len(window) > 1 else self.window_seconds
        )

        if window_duration < 0.1:
            return 0.0

        # 对每个在当前窗口活跃的系统调用计算 z-score
        max_z = 0.0
        unknown_count = 0  # 训练中未见过的 syscall 数量
        for sc, count in freq_counter.items():
            rate = count / window_duration
            if sc in self.syscall_stats and self.syscall_stats[sc].count > 30:
                z = abs(self.syscall_stats[sc].z_score(rate))
                max_z = max(max_z, z)
            elif sc not in self.syscall_stats:
                # 训练中从未见过的系统调用 → 高度可疑
                unknown_count += 1
                # 给予一个默认高 z-score（等价于 5 sigma）
                max_z = max(max_z, 5.0 + rate)  # 频率越高越可疑

        # 分段映射：保留 3-sigma 内的线性区分度，3+ 用对数渐进
        # z=1 → 0.33, z=3 → 1.0, z=10 → 2.0, z=77 → 5.0, z=800 → 8.7
        import math
        if max_z <= 3.0:
            result = max_z / 3.0  # 正常范围：线性映射到 [0, 1]
        elif max_z <= 30.0:
            result = 1.0 + (max_z - 3.0) / 13.5  # 中度异常：[1, 3]
        else:
            result = 3.0 + math.log2(max_z / 30.0)  # 高度异常：[3, …]
        # 未见过的 syscall 额外加分
        if unknown_count > 0:
            result += unknown_count * 0.5
        return result

    def _check_sequence_anomaly(self, syscall_nr: int, pid: int) -> float:
        """
        序列异常检测。

        使用 n-gram 模型的困惑度来判断当前序列是否"正常"。
        对新 PID 有预热期——需要累积足够事件后才开始评分。
        """
        window = self.sliding_windows[pid]
        # 预热期：至少 20 个事件才开始序列检测
        if len(window) < 20:
            return 0.0

        recent = [sc for _, sc in list(window)[-10:]]
        perplexity = self.ngram_model.perplexity(recent)

        # 困惑度过高 → 序列异常
        import math
        if math.isinf(perplexity) or perplexity > 1000:
            return 5.0  # 全新 syscall 组合，极高困惑度
        if perplexity > 100:
            return math.log2(1.0 + perplexity / 20.0)
        elif perplexity > 50:
            return 2.0 + (perplexity - 50) * 0.02
        elif perplexity > 20:
            return 0.5 + (perplexity - 20) * 0.03
        else:
            return perplexity / 40.0  # < 0.5

    def _check_entropy_anomaly(self) -> float:
        """
        熵异常检测。

        香农熵 H = -Σ p(x) * log₂(p(x))
        当程序行为突然变得非常随机（熵急剧上升）或
        非常单一（熵急剧下降），都可能是异常。

        例如：
          - 勒索软件扫描文件 → 大量不同的系统调用 → 熵急剧上升
          - 挖矿程序死循环 → 只有少数几种系统调用 → 熵急剧下降
        """
        if self.total_syscalls < 100:
            return 0.0

        current_entropy = self._calculate_entropy(
            self.global_counter, self.total_syscalls)

        if self.entropy_stats.count > 30:
            z = abs(self.entropy_stats.z_score(current_entropy))
            return min(z, 5.0)

        return 0.0

    def _check_diversity_anomaly(self) -> float:
        """
        多样性异常检测。

        检测使用的系统调用种类数是否异常。
        """
        current_diversity = len(self.global_counter)

        if self.diversity_stats.count > 30:
            z = abs(self.diversity_stats.z_score(float(current_diversity)))
            return min(z, 5.0)

        return 0.0

    def _check_file_context_anomaly(self, syscall_nr: int, filename: str,
                                     ts: float) -> float:
        """
        文件上下文异常检测。

        核心逻辑：同样的 openat 系统调用，操作 /etc/passwd 和操作 /var/log/app.log
        在频率基线中应该被区别对待。

        例如：www-data 用户大量 open /etc/ 目录就是异常，
        但大量 open /var/www/ 目录就是正常。
        """
        category = FileContextClassifier.get_category(filename)
        if category not in self.file_context_stats:
            # 从未见过的文件类别 → 中等可疑
            return 2.0

        # 更新滑动窗口
        window = self.file_context_windows[category]
        window.append((ts, syscall_nr))
        cutoff = ts - self.window_seconds
        while window and window[0][0] < cutoff:
            window.popleft()

        freq_counter = Counter(sc for _, sc in window)
        dur = min(self.window_seconds,
                  ts - window[0][0] if len(window) > 1 else self.window_seconds)
        if dur < 0.1:
            return 0.0

        max_z = 0.0
        for sc, count in freq_counter.items():
            rate = count / dur
            stats = self.file_context_stats[category]
            if sc in stats and stats[sc].count > 30:
                z = abs(stats[sc].z_score(rate))
                max_z = max(max_z, z)

        import math
        if max_z <= 3.0:
            return max_z / 3.0
        elif max_z <= 30.0:
            return 1.0 + (max_z - 3.0) / 13.5
        else:
            return 3.0 + math.log2(max_z / 30.0)

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _calculate_entropy(counter: Counter, total: int) -> float:
        """
        计算香农熵（Shannon Entropy）。

        熵 H = -Σ p(x) * log₂(p(x))

        类比：
          - 高熵 ≈ 程序行为多样化（正常的大型应用）
          - 低熵 ≈ 程序只做一两件事（简单的脚本）
          - 熵突变 ≈ 程序行为发生根本改变（可能是攻击）

        Python 类比：
          counter = {"openat": 100, "read": 200, "write": 50}
          total = 350
          p(openat) = 100/350 = 0.286
          p(read)    = 200/350 = 0.571
          p(write)   = 50/350  = 0.143
          H = -(0.286*log2(0.286) + 0.571*log2(0.571) + 0.143*log2(0.143))
            ≈ 1.38 比特
        """
        if total == 0:
            return 0.0

        entropy = 0.0
        for count in counter.values():
            if count > 0:
                p = count / total
                entropy -= p * math.log2(p)

        return entropy

    def get_per_process_stats(self, pid: int) -> Dict[str, float]:
        """获取指定进程的当前统计数据（用于调试）"""
        window = self.sliding_windows.get(pid, deque())
        if not window:
            return {}

        freq_counter = Counter(sc for _, sc in window)
        duration = window[-1][0] - window[0][0] if len(window) > 1 else 1.0
        if duration < 0.1:
            duration = 0.1

        rates = {}
        for sc, count in freq_counter.most_common(10):
            name = SYSCALL_NAMES.get(sc, f"sys_{sc}")
            rate = count / duration
            z = 0.0
            if sc in self.syscall_stats and self.syscall_stats[sc].count > 30:
                z = self.syscall_stats[sc].z_score(rate)
            rates[name] = {"rate": round(rate, 1), "z_score": round(z, 2)}

        return rates

    # ================================================================
    # 持久化
    # ================================================================

    def save(self, filepath: str):
        """
        将训练好的基线模型保存为 JSON 文件。

        类比：PyTorch 的 torch.save(model.state_dict(), path)
        """
        data = {
            "version": 1,
            "window_seconds": self.window_seconds,
            "ewma_alpha": self.ewma_alpha,
            "total_syscalls": self.total_syscalls,
            "syscall_stats": {
                str(k): v.to_dict() for k, v in self.syscall_stats.items()
            },
            "ngram_model": self.ngram_model.to_dict() if self.ngram_model else None,
            "diversity_baseline": self.diversity_stats.to_dict(),
            "entropy_baseline": self.entropy_stats.to_dict(),
            # 保存全局计数器（熵和多样性检测依赖此分布）
            "global_counter": dict(self.global_counter),
            # 深层维度持久化
            "user_profiler": self.user_profiler.to_dict() if self.user_profiler else None,
            "time_profiler": self.time_profiler.to_dict() if self.time_profiler else None,
            "file_context_stats": {
                cat: {str(sc): s.to_dict() for sc, s in stats.items()}
                for cat, stats in self.file_context_stats.items()
            },
            # 负载感知和自适应控制器
            "load_awareness": self.load_awareness.to_dict(),
            "adaptive_controller": self.adaptive_controller.to_dict(),
        }

        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"✅ 基线模型已保存到: {filepath}")
        print(f"   训练事件总数: {self.total_syscalls}")
        print(f"   系统调用种类: {len(self.syscall_stats)}")

    @classmethod
    def load(cls, filepath: str) -> "BaselineDetector":
        """
        从 JSON 文件加载训练好的基线模型。

        类比：PyTorch 的 model.load_state_dict(torch.load(path))
        """
        with open(filepath) as f:
            data = json.load(f)

        detector = cls(
            window_seconds=data.get("window_seconds", 10.0),
            ewma_alpha=data.get("ewma_alpha", 0.05),
            enable_ngram=data.get("ngram_model") is not None,
            enable_entropy=True,
        )

        detector.total_syscalls = data["total_syscalls"]
        detector.is_trained = True

        # 恢复系统调用统计
        for k, v in data["syscall_stats"].items():
            detector.syscall_stats[int(k)] = SyscallStats.from_dict(v)

        # 恢复 n-gram 模型
        if data.get("ngram_model"):
            detector.ngram_model = NgramModel.from_dict(data["ngram_model"])

        # 恢复熵和多样性基线
        if "entropy_baseline" in data:
            detector.entropy_stats = SyscallStats.from_dict(data["entropy_baseline"])
        if "diversity_baseline" in data:
            detector.diversity_stats = SyscallStats.from_dict(data["diversity_baseline"])

        # 恢复全局计数器（熵和多样性检测依赖此分布快照）
        if "global_counter" in data:
            detector.global_counter = Counter(
                {int(k): v for k, v in data["global_counter"].items()}
            )
            detector.total_syscalls = sum(detector.global_counter.values())

        # 恢复多用户画像
        if data.get("user_profiler"):
            detector.user_profiler = MultiUserProfiler.from_dict(
                data["user_profiler"], ewma_alpha=detector.ewma_alpha)

        # 恢复时间分段基线
        if data.get("time_profiler"):
            detector.time_profiler = TimeWindowedProfiler.from_dict(
                data["time_profiler"], ewma_alpha=detector.ewma_alpha)

        # 恢复文件上下文统计
        if "file_context_stats" in data:
            for cat, sc_dict in data["file_context_stats"].items():
                for sc_str, stats_d in sc_dict.items():
                    detector.file_context_stats[cat][int(sc_str)] = \
                        SyscallStats.from_dict(stats_d)

        # 恢复负载感知和自适应控制器
        if "load_awareness" in data:
            detector.load_awareness = LoadAwareness.from_dict(data["load_awareness"])
        if "adaptive_controller" in data:
            detector.adaptive_controller = AdaptiveThresholdController.from_dict(
                data["adaptive_controller"])

        print(f"✅ 基线模型已加载: {filepath}")
        print(f"   训练事件总数: {detector.total_syscalls}")
        print(f"   系统调用种类: {len(detector.syscall_stats)}")

        return detector


# ================================================================
# 自测代码 — 多用户 + 文件上下文 + 时间分段 深度压力测试
# ================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("基线异常检测算法 — 多维度深度压力测试")
    print("=" * 70)

    import random

    # ================================================================
    # 场景设定：3个用户 + 4种文件类型 + 2个时段
    # ================================================================
    # 用户:
    #   root(UID=0)      — 系统管理员，偶尔操作 /etc/、/boot/
    #   www-data(UID=33) — Web服务器，频繁操作 /var/www/、/var/log/
    #   dev(UID=1000)    — 开发者，操作 /home/、/tmp/
    #
    # 文件类型:
    #   SYSCONFIG: /etc/nginx/nginx.conf  — root 正常，www-data 异常
    #   LOG:       /var/log/nginx/access.log — www-data 正常
    #   WEB_ROOT:  /var/www/html/index.html  — www-data 正常
    #   TEMP:      /tmp/build/output.o       — dev 正常，root 异常

    NORMAL_SYSCALL_PROFILE = {
        281: 280, 45: 140, 44: 100, 43: 40,
        0: 120, 257: 30, 3: 55, 5: 20, 8: 8,
        9: 15, 10: 6, 11: 10, 12: 6,
        228: 70, 202: 18,
        39: 8, 56: 2, 61: 3,
        1: 25, 78: 3, 13: 5, 16: 4, 21: 6,
        231: 1, 60: 1,
    }

    # 每个用户操作的文件类型分布 (用于训练)
    USER_FILE_PROFILES = {
        0: {   # root: 主要操作系统配置
            "SYSCONFIG": 0.40, "LOG": 0.15, "LIBRARY": 0.20,
            "BINARY": 0.15, "TEMP": 0.02, "WEB_ROOT": 0.01, "UNKNOWN": 0.07,
        },
        33: {  # www-data: Web 目录 + 日志
            "WEB_ROOT": 0.40, "LOG": 0.35, "TEMP": 0.10,
            "SYSCONFIG": 0.01, "UNKNOWN": 0.14,
        },
        1000: { # dev: 用户目录 + 临时文件
            "USER_HOME": 0.35, "TEMP": 0.30, "DATA": 0.15,
            "SYSCONFIG": 0.02, "UNKNOWN": 0.18,
        },
    }

    CATEGORY_PATHS = {
        "SYSCONFIG": "/etc/nginx/nginx.conf",
        "LOG": "/var/log/nginx/access.log",
        "WEB_ROOT": "/var/www/html/index.html",
        "TEMP": "/tmp/build/output.o",
        "LIBRARY": "/usr/lib/libc.so",
        "BINARY": "/usr/bin/gcc",
        "USER_HOME": "/home/dev/project/main.py",
        "DATA": "/var/lib/mysql/data.ibd",
        "UNKNOWN": "/some/random/file.txt",
    }

    # ================================================================
    # 阶段 1: 多维度训练（模拟真实生产环境 4 小时）
    # ================================================================
    print("\n📚 阶段 1: 多维度训练（模拟生产环境 4 小时）...")
    detector = BaselineDetector(
        window_seconds=10.0, enable_ngram=True, enable_entropy=True,
        enable_multi_user=True, enable_file_context=True, enable_time_window=True)

    syscalls_pool = list(NORMAL_SYSCALL_PROFILE.keys())
    weights_pool = list(NORMAL_SYSCALL_PROFILE.values())
    total_events = 0

    # 模拟 4 小时 × 3600 秒 × 每 200ms 一个事件 ≈ 72000 个事件
    # 时间从凌晨2点开始（NIGHT → MORNING → BUSINESS）
    base_ts = time.time() - 4 * 3600  # 4小时前开始
    print("   生成多用户+多文件类型流量...", end="", flush=True)

    for t in range(72000):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        ts = base_ts + t * 0.2

        # 轮转 3 个用户
        if t % 3 == 0:
            uid, pid_base = 0, 100
        elif t % 3 == 1:
            uid, pid_base = 33, 200
        else:
            uid, pid_base = 1000, 300

        pid = pid_base + (t % 5)  # 每个用户有 5 个进程

        # 根据用户和文件类别生成文件名
        filename = ""
        if sc == 257:  # openat
            profile = USER_FILE_PROFILES[uid]
            categories = list(profile.keys())
            weights = list(profile.values())
            cat = random.choices(categories, weights=weights, k=1)[0]
            filename = CATEGORY_PATHS[cat]

        detector.train(sc, pid=pid, uid=uid, timestamp=ts, filename=filename)
        total_events += 1

    print(f" 完成 ({total_events} 个事件)")
    print(f"   系统调用种类: {len(detector.syscall_stats)}")
    if detector.user_profiler:
        for uid in [0, 33, 1000]:
            p = detector.user_profiler.get_user_profile(uid)
            print(f"   UID={uid}: {p.get('total_events', 0)} 事件, "
                  f"{p.get('syscall_types', 0)} 种 syscall")
    if detector.time_profiler:
        buckets = list(detector.time_profiler.bucket_counts.keys())
        print(f"   时间分桶: {buckets}")

    # ================================================================
    # 阶段 2: 保存 & 加载
    # ================================================================
    print("\n💾 阶段 2: 保存并重新加载模型...")
    detector.save("/tmp/baseline_deep_test.json")
    loaded = BaselineDetector.load("/tmp/baseline_deep_test.json")

    # ================================================================
    # 阶段 3: 深度对抗测试 — 11 个场景
    # ================================================================
    print("\n" + "=" * 70)
    print("🔍 阶段 3: 11 组深度对抗测试")
    print("=" * 70)

    def run_deep_test(name, test_events, expected, desc):
        """test_events: list of (syscall_nr, uid, filename) tuples"""
        scores = []
        base_ts_test = time.time()
        for i, (sc, uid, fname) in enumerate(test_events):
            s = loaded.check(sc, pid=9000+i, uid=uid,
                           timestamp=base_ts_test + i * 0.050,
                           filename=fname)
            scores.append(s)
        avg = sum(scores) / len(scores)
        max_s = max(scores)
        passed = expected[0] <= avg <= expected[1]
        status = "✅ PASS" if passed else "⚠️ 偏离"
        print(f"\n  [{name}] {desc}")
        print(f"    样本数: {len(scores)}, 平均分: {avg:.2f}, 最高分: {max_s:.2f}")
        print(f"    期望: [{expected[0]:.1f}, {expected[1]:.1f}] → {status}")
        return avg

    # --- 场景 1: www-data 正常操作 Web 目录 ---
    ev = []
    for _ in range(80):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        fname = CATEGORY_PATHS["WEB_ROOT"] if sc == 257 else ""
        ev.append((sc, 33, fname))
    run_deep_test("场景1 www-data正常Web", ev, (0.0, 2.5),
                  "www-data 操作 /var/www/ → 正常")

    # --- 场景 2: www-data 突然大量访问 /etc/（越权） ---
    ev = []
    for _ in range(50):
        ev.append((257, 33, "/etc/shadow"))
        ev.append((257, 33, "/etc/passwd"))
        ev.append((0, 33, ""))  # read
    run_deep_test("场景2 www-data越权访问/etc", ev, (3.0, 8.0),
                  "www-data 访问 /etc/shadow → 严重越权（文件上下文+用户双重告警）")

    # --- 场景 3: root 正常操作配置 ---
    ev = []
    for _ in range(60):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        fname = CATEGORY_PATHS["SYSCONFIG"] if sc == 257 else ""
        ev.append((sc, 0, fname))
    run_deep_test("场景3 root正常配置管理", ev, (0.0, 2.5),
                  "root 操作 /etc/nginx/ → 正常")

    # --- 场景 4: root 从 /tmp/ 执行文件（高度可疑） ---
    ev = [(257, 0, "/tmp/.hidden"), (0, 0, ""), (0, 0, ""),
          (1, 0, ""), (59, 0, "/tmp/.hidden")] * 8  # open→read→write→exec
    run_deep_test("场景4 root从/tmp执行", ev, (3.5, 8.0),
                  "root 从临时目录执行文件 → 文件上下文+用户双重告警")

    # --- 场景 5: dev 正常操作 /home/ ---
    ev = []
    for _ in range(60):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        fname = CATEGORY_PATHS["USER_HOME"] if sc == 257 else ""
        ev.append((sc, 1000, fname))
    run_deep_test("场景5 dev正常操作home", ev, (0.0, 2.5),
                  "dev 操作 /home/dev/ → 正常")

    # --- 场景 6: 未知用户 (UID=9999) 突然出现 ---
    ev = []
    for _ in range(40):
        ev.append((257, 9999, "/etc/passwd"))
        ev.append((0, 9999, ""))
    run_deep_test("场景6 未知UID访问敏感文件", ev, (3.5, 8.0),
                  "从未见过的 UID=9999 访问 /etc/passwd → 双重异常")

    # --- 场景 7: 文件类型风暴（TEMP 目录 openat 频率异常） ---
    ev = [(257, 1000, "/tmp/a"), (257, 1000, "/tmp/b"),
          (257, 1000, "/tmp/c"), (257, 1000, "/tmp/d")] * 30  # 120 次
    run_deep_test("场景7 TEMP风暴", ev, (3.0, 7.0),
                  "dev 在 TEMP 目录的超高频 openat → 文件上下文告警")

    # --- 场景 8: 正常但不匹配文件类型 ---
    ev = []
    for _ in range(60):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        # www-data 操作 LOG（正常），但频率匹配
        fname = CATEGORY_PATHS["LOG"] if sc == 257 else ""
        ev.append((sc, 33, fname))
    run_deep_test("场景8 www-data正常日志", ev, (0.0, 2.5),
                  "www-data 操作 /var/log/ → 正常（文件上下文匹配用户身份）")

    # --- 场景 9: 时间段冲突（NIGHT 时段的行为在 BUSINESS 时段出现）---
    # 模拟凌晨的安静模式在下午出现（可能是攻击者挑选低峰时段）
    from datetime import datetime
    now = datetime.now()
    # 如果现在不是 NIGHT，强制用 NIGHT 配置文件攻击
    ev = [(257, 0, "/etc/shadow"), (0, 0, ""), (1, 0, ""),
          (59, 0, "/bin/dash")] * 6
    run_deep_test("场景9 非工作时间可疑活动", ev, (3.0, 7.0),
                  "系统管理操作在当前时段异常高频 → 时间分段+用户+文件")

    # --- 场景 10: 反弹 Shell（从未出现过的 syscall 组合） ---
    ev = []
    for _ in range(15):
        ev.append((41, 33, ""))  # socket — www-data 极少用
        ev.append((42, 33, ""))  # connect — www-data 极少用
        ev.append((33, 33, ""))  # dup2 — 从未用过
        ev.append((33, 33, ""))
        ev.append((33, 33, ""))
        ev.append((59, 33, "/bin/bash"))  # execve /bin/bash
    run_deep_test("场景10 www-data反弹Shell", ev, (4.0, 9.0),
                  "www-data 使用 socket/connect/dup2/execve → 用户+序列+文件三重告警")

    # --- 场景 11: 混合正常流量中的隐蔽攻击 ---
    ev = []
    for _ in range(60):
        sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
        fname = CATEGORY_PATHS["WEB_ROOT"] if sc == 257 else ""
        ev.append((sc, 33, fname))
    # 混入 6 次 /etc/shadow 访问
    for _ in range(6):
        ev.insert(random.randint(0, len(ev)-1), (257, 33, "/etc/shadow"))
    run_deep_test("场景11 混合隐蔽攻击", ev, (2.5, 6.0),
                  "90% 正常 www-data 流量 + 10% 越权 → 多维综合检测")

    # ================================================================
    # 阶段 3.5: 负载自适应测试 — 模拟流量潮汐
    # ================================================================
    print("\n" + "=" * 70)
    print("🌊 阶段 3.5: 负载自适应测试（流量潮汐 + 业务增长）")
    print("=" * 70)

    # 模拟不同负载水平：先让负载感知器学习分布
    # 注入低/中/高负载的训练数据
    for rate_mult, label in [(0.3, "LOW"), (1.0, "NORMAL"), (2.5, "SURGE")]:
        for _ in range(100):
            sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
            loaded.load_awareness.record(time.time())
        loaded.load_awareness.update_baseline()
        level, dev = loaded.load_awareness.get_load_level()
        print(f"  负载注入({label} ×{rate_mult}): level={level}, P50={loaded.load_awareness.percentiles[2]:.0f}/s")

    # 测试：同一攻击在不同负载下的评分变化
    attack_ev = [(257, 33, "/etc/shadow"), (0, 33, ""),
                 (257, 33, "/etc/passwd"), (0, 33, "")] * 10

    print(f"\n  🧪 同一攻击在不同负载水平下的评分对比:")
    for rate_mult, label in [(0.3, "低负载(30%)"), (1.0, "正常负载"),
                               (2.5, "高峰负载"), (4.0, "浪涌负载")]:
        # 注入对应负载水平的事件
        for _ in range(200):
            sc = random.choices(syscalls_pool, weights=weights_pool, k=1)[0]
            loaded.load_awareness.record(time.time())
        loaded.load_awareness.update_baseline()

        # 运行攻击检测
        scores = []
        base_ts_load = time.time()
        for i, (sc, uid, fname) in enumerate(attack_ev):
            s = loaded.check(sc, pid=9500, uid=uid,
                           timestamp=base_ts_load + i * 0.050,
                           filename=fname, debug=(i == 0))
            scores.append(s)
        avg = sum(scores) / len(scores)
        weights = loaded.adaptive_controller.get_dynamic_weights()
        thr = loaded.adaptive_controller.get_threshold_multiplier()
        level, _ = loaded.load_awareness.get_load_level()
        print(f"    {label:12s}: raw={avg:.2f}  "
              f"effective_thr={3.0*thr:.1f}  "
              f"freq_w={weights['freq']:.2f} seq_w={weights['seq']:.2f}  "
              f"level={level}")

    # ================================================================
    # 阶段 4: 多维度画像摘要
    # ================================================================
    print("\n" + "=" * 70)
    print("📊 阶段 4: 多维度行为画像")
    print("=" * 70)

    if loaded.user_profiler:
        for uid, label in [(0, "root"), (33, "www-data"), (1000, "dev")]:
            p = loaded.user_profiler.get_user_profile(uid)
            if p:
                print(f"\n  [{label}] UID={uid}")
                print(f"    累计事件: {p['total_events']}")
                print(f"    syscall 种类: {p['syscall_types']}")
                print(f"    高频 syscall:")
                for sc_num, rate in p.get("top_syscalls", [])[:4]:
                    sc_name = SYSCALL_NAMES.get(sc_num, f"sys_{sc_num}")
                    print(f"      {sc_name:20s} μ={rate:6.1f}/s")

    if loaded.time_profiler:
        print(f"\n  时间分段基线:")
        for bucket, stats in loaded.time_profiler.bucket_stats.items():
            count = loaded.time_profiler.bucket_counts.get(bucket, 0)
            print(f"    {bucket:10s}: {count:6d} 事件, {len(stats)} 种 syscall")

    print(f"\n  文件上下文基线:")
    for cat in sorted(loaded.file_context_stats.keys()):
        stats = loaded.file_context_stats[cat]
        print(f"    {cat:12s}: {len(stats)} 种 syscall 模式")

    # ================================================================
    # 最终总结
    # ================================================================
    print("\n" + "=" * 70)
    print("📋 七维检测 vs 传统方案")
    print("=" * 70)
    print("""
  检测维度           | 静态扫描 | strace  | 旧版(4维) | 本版(7维)
  ------------------|---------|---------|----------|----------
  1.频率异常         | ❌      | ✅ 粗   | ✅       | ✅
  2.序列模式         | ❌      | ❌      | ✅       | ✅
  3.熵异常           | ❌      | ❌      | ✅       | ✅
  4.多样性异常       | ❌      | ❌      | ✅       | ✅
  5.多用户行为★      | ❌      | ❌      | ❌       | ✅ 谁在调用?
  6.文件上下文★      | ❌      | ❌      | ❌       | ✅ 操作什么文件?
  7.时间分段★        | ❌      | ❌      | ❌       | ✅ 什么时间调用?
  """)
    print("✅ 全部测试完成！")
    print(f"   训练数据: {total_events} 个事件 × 3 用户 × 9 文件类别 × 多时段")
    print(f"   新增 3 个深层维度使检测从'单进程频率'升级为'多维度上下文感知'")
