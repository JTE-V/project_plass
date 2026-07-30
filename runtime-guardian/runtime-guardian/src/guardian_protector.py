#!/usr/bin/env python3
"""
Guardian Protector — 动态资源管理 + 防死机保护模块
====================================================
解决生产环境中的三个核心风险：
  1. CPU 飙升 → perf buffer 溢出 → 事件丢失/系统卡死
  2. 内存压力 → 状态机/sliding windows 膨胀 → OOM → 闪退
  3. 进程崩溃 → 监控器自身挂了 → 单点故障 → 所有保护失效

防护层次（纵深防御）：
  ┌─────────────────────────────────────────────┐
  │ 第1层: DynamicSampler     — CPU感知采样率    │
  │ 第2层: BackpressureGuard  — perf buffer背压  │
  │ 第3层: MemoryGuard        — 内存硬限制+LRU   │
  │ 第4层: Watchdog           — 心跳监控+自动重启│
  │ 第5层: GracefulDegrader   — 优雅降级策略     │
  └─────────────────────────────────────────────┘

类比：Kubernetes 的 HPA（水平自动伸缩）+ Pod 的 liveness probe
     - DynamicSampler ≈ HPA（根据负载自动调整）
     - Watchdog ≈ liveness probe（挂了就重启）
     - MemoryGuard ≈ resource limits（防止 OOM）
"""

import os
import sys
import time
import signal
import threading
import logging
import gc
import atexit
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, List, Tuple
from enum import IntEnum

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ================================================================
# 降级级别枚举
# ================================================================
class DegradationLevel(IntEnum):
    """优雅降级级别——数字越大保护越激进"""
    FULL = 0        # 全部 7 维检测
    REDUCED = 1     # 5 维（关闭 ngram + 熵）
    MINIMAL = 2     # 3 维（频率 + 用户 + 文件上下文）
    EMERGENCY = 3   # 仅 eBPF 内核态规则过滤


# ================================================================
# 资源监控器
# ================================================================
class ResourceMonitor:
    """
    实时 CPU / 内存监控。

    类比：Linux 的 top 命令，但每个周期采集一次而非持续轮询
          Node.js 的 process.memoryUsage() + os.loadavg()
    """

    def __init__(self, check_interval: float = 1.0, history_size: int = 60):
        self.check_interval = check_interval
        self._lock = threading.Lock()

        # 当前进程的 psutil 对象
        self._process = psutil.Process() if HAS_PSUTIL else None

        # 历史记录（用于趋势判断）
        self.cpu_history: deque = deque(maxlen=history_size)
        self.mem_history: deque = deque(maxlen=history_size)

        # 当前值
        self.current_cpu_pct: float = 0.0
        self.current_mem_mb: float = 0.0
        self.current_mem_pct: float = 0.0

        # 系统级指标
        self.system_cpu_pct: float = 0.0
        self.system_mem_pct: float = 0.0

        self._running = False
        self._started = False

    def start(self):
        """启动后台监控线程"""
        if not HAS_PSUTIL:
            return
        self._running = True
        self._started = True
        t = threading.Thread(target=self._monitor_loop, daemon=True, name="resource-monitor")
        t.start()

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            try:
                if self._process:
                    with self._lock:
                        self.current_cpu_pct = self._process.cpu_percent(interval=0.1)
                        mem_info = self._process.memory_info()
                        self.current_mem_mb = mem_info.rss / (1024 * 1024)
                        self.current_mem_pct = self._process.memory_percent()

                        self.system_cpu_pct = psutil.cpu_percent(interval=0)
                        self.system_mem_pct = psutil.virtual_memory().percent

                        self.cpu_history.append(self.current_cpu_pct)
                        self.mem_history.append(self.current_mem_mb)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            time.sleep(self.check_interval)

    @property
    def cpu_trend(self) -> str:
        """CPU 趋势：rising / falling / stable"""
        if len(self.cpu_history) < 10:
            return "stable"
        recent = list(self.cpu_history)[-10:]
        first_half = sum(recent[:5]) / 5
        second_half = sum(recent[5:]) / 5
        if second_half > first_half * 1.2:
            return "rising"
        elif second_half < first_half * 0.8:
            return "falling"
        return "stable"

    @property
    def mem_trend(self) -> str:
        """内存趋势"""
        if len(self.mem_history) < 10:
            return "stable"
        recent = list(self.mem_history)[-10:]
        first_half = sum(recent[:5]) / 5
        second_half = sum(recent[5:]) / 5
        if second_half > first_half * 1.1:
            return "rising"   # 内存泄漏风险
        elif second_half < first_half * 0.9:
            return "falling"
        return "stable"

    def get_snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu_pct": self.current_cpu_pct,
                "mem_mb": self.current_mem_mb,
                "mem_pct": self.current_mem_pct,
                "system_cpu": self.system_cpu_pct,
                "system_mem": self.system_mem_pct,
                "cpu_trend": self.cpu_trend,
                "mem_trend": self.mem_trend,
            }


# ================================================================
# 动态采样控制器
# ================================================================
class DynamicSampler:
    """
    CPU 感知的动态采样率。

    策略：
      CPU < 30%  → 采样率 100%（全量检测）
      CPU 30-60% → 采样率 70%
      CPU 60-80% → 采样率 40%
      CPU 80-95% → 采样率 10%，仅保留高危 syscall
      CPU > 95%  → 采样率 5%，紧急模式

    类比：视频流的自适应码率（ABR）
          网络好时 1080p，网络差时自动降到 360p
    """

    # 高危系统调用（任何负载下都优先保留）
    HIGH_PRIORITY_SYSCALLS = {
        59,   # execve  — 进程执行（最高优先级）
        257,  # openat  — 文件访问
        42,   # connect — 网络连接
        41,   # socket  — 创建套接字
        56,   # clone   — 创建线程/进程
        105,  # setuid  — 提权
        106,  # setgid  — 提权
    }

    def __init__(self):
        self.sample_rate: float = 1.0  # 当前采样率 (0.0 ~ 1.0)
        self._counter: int = 0

    def update(self, cpu_pct: float):
        """根据 CPU 使用率更新采样率"""
        if cpu_pct < 30:
            self.sample_rate = 1.0
        elif cpu_pct < 60:
            self.sample_rate = 0.7
        elif cpu_pct < 80:
            self.sample_rate = 0.4
        elif cpu_pct < 95:
            self.sample_rate = 0.1
        else:
            self.sample_rate = 0.05

    def should_process(self, syscall_nr: int) -> bool:
        """
        判断是否应该处理这个事件。

        - 高危 syscall 始终处理（不管采样率）
        - 其他按采样率随机决定
        """
        if syscall_nr in self.HIGH_PRIORITY_SYSCALLS:
            return True

        if self.sample_rate >= 1.0:
            return True

        # 确定性采样（基于计数器而非随机，性能更好）
        self._counter += 1
        return (self._counter % int(1.0 / self.sample_rate)) == 0

    @property
    def is_degraded(self) -> bool:
        return self.sample_rate < 1.0


# ================================================================
# 背压保护器
# ================================================================
class BackpressureGuard:
    """
    perf ring buffer 背压保护。

    问题：eBPF 内核态产生事件速度 > 用户态消费速度 → buffer 溢出 → 丢事件
    解决：监控消费延迟 + 队列深度，超阈值时触发保护

    类比：TCP 的拥塞控制（congestion control）
          接收窗口满了 → 通知发送方减速
    """

    def __init__(self, max_queue_depth: int = 10000, warn_depth: int = 5000):
        self.max_queue_depth = max_queue_depth
        self.warn_depth = warn_depth

        # 事件处理延迟窗口（秒）
        self._latency_window: deque = deque(maxlen=100)
        self._queue_depth: int = 0
        self._overflow_count: int = 0
        self._last_warn_time: float = 0

    def record_latency(self, latency_sec: float):
        """记录单次事件处理延迟"""
        self._latency_window.append(latency_sec)

    def set_queue_depth(self, depth: int):
        self._queue_depth = depth

    @property
    def avg_latency(self) -> float:
        if not self._latency_window:
            return 0.0
        return sum(self._latency_window) / len(self._latency_window)

    @property
    def p99_latency(self) -> float:
        if len(self._latency_window) < 100:
            return self.avg_latency
        sorted_l = sorted(self._latency_window)
        return sorted_l[int(len(sorted_l) * 0.99)]

    @property
    def is_under_pressure(self) -> bool:
        """是否处于背压状态"""
        return (self._queue_depth > self.warn_depth or
                self.avg_latency > 0.1)  # 平均延迟 > 100ms

    @property
    def is_critical(self) -> bool:
        """是否处于严重背压"""
        return (self._queue_depth > self.max_queue_depth or
                self.p99_latency > 0.5)  # P99 延迟 > 500ms

    def should_throttle(self) -> bool:
        """是否需要限流（丢弃低优先级事件）"""
        if self.is_critical:
            return True
        if self.is_under_pressure and self._queue_depth > self.warn_depth:
            # 每 5 秒最多告警一次
            now = time.time()
            if now - self._last_warn_time > 5:
                self._last_warn_time = now
            return True
        return False

    def get_stats(self) -> dict:
        return {
            "avg_latency_ms": self.avg_latency * 1000,
            "p99_latency_ms": self.p99_latency * 1000,
            "queue_depth": self._queue_depth,
            "overflow_count": self._overflow_count,
            "under_pressure": self.is_under_pressure,
        }


# ================================================================
# 内存保护器
# ================================================================
class MemoryGuard:
    """
    内存硬限制保护器。

    策略：
      1. 软限制（warn_mb）：触发 LRU 淘汰、强制 GC
      2. 硬限制（max_mb）：触发紧急模式降级、拒绝新事件
      3. 泄漏检测：mem_trend == rising 持续 60s → 触发告警

    类比：JVM 的 -Xmx（最大堆）+ GC
          Redis 的 maxmemory-policy allkeys-lru
    """

    def __init__(self, warn_mb: float = 200, max_mb: float = 500):
        self.warn_mb = warn_mb
        self.max_mb = max_mb
        self._rising_start: Optional[float] = None  # 内存连续上升的起始时间

    def check(self, current_mem_mb: float, mem_trend: str) -> dict:
        """
        检查内存状态，返回操作指令。

        返回:
          { "level": "ok"|"warn"|"critical",
            "action": "none"|"gc"|"lru_evict"|"reject_new"|"emergency" }
        """
        if current_mem_mb > self.max_mb:
            return {"level": "critical", "action": "emergency",
                    "msg": f"内存 {current_mem_mb:.0f}MB 超过硬限制 {self.max_mb}MB"}

        if current_mem_mb > self.warn_mb:
            if mem_trend == "rising":
                return {"level": "warn", "action": "lru_evict",
                        "msg": f"内存 {current_mem_mb:.0f}MB 超软限制且持续上升"}
            return {"level": "warn", "action": "gc",
                    "msg": f"内存 {current_mem_mb:.0f}MB 超过软限制 {self.warn_mb}MB"}

        # 泄漏检测
        if mem_trend == "rising":
            if self._rising_start is None:
                self._rising_start = time.time()
            elif time.time() - self._rising_start > 60:
                return {"level": "warn", "action": "leak_alert",
                        "msg": f"内存持续上升超过 60s，可能存在泄漏"}
        else:
            self._rising_start = None

        return {"level": "ok", "action": "none", "msg": ""}


# ================================================================
# 看门狗
# ================================================================
class Watchdog:
    """
    心跳看门狗。

    原理：
      1. 主循环每次迭代喂狗（heartbeat）
      2. 独立线程定期检查心跳
      3. 超时未喂 → 认为主循环卡死 → 执行紧急动作

    类比：MCU 的硬件看门狗定时器
          systemd 的 WatchdogSec=
    """

    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds
        self._last_heartbeat: float = time.time()
        self._lock = threading.Lock()
        self._running = False
        self._alert_count: int = 0
        # 紧急回调（主循环卡死时调用）
        self._emergency_callbacks: List[Callable] = []

    def register_emergency(self, callback: Callable):
        """注册紧急回调（主循环卡死时执行）"""
        self._emergency_callbacks.append(callback)

    def heartbeat(self):
        """喂狗——主循环每次迭代调用"""
        with self._lock:
            self._last_heartbeat = time.time()

    def start(self):
        """启动看门狗线程"""
        self._running = True
        t = threading.Thread(target=self._watch_loop, daemon=True, name="watchdog")
        t.start()

    def stop(self):
        self._running = False

    def _watch_loop(self):
        while self._running:
            time.sleep(min(5.0, self.timeout_seconds / 3))
            with self._lock:
                elapsed = time.time() - self._last_heartbeat
            if elapsed > self.timeout_seconds:
                self._alert_count += 1
                self._on_timeout()

    def _on_timeout(self):
        """主循环超时处理"""
        logging.error(f"⚠️ 看门狗超时！主循环已卡死 "
                      f"{time.time() - self._last_heartbeat:.0f}s")
        for callback in self._emergency_callbacks:
            try:
                callback()
            except Exception:
                pass

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return (time.time() - self._last_heartbeat) < self.timeout_seconds

    @property
    def last_heartbeat_age(self) -> float:
        with self._lock:
            return time.time() - self._last_heartbeat


# ================================================================
# 优雅降级调度器
# ================================================================
class GracefulDegrader:
    """
    优雅降级调度器。

    不是"崩了再重启"——而是"在崩之前主动降低服务质量以保证存活"。

    降级决策矩阵：
      ┌──────────┬──────────┬──────────┬─────────────┐
      │          │ CPU<60%  │ CPU60-85%│ CPU>85%     │
      ├──────────┼──────────┼──────────┼─────────────┤
      │ Mem<200M │ FULL     │ REDUCED  │ MINIMAL     │
      │ Mem200M+ │ REDUCED  │ MINIMAL  │ EMERGENCY   │
      │ 背压严重 │ MINIMAL  │ MINIMAL  │ EMERGENCY   │
      └──────────┴──────────┴──────────┴─────────────┘
    """

    def __init__(self):
        self.current_level = DegradationLevel.FULL
        self.level_history: deque = deque(maxlen=20)
        self._level_lock = threading.Lock()

    def evaluate(self, resource: ResourceMonitor,
                 backpressure: BackpressureGuard,
                 memory: MemoryGuard) -> DegradationLevel:
        """评估当前应该使用的降级级别"""
        snap = resource.get_snapshot()
        cpu = snap["cpu_pct"]
        mem = snap["mem_mb"]

        # 紧急情况优先
        if backpressure.is_critical:
            new_level = DegradationLevel.EMERGENCY
        elif cpu > 85 or mem > 500:
            new_level = DegradationLevel.EMERGENCY
        elif backpressure.is_under_pressure:
            new_level = DegradationLevel.MINIMAL
        elif cpu > 60 or mem > 200:
            new_level = DegradationLevel.REDUCED
        else:
            new_level = DegradationLevel.FULL

        with self._level_lock:
            old = self.current_level
            self.current_level = new_level
            self.level_history.append(new_level)

        return new_level

    @property
    def level(self) -> DegradationLevel:
        with self._level_lock:
            return self.current_level

    def should_disable_ngram(self) -> bool:
        return self.level >= DegradationLevel.REDUCED

    def should_disable_entropy(self) -> bool:
        return self.level >= DegradationLevel.REDUCED

    def should_disable_user_profiling(self) -> bool:
        return self.level >= DegradationLevel.MINIMAL

    def should_disable_file_context(self) -> bool:
        return self.level >= DegradationLevel.EMERGENCY

    def should_disable_time_window(self) -> bool:
        return self.level >= DegradationLevel.EMERGENCY

    def get_active_dimensions(self) -> List[str]:
        """返回当前激活的检测维度"""
        dims = ["freq"]
        if self.level < DegradationLevel.REDUCED:
            dims.extend(["seq", "entropy", "diversity"])
        if self.level < DegradationLevel.MINIMAL:
            dims.extend(["user", "file_context"])
        if self.level < DegradationLevel.EMERGENCY:
            dims.append("time")
        return dims

    def get_stats(self) -> dict:
        with self._level_lock:
            return {
                "current_level": self.current_level.name,
                "level_value": int(self.current_level),
                "active_dimensions": self.get_active_dimensions(),
            }


# ================================================================
# 保护器门面——组合所有保护机制
# ================================================================
class GuardianProtector:
    """
    保护器门面。

    使用方式：
      protector = GuardianProtector(max_mem_mb=300)
      protector.start()

      while running:
          protector.watchdog.heartbeat()
          event = get_next_event()
          if protector.dynamic_sampler.should_process(event.syscall_nr):
              process(event)
          protector.backpressure.record_latency(latency)
    """

    def __init__(self,
                 max_mem_mb: float = 300,
                 warn_mem_mb: float = 150,
                 watchdog_timeout: float = 30.0,
                 max_queue_depth: int = 8000):
        self.resource_monitor = ResourceMonitor()
        self.dynamic_sampler = DynamicSampler()
        self.backpressure = BackpressureGuard(max_queue_depth=max_queue_depth)
        self.memory_guard = MemoryGuard(warn_mb=warn_mem_mb, max_mb=max_mem_mb)
        self.watchdog = Watchdog(timeout_seconds=watchdog_timeout)
        self.degrader = GracefulDegrader()

        self._protector_thread: Optional[threading.Thread] = None
        self._running = False

        # 紧急回调：主循环卡死时保存状态后重启
        self.watchdog.register_emergency(self._on_watchdog_timeout)

    def start(self):
        """启动所有保护机制"""
        self._running = True
        self.resource_monitor.start()
        self.watchdog.start()
        self._protector_thread = threading.Thread(
            target=self._protection_loop, daemon=True, name="guardian-protector")
        self._protector_thread.start()

    def stop(self):
        self._running = False
        self.resource_monitor.stop()
        self.watchdog.stop()

    def _protection_loop(self):
        """保护循环——定期评估并执行保护动作"""
        while self._running:
            time.sleep(2.0)

            snap = self.resource_monitor.get_snapshot()

            # 1. 更新动态采样率
            self.dynamic_sampler.update(snap["cpu_pct"])

            # 2. 内存检查 + 执行保护动作
            mem_result = self.memory_guard.check(snap["mem_mb"], snap["mem_trend"])
            if mem_result["action"] == "gc":
                gc.collect()
            elif mem_result["action"] == "lru_evict":
                gc.collect()
                # 通知外部执行 LRU 淘汰（通过回调）
                if hasattr(self, '_on_lru_evict'):
                    self._on_lru_evict()
            elif mem_result["action"] == "emergency":
                logging.critical(mem_result["msg"])
                # 紧急内存释放
                gc.collect()
                if hasattr(self, '_on_emergency'):
                    self._on_emergency()

            # 3. 更新降级级别
            new_level = self.degrader.evaluate(
                self.resource_monitor, self.backpressure, self.memory_guard)

            # 4. 级别变化时记录日志
            if (hasattr(self, '_last_level') and
                    self._last_level != new_level):
                logging.warning(f"降级级别变更: {self._last_level.name} → "
                                f"{new_level.name}")
            self._last_level = new_level

    def on_lru_evict(self, callback: Callable):
        """注册 LRU 淘汰回调"""
        self._on_lru_evict = callback

    def on_emergency(self, callback: Callable):
        """注册紧急回调"""
        self._on_emergency = callback

    def _on_watchdog_timeout(self):
        """看门狗超时——保存状态并尝试优雅退出"""
        logging.critical("看门狗超时——主循环卡死！")
        # 尝试保存当前基线
        try:
            if hasattr(self, '_save_baseline'):
                self._save_baseline()
        except Exception:
            pass

    def get_health_report(self) -> dict:
        """返回完整健康报告"""
        return {
            "resource": self.resource_monitor.get_snapshot(),
            "sampler": {"rate": self.dynamic_sampler.sample_rate,
                        "degraded": self.dynamic_sampler.is_degraded},
            "backpressure": self.backpressure.get_stats(),
            "degradation": self.degrader.get_stats(),
            "watchdog": {"alive": self.watchdog.is_alive,
                         "last_heartbeat": self.watchdog.last_heartbeat_age},
        }


# ================================================================
# 自测
# ================================================================
if __name__ == "__main__":
    if not HAS_PSUTIL:
        print("⚠️ psutil 未安装，资源监控不可用")
        print("   pip install psutil")
        sys.exit(0)

    print("=" * 60)
    print("Guardian Protector — 保护机制自测")
    print("=" * 60)

    protector = GuardianProtector(max_mem_mb=500, watchdog_timeout=5.0)
    protector.start()

    print("\n🧪 模拟正常运行 5 秒...")
    # 后台喂狗线程（模拟主循环持续运行）
    import threading as _thr
    _feeding = True
    def _feed_loop():
        while _feeding:
            protector.watchdog.heartbeat()
            time.sleep(0.5)
    _thr.Thread(target=_feed_loop, daemon=True).start()

    for i in range(5):
        time.sleep(1.0)

        report = protector.get_health_report()
        r = report["resource"]
        print(f"  [{i+1}s] CPU={r['cpu_pct']:.1f}% Mem={r['mem_mb']:.0f}MB "
              f"Sampling={report['sampler']['rate']:.0%} "
              f"Level={report['degradation']['current_level']} "
              f"Latency={report['backpressure']['avg_latency_ms']:.1f}ms")

    _feeding = False

    print(f"\n📊 最终健康报告:")
    report = protector.get_health_report()
    for section, data in report.items():
        print(f"  [{section}]")
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"    {k}: {v}")

    protector.stop()
    print("\n✅ 保护机制自测完成")
