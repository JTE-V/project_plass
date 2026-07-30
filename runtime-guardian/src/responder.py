#!/usr/bin/env python3
"""
Runtime Guardian — 自动响应模块 (Responder)
=============================================
当检测引擎发现可疑行为时，本模块根据配置的响应策略自动执行
防御动作。支持从温和的日志记录到强力的进程隔离，按严重程度递
增的多级响应体系。

响应策略（按严重程度递增）：
  1. LOG        — 仅记录日志（零侵入）
  2. SIGSTOP    — 暂停进程（冻结，可恢复）
  3. SIGKILL    — 终止进程（不可恢复）
  4. BLOCK_IP   — 使用 iptables 阻断外连 IP
  5. QUARANTINE — cgroup freezer + 限制 CPU/内存（深度隔离）

核心设计理念（类比说明）：
  ┌─────────────────┬──────────────────────┬──────────────────────┐
  │ 本模块概念       │ JavaScript/Node.js    │ Python               │
  ├─────────────────┼──────────────────────┼──────────────────────┤
  │ 响应策略链       │ Promise.all() 并发    │ asyncio.gather()     │
  │ 白名单           │ Set.has() 快速查找    │ set/dict 成员检查    │
  │ 冷却时间         │ setTimeout 防抖       │ threading.Timer      │
  │ JSON 日志        │ console.log(JSON)     │ json.dumps + print   │
  │ dry_run 模式     │ NODE_ENV=development  │ unittest.mock.patch  │
  │ cgroup 操作      │ fs.writeFileSync      │ open().write()       │
  └─────────────────┴──────────────────────┴──────────────────────┘

运行方式（命令行自测）：
  # 仅记录日志
  python3 responder.py --pid 1234 --action LOG --reason "test_alert"

  # 终止进程
  python3 responder.py --pid 1234 --action SIGKILL --reason "ransomware_detected"

  # 阻断 IP（需要 root）
  sudo python3 responder.py --pid 1234 --action BLOCK_IP \\
      --reason "c2_communication" --target-ip 10.0.0.99

  # 隔离进程（需要 root）
  sudo python3 responder.py --pid 1234 --action QUARANTINE \\
      --reason "suspicious_behavior"

  # 试运行模式（仅打印不执行）
  python3 responder.py --pid 1234 --action SIGKILL --dry-run

  # 多动作链
  python3 responder.py --pid 1234 --action LOG,SIGSTOP,SIGKILL \\
      --reason "critical_threat"
"""

import os
import sys
import json
import time
import signal as _signal  # 与内置 signal 模块区分（类似 JS 的 import * as signal）
import subprocess
import argparse
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Union, Any, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

# ================================================================
# 类型别名与常量（类似 TypeScript 的 type / const 声明）
# ================================================================

# 进程标识：可以是 PID（int）、进程名（str）、或 UID（int）
ProcessIdentifier = Union[int, str]

# 响应动作回调：签名为 (pid: int, reason: str, metadata: dict) -> bool
# 返回 True 表示成功，False 表示失败
ResponseCallback = Callable[[int, str, dict], bool]

# cgroup 根路径（类似 Linux 的 /sys/fs/cgroup，不同发行版可能不同）
CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_FREEZER_PATH = CGROUP_ROOT / "freezer"
DEFAULT_CGROUP_NAME = "runtime-guardian"

# iptables 链名
IPTABLES_CHAIN = "RUNTIME-GUARDIAN"

# 默认冷却时间（秒）—— 同一进程在窗口内不重复响应
DEFAULT_COOLDOWN_SECONDS = 60


# ================================================================
# 响应动作枚举（类似 TypeScript 的 enum，Java 的 enum）
# 按严重程度递增排列：数值越大越严重
# ================================================================

class ResponseAction(Enum):
    """
    响应动作枚举。

    类比：
      - JS/TS:  enum ResponseAction { LOG, SIGSTOP, SIGKILL, BLOCK_IP, QUARANTINE }
      - Python: 标准库 enum.Enum 更强大，可附带行为和元数据
      - Rust:   enum ResponseAction { Log, Sigstop, Sigkill, ... }
    """
    LOG = (0, "仅记录日志", False)
    SIGSTOP = (1, "发送 SIGSTOP 暂停进程", True)
    SIGKILL = (2, "发送 SIGKILL 终止进程", True)
    BLOCK_IP = (3, "使用 iptables 阻断外连 IP", True)
    QUARANTINE = (4, "cgroup freezer + 限制 CPU/内存", True)

    def __init__(self, severity: int, description: str, requires_root: bool):
        """
        Args:
            severity: 严重程度（0=最温和，4=最严厉）
            description: 中文说明
            requires_root: 是否需要 root 权限
        """
        self.severity = severity
        self.description = description
        self.requires_root = requires_root

    @classmethod
    def from_string(cls, name: str) -> "ResponseAction":
        """
        从字符串解析响应动作（大小写不敏感）。

        类似 JS 的:  ResponseAction[name.toUpperCase()]
        """
        try:
            return cls[name.upper()]
        except KeyError:
            valid = ", ".join(a.name for a in cls)
            raise ValueError(f"未知响应动作 '{name}'，支持: {valid}")

    def __str__(self) -> str:
        return self.name


# ================================================================
# 数据结构：告警记录与响应结果
# ================================================================

@dataclass
class Alert:
    """
    一条告警记录。

    类似：
      - JS:   const alert = { pid, action, reason, timestamp, metadata }
      - Python: dataclass ≈ JS 的普通 object + JSDoc 类型注解
    """
    pid: int                          # 目标进程 PID
    action: ResponseAction            # 要执行的响应动作
    reason: str                       # 告警原因（人类可读）
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata 示例: {"process_name": "node", "uid": 1000, "target_ip": "10.0.0.99",
    #                 "syscall": "connect", "score": 0.95}


@dataclass
class ResponseResult:
    """
    单个响应动作的执行结果。

    类似：
      - JS:   const result = { action, success, message, duration_ms }
      - Go:   type ResponseResult struct { Action string; Success bool; ... }
    """
    action: ResponseAction
    success: bool
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "action": self.action.name,
            "success": self.success,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class WhitelistEntry:
    """
    白名单条目 —— 匹配条件为"或"关系。

    类似 JS 的:  { type: 'pid'|'name'|'uid', value: any }
    """
    entry_type: str   # "pid" | "name" | "uid"
    value: Any        # 对应的值


# ================================================================
# JSON 日志格式化器
# ================================================================

class JsonFormatter(logging.Formatter):
    """
    将日志记录格式化为 JSON 行。

    每条日志一行 JSON（类似 Node.js 的 pino / winston JSON 格式）。
    这种格式便于被 ELK、Splunk、jq 等工具消费。

    类比：
      - JS:  pino() 或 winston.format.json()
      - Python: python-json-logger 库，这里手写精简版
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        # 附加额外字段（如 pid, action 等）
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)
        return json.dumps(log_entry, ensure_ascii=False, default=str)


# ================================================================
# Responder 类 —— 核心自动响应引擎
# ================================================================

class Responder:
    """
    自动响应引擎。

    职责：
      1. 接收告警 → 查询白名单 → 检查冷却时间 → 执行响应链
      2. 管理白名单（PID/进程名/UID）
      3. 管理冷却时间窗口
      4. 输出 JSON 格式日志
      5. 支持 dry_run 模式

    使用示例（类似 JS 的类实例化）：
        responder = Responder(dry_run=False, cooldown_seconds=60)
        responder.add_to_whitelist(pid=1)          # 永不响应 PID 1
        responder.add_to_whitelist(name="sshd")    # 永不响应 sshd
        responder.handle_alert(Alert(pid=1234, action=ResponseAction.SIGKILL,
                                     reason="ransomware_detected"))

    生命周期：
      - 创建 → 配置白名单 → handle_alert() 循环 → shutdown()
      - 类似 JS 的 EventEmitter 生命周期：on → emit → removeListener
    """

    # 允许无 root 时优雅降级的动作集合
    # 类似 JS 的 feature detection: if (!fs.existsSync('/sys/fs/cgroup')) { ... }
    _HARMLESS_ACTIONS = {ResponseAction.LOG}

    def __init__(
        self,
        dry_run: bool = False,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        logger_name: str = "runtime-guardian.responder",
    ):
        """
        初始化响应器。

        Args:
            dry_run: True 时仅打印日志不实际执行（类似 --dry-run / 演习模式）
            cooldown_seconds: 同一进程两次响应之间的最小间隔（秒）
            logger_name: 日志记录器名称

        类比：
          - JS:  new Responder({ dryRun: true, cooldownMs: 60000 })
          - Python: Responder(dry_run=True, cooldown_seconds=60)
        """
        self.dry_run = dry_run
        self.cooldown_seconds = cooldown_seconds

        # ---- 白名单 ----
        # 使用集合保证 O(1) 查找（类似 JS 的 Set / Map）
        self._whitelist_pids: Set[int] = set()       # 白名单 PID
        self._whitelist_names: Set[str] = set()      # 白名单进程名
        self._whitelist_uids: Set[int] = set()       # 白名单 UID

        # ---- 冷却时间追踪 ----
        # key=pid, value=上次响应时间戳
        # 类似 JS 的 Map<number, Date>，或 Redis 的 TTL key
        self._cooldown_map: Dict[int, datetime] = {}
        self._cooldown_lock = threading.Lock()        # 线程安全锁

        # ---- 响应动作注册表 ----
        # 每个 ResponseAction 映射到一个可调用对象
        # 类似 JS 的策略模式: const strategies = { LOG: () => {...}, SIGKILL: () => {...} }
        self._action_handlers: Dict[ResponseAction, ResponseCallback] = {}
        self._register_default_handlers()

        # ---- 日志 ----
        self.logger = self._setup_logger(logger_name)

        # ---- 统计 ----
        self.stats: Dict[str, int] = {
            "total_alerts": 0,
            "whitelist_hits": 0,
            "cooldown_hits": 0,
            "actions_executed": 0,
            "actions_failed": 0,
            "actions_skipped_dry_run": 0,
        }

        self.logger.info(
            "Responder 初始化完成",
            extra={"extra_fields": {
                "dry_run": dry_run,
                "cooldown_seconds": cooldown_seconds,
                "mode": "DRY-RUN (演习)" if dry_run else "LIVE (生产)",
            }},
        )

    # ----------------------------------------------------------
    # 日志配置
    # ----------------------------------------------------------

    def _setup_logger(self, name: str) -> logging.Logger:
        """
        创建带 JSON 格式化的日志记录器。

        类似 JS 的:
          const logger = pino({ level: 'info' });
          logger.info({ dry_run: true }, 'Responder init');
        """
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # 不向根 logger 传播（避免重复输出）

        # 仅在没有 handler 时添加（防止重复添加）
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)

        return logger

    # ----------------------------------------------------------
    # 白名单管理（公开 API）
    # ----------------------------------------------------------

    def add_to_whitelist(
        self,
        pid: Optional[int] = None,
        name: Optional[str] = None,
        uid: Optional[int] = None,
    ) -> None:
        """
        将进程加入白名单。

        白名单中的进程永远不会触发自动响应。可以按 PID、进程名或 UID 添加。
        一次调用可以同时添加多种匹配条件。

        Args:
            pid: 进程 ID（如 1 = systemd/init）
            name: 进程名（如 "sshd", "systemd"）
            uid: 用户 ID（如 0 = root, 1000 = 普通用户）

        类比：
          - JS:   whitelist.add({ type: 'pid', value: 1 })
          - 防火墙: iptables -A INPUT -s 127.0.0.1 -j ACCEPT

        Example:
            responder.add_to_whitelist(pid=1)                 # systemd
            responder.add_to_whitelist(name="sshd")           # SSH 守护进程
            responder.add_to_whitelist(uid=0)                 # root 用户所有进程
        """
        if pid is not None:
            self._whitelist_pids.add(pid)
            self.logger.debug(f"白名单添加 PID={pid}")

        if name is not None:
            self._whitelist_names.add(name)
            self.logger.debug(f"白名单添加进程名={name}")

        if uid is not None:
            self._whitelist_uids.add(uid)
            self.logger.debug(f"白名单添加 UID={uid}")

    def remove_from_whitelist(
        self,
        pid: Optional[int] = None,
        name: Optional[str] = None,
        uid: Optional[int] = None,
    ) -> None:
        """从白名单中移除。参数同 add_to_whitelist。"""
        if pid is not None:
            self._whitelist_pids.discard(pid)
        if name is not None:
            self._whitelist_names.discard(name)
        if uid is not None:
            self._whitelist_uids.discard(uid)

    def is_whitelisted(self, pid: int, process_name: str = "", uid: int = -1) -> bool:
        """
        检查进程是否在白名单中。

        Args:
            pid: 进程 PID
            process_name: 进程名（可选）
            uid: 进程的 UID（可选，-1 表示未知）

        Returns:
            True 表示在白名单中，不应响应。

        类比 JS 的:
          whitelistPids.has(pid) || whitelistNames.has(name) || whitelistUids.has(uid)
        """
        if pid in self._whitelist_pids:
            return True
        if process_name and process_name in self._whitelist_names:
            return True
        if uid >= 0 and uid in self._whitelist_uids:
            return True
        return False

    # ----------------------------------------------------------
    # 冷却时间管理
    # ----------------------------------------------------------

    def is_in_cooldown(self, pid: int) -> bool:
        """
        检查进程是否在冷却期内。

        冷却期 = 上一次对此 PID 响应后，尚未过去 cooldown_seconds 秒。
        类似 JS 的防抖（debounce）逻辑，但时间窗口更长。

        Args:
            pid: 进程 PID

        Returns:
            True 表示在冷却中，应跳过本次响应。
        """
        with self._cooldown_lock:
            if pid not in self._cooldown_map:
                return False
            last_time = self._cooldown_map[pid]
            elapsed = (datetime.now() - last_time).total_seconds()
            return elapsed < self.cooldown_seconds

    def _set_cooldown(self, pid: int) -> None:
        """记录/更新进程的最后响应时间（进入冷却期）。"""
        with self._cooldown_lock:
            self._cooldown_map[pid] = datetime.now()

    def reset_cooldown(self, pid: int) -> None:
        """手动重置某个进程的冷却时间（强制允许下次立即响应）。"""
        with self._cooldown_lock:
            self._cooldown_map.pop(pid, None)
            self.logger.debug(f"冷却时间已重置 PID={pid}")

    def clear_expired_cooldowns(self) -> int:
        """
        清理过期的冷却记录（防止内存泄漏）。

        Returns:
            清理的条目数。

        类似 JS 的:  map.forEach((time, pid) => { if (expired) map.delete(pid); })
        """
        with self._cooldown_lock:
            expired_pids = [
                pid for pid, t in self._cooldown_map.items()
                if (datetime.now() - t).total_seconds() >= self.cooldown_seconds
            ]
            for pid in expired_pids:
                del self._cooldown_map[pid]
            if expired_pids:
                self.logger.debug(f"清理 {len(expired_pids)} 条过期冷却记录")
            return len(expired_pids)

    # ----------------------------------------------------------
    # 响应动作处理器注册
    # ----------------------------------------------------------

    def _register_default_handlers(self) -> None:
        """注册默认的五个响应动作处理器。"""
        self._action_handlers[ResponseAction.LOG] = self._handle_log
        self._action_handlers[ResponseAction.SIGSTOP] = self._handle_sigstop
        self._action_handlers[ResponseAction.SIGKILL] = self._handle_sigkill
        self._action_handlers[ResponseAction.BLOCK_IP] = self._handle_block_ip
        self._action_handlers[ResponseAction.QUARANTINE] = self._handle_quarantine

    def register_handler(self, action: ResponseAction, handler: ResponseCallback) -> None:
        """
        注册自定义响应处理器（扩展点）。

        类似 JS 的策略模式注入:
          responder.register_handler(CUSTOM_ACTION, (pid, reason, meta) => { ... })
        """
        self._action_handlers[action] = handler
        self.logger.info(f"注册自定义处理器: {action.name}")

    # ----------------------------------------------------------
    # 核心入口：处理告警
    # ----------------------------------------------------------

    def handle_alert(self, alert: Alert) -> List[ResponseResult]:
        """
        处理一条告警 —— 这是响应模块的主入口。

        流程（类比 HTTP 请求的中间件链）：
          1. 统计计数
          2. 检查白名单 → 命中则跳过
          3. 检查冷却时间 → 命中则跳过
          4. 执行响应动作 → 返回结果列表

        Args:
            alert: 告警对象

        Returns:
            响应结果列表（可能为空，表示被白名单/冷却过滤）

        Example:
            alert = Alert(pid=5678, action=ResponseAction.SIGKILL,
                          reason="ransomware_score_0.98")
            results = responder.handle_alert(alert)
            for r in results:
                print(f"{r.action.name}: {'OK' if r.success else 'FAIL'}")
        """
        self.stats["total_alerts"] += 1

        # ---- 解析进程元信息 ----
        process_name = alert.metadata.get("process_name", "")
        uid = alert.metadata.get("uid", -1)

        # ---- 步骤 1: 白名单检查 ----
        if self.is_whitelisted(alert.pid, process_name, uid):
            self.stats["whitelist_hits"] += 1
            self.logger.info(
                f"⏭️  白名单命中，跳过响应: PID={alert.pid} "
                f"name={process_name} uid={uid} reason={alert.reason}",
            )
            return []  # 空列表表示未执行任何动作

        # ---- 步骤 2: 冷却时间检查 ----
        if self.is_in_cooldown(alert.pid):
            self.stats["cooldown_hits"] += 1
            self.logger.info(
                f"⏳ 冷却期内，跳过响应: PID={alert.pid} "
                f"reason={alert.reason} cooldown={self.cooldown_seconds}s",
            )
            return []

        # ---- 步骤 3: 权限预检 ----
        if alert.action.requires_root and not self.dry_run:
            if os.geteuid() != 0:
                msg = (
                    f"动作 {alert.action.name} 需要 root 权限，但当前为 "
                    f"UID={os.geteuid()}。请使用 sudo 运行。"
                )
                self.logger.error(msg)
                return [ResponseResult(
                    action=alert.action,
                    success=False,
                    message=msg,
                )]

        # ---- 步骤 4: 执行响应动作 ----
        results = self._execute_action(alert)

        # ---- 步骤 5: 更新冷却时间 ----
        self._set_cooldown(alert.pid)

        # ---- 步骤 6: 更新统计 ----
        for r in results:
            if r.success:
                self.stats["actions_executed"] += 1
            else:
                self.stats["actions_failed"] += 1

        return results

    def handle_alert_chain(
        self,
        pid: int,
        actions: List[ResponseAction],
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[ResponseResult]:
        """
        执行响应策略链：一个告警触发多个响应动作。

        动作按 severity 排序后顺序执行（温和→严厉）。
        如果某个动作失败，默认继续执行后续动作（fail-open 策略）。

        类比：
          - JS:   await Promise.all(actions.map(a => execute(a)))
          - 但这里是有序执行（因为动作有副作用和依赖关系）

        Args:
            pid: 目标 PID
            actions: 响应动作列表（如 [LOG, SIGSTOP, SIGKILL]）
            reason: 告警原因
            metadata: 可选元数据

        Returns:
            所有动作的结果列表
        """
        # 按严重程度排序（温和的动作先执行）
        sorted_actions = sorted(actions, key=lambda a: a.severity)

        all_results: List[ResponseResult] = []
        for action in sorted_actions:
            alert = Alert(
                pid=pid,
                action=action,
                reason=reason,
                metadata=metadata or {},
            )
            results = self.handle_alert(alert)
            all_results.extend(results)

        return all_results

    def _execute_action(self, alert: Alert) -> List[ResponseResult]:
        """
        执行单个响应动作。

        返回列表是为了扩展性（未来一个 Alert 可能映射到多个子动作）。
        """
        handler = self._action_handlers.get(alert.action)
        if handler is None:
            msg = f"未找到动作处理器: {alert.action}"
            self.logger.error(msg)
            return [ResponseResult(action=alert.action, success=False, message=msg)]

        # dry_run 模式：模拟执行
        if self.dry_run and alert.action not in self._HARMLESS_ACTIONS:
            self.stats["actions_skipped_dry_run"] += 1
            msg = (
                f"[DRY-RUN] 将执行 {alert.action.name} 于 PID={alert.pid}，"
                f"原因: {alert.reason}"
            )
            self.logger.warning(msg)
            return [ResponseResult(
                action=alert.action,
                success=True,  # dry-run 视为"成功跳过"
                message=f"(dry-run) {msg}",
            )]

        # 实际执行
        t_start = time.perf_counter()
        try:
            success = handler(alert.pid, alert.reason, alert.metadata)
            message = f"{alert.action.name} PID={alert.pid} reason={alert.reason}"
            if not success:
                message = f"FAILED: {message}"
        except Exception as exc:
            success = False
            message = f"异常: {type(exc).__name__}: {exc}"
            self.logger.exception(f"执行 {alert.action.name} 时发生异常")

        duration_ms = (time.perf_counter() - t_start) * 1000.0

        result = ResponseResult(
            action=alert.action,
            success=success,
            message=message,
            duration_ms=duration_ms,
        )

        # 记录 JSON 日志
        log_extra = {
            "extra_fields": {
                "pid": alert.pid,
                "action": alert.action.name,
                "reason": alert.reason,
                "success": success,
                "duration_ms": round(duration_ms, 2),
                "dry_run": self.dry_run,
            },
        }
        if success:
            self.logger.info(f"✅ {alert.action.name} 成功: PID={alert.pid}", extra=log_extra)
        else:
            self.logger.error(f"❌ {alert.action.name} 失败: PID={alert.pid}", extra=log_extra)

        return [result]

    # ----------------------------------------------------------
    # 响应动作实现（5 个策略函数）
    # ----------------------------------------------------------

    def _handle_log(self, pid: int, reason: str, metadata: dict) -> bool:
        """
        策略 0: LOG — 仅记录日志。

        这是最温和的响应，零侵入。告警已在上层 _execute_action 中记录，
        此处仅作显式占位。总是返回 True。

        类比：
          - JS:  console.warn({ pid, reason, ... })
          - Python: logger.warning(...)
        """
        # 记录详细的结构化日志
        self.logger.info(
            f"[LOG] PID={pid} reason={reason} metadata={json.dumps(metadata, default=str)}",
            extra={"extra_fields": {
                "pid": pid,
                "action": "LOG",
                "reason": reason,
                "metadata": metadata,
            }},
        )
        return True

    def _handle_sigstop(self, pid: int, reason: str, metadata: dict) -> bool:
        """
        策略 1: SIGSTOP — 发送 SIGSTOP 暂停进程。

        SIGSTOP 与 SIGKILL 的区别（类比 JS）：
          - SIGSTOP ≈ debugger 的断点暂停（可恢复，进程状态完整保留）
          - SIGKILL ≈ process.exit() 强制杀进程（不可恢复）
          - SIGTERM ≈ 给进程发信号让它自己善后（可捕获，类似 beforeunload 事件）

        进程被 SIGSTOP 后：
          - 状态变为 T (stopped)
          - 所有线程暂停
          - 内存/文件描述符保持不变
          - 可用 SIGCONT 恢复（类似继续执行断点）

        Returns:
            True 如果成功发送信号
        """
        try:
            os.kill(pid, _signal.SIGSTOP)
            self.logger.info(
                f"已发送 SIGSTOP → PID={pid}",
                extra={"extra_fields": {"pid": pid, "signal": "SIGSTOP"}},
            )
            return True
        except ProcessLookupError:
            self.logger.warning(f"PID={pid} 不存在（可能已退出）")
            return False
        except PermissionError:
            self.logger.error(f"无权限向 PID={pid} 发送 SIGSTOP（需要 root 或同 UID）")
            return False

    def _handle_sigkill(self, pid: int, reason: str, metadata: dict) -> bool:
        """
        策略 2: SIGKILL — 发送 SIGKILL 终止进程。

        SIGKILL (signal 9) 是 Linux 内核强制终止信号：
          - 进程无法捕获、忽略或处理
          - 内核立即回收资源
          - 不会给进程任何清理机会（不同于 SIGTERM）

        类比：
          - JS:  process.kill(pid, 'SIGKILL')  ← Node.js 也直接调用这个
          - 任务管理器: "结束任务" ≈ SIGTERM，"结束进程树" ≈ SIGKILL

        Returns:
            True 如果成功发送信号
        """
        try:
            os.kill(pid, _signal.SIGKILL)
            self.logger.warning(
                f"已发送 SIGKILL → PID={pid} reason={reason}",
                extra={"extra_fields": {"pid": pid, "signal": "SIGKILL", "reason": reason}},
            )
            return True
        except ProcessLookupError:
            self.logger.warning(f"PID={pid} 不存在（可能已退出）")
            return False
        except PermissionError:
            self.logger.error(f"无权限向 PID={pid} 发送 SIGKILL（需要 root 或同 UID）")
            return False

    def _handle_block_ip(self, pid: int, reason: str, metadata: dict) -> bool:
        """
        策略 3: BLOCK_IP — 使用 iptables 阻断外连 IP。

        原理（类比防火墙）：
          - 类似 Windows 防火墙的出站规则
          - 类似 AWS Security Group 的 deny 规则
          - iptables 是 Linux 内核 netfilter 的用户态工具

        操作：
          1. 确保自定义链 RUNTIME-GUARDIAN 存在
          2. 将目标 IP 加入 DROP 规则
          3. 确保 OUTPUT 链跳转到自定义链

        iptables 规则结构：
          OUTPUT → RUNTIME-GUARDIAN → DROP (target_ip)

        类比解释：
          - JS 中间件:  app.use((req, res, next) => { if (blocked) res.status(403) })
          - iptables:   每个出站包依次经过规则链，匹配则 DROP

        Args:
            pid: 触发告警的进程 PID（用于日志）
            reason: 阻断原因
            metadata: 必须包含 "target_ip" 字段

        Returns:
            True 如果 iptables 规则添加成功
        """
        target_ip = metadata.get("target_ip", "")
        if not target_ip:
            self.logger.error("BLOCK_IP 需要 metadata.target_ip")
            return False

        # 简单的 IP 地址格式校验（不用正则，避免引入 re 仅为此一处）
        # 类似 JS 的:  net.isIP(target_ip) 检查
        if not self._is_valid_ip(target_ip):
            self.logger.error(f"无效 IP 地址: {target_ip}")
            return False

        try:
            # 步骤 1: 确保自定义链存在（幂等操作）
            # iptables -N CHAIN 在链已存在时会报错，所以先检查
            # 类似 JS 的:  if (!chainExists) { createChain(); }
            result = subprocess.run(
                ["iptables", "-L", IPTABLES_CHAIN, "-n"],
                capture_output=True, text=True,
                timeout=5,
            )
            if result.returncode != 0:
                # 链不存在，创建它
                subprocess.run(
                    ["iptables", "-N", IPTABLES_CHAIN],
                    capture_output=True, text=True,
                    timeout=5, check=True,
                )
                self.logger.info(f"创建 iptables 链: {IPTABLES_CHAIN}")

            # 步骤 2: 确保 OUTPUT 链有跳转规则
            # 避免重复添加：检查是否已存在跳转
            jump_exists = False
            output_result = subprocess.run(
                ["iptables", "-L", "OUTPUT", "-n"],
                capture_output=True, text=True,
                timeout=5,
            )
            if IPTABLES_CHAIN in output_result.stdout:
                jump_exists = True

            if not jump_exists:
                subprocess.run(
                    ["iptables", "-I", "OUTPUT", "1", "-j", IPTABLES_CHAIN],
                    capture_output=True, text=True,
                    timeout=5, check=True,
                )
                self.logger.info(f"OUTPUT 链已跳转到 {IPTABLES_CHAIN}")

            # 步骤 3: 检查是否已有相同规则（避免重复）
            existing = subprocess.run(
                ["iptables", "-L", IPTABLES_CHAIN, "-n"],
                capture_output=True, text=True,
                timeout=5,
            )
            if target_ip in existing.stdout:
                self.logger.info(f"IP {target_ip} 已被阻断，跳过重复规则")
                return True

            # 步骤 4: 添加 DROP 规则
            subprocess.run(
                [
                    "iptables", "-A", IPTABLES_CHAIN,
                    "-d", target_ip,
                    "-j", "DROP",
                    "-m", "comment",
                    "--comment", f"RG:{reason}:PID={pid}",
                ],
                capture_output=True, text=True,
                timeout=5, check=True,
            )

            self.logger.warning(
                f"已阻断 IP: {target_ip} (原因: {reason}, PID={pid})",
                extra={"extra_fields": {
                    "pid": pid, "action": "BLOCK_IP",
                    "target_ip": target_ip, "reason": reason,
                }},
            )
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"iptables 命令失败: {e.stderr.strip() if e.stderr else str(e)}")
            return False
        except FileNotFoundError:
            self.logger.error("未找到 iptables 命令（请确认已安装 iptables）")
            return False
        except Exception as e:
            self.logger.error(f"BLOCK_IP 异常: {type(e).__name__}: {e}")
            return False

    def _handle_quarantine(self, pid: int, reason: str, metadata: dict) -> bool:
        """
        策略 4: QUARANTINE — 隔离进程（cgroup freezer + 限制 CPU/内存）。

        这是最严厉的本地响应措施（仅次于物理断网）。

        实现原理：
          1. 创建 cgroup freezer 子组，将进程移入并冻结
             - 类似 Docker 的 docker pause（本质也是 cgroup freezer）
          2. 限制 CPU 配额（cpu.max / cpu.cfs_quota_us）
             - 类似 Docker 的 --cpus=0.5
          3. 限制内存上限（memory.max / memory.limit_in_bytes）
             - 类似 Docker 的 --memory=128m

        cgroup v1 vs v2:
          - v1 (legacy): /sys/fs/cgroup/freezer/ + /sys/fs/cgroup/cpu/ + /sys/fs/cgroup/memory/
          - v2 (unified): /sys/fs/cgroup/ 单一层次
          - 本实现优先尝试 v2，回退到 v1

        类比解释（给 JS 开发者）：
          - cgroup freezer ≈ 浏览器 tab suspender 扩展（冻结不活跃标签页）
          - CPU 限制 ≈ Chrome 的 "节流后台标签页 JS 定时器"
          - 内存限制 ≈ Node.js 的 --max-old-space-size=512

        Args:
            pid: 目标进程 PID
            reason: 隔离原因
            metadata: 可选，支持 "cpu_percent" (默认 10), "memory_mb" (默认 128)

        Returns:
            True 如果隔离成功
        """
        cpu_percent = metadata.get("cpu_percent", 10)     # 默认限制到 10%
        memory_mb = metadata.get("memory_mb", 128)        # 默认限制 128MB
        cgroup_name = f"{DEFAULT_CGROUP_NAME}-{pid}"

        try:
            # ---- 检测 cgroup 版本 ----
            cgroup_version = self._detect_cgroup_version()
            self.logger.info(f"检测到 cgroup v{cgroup_version}")

            if cgroup_version == 2:
                success = self._quarantine_v2(pid, cgroup_name, cpu_percent, memory_mb)
            else:
                success = self._quarantine_v1(pid, cgroup_name, cpu_percent, memory_mb)

            if success:
                self.logger.warning(
                    f"🔒 已隔离 PID={pid}: cpu≤{cpu_percent}% mem≤{memory_mb}MB "
                    f"cgroup={cgroup_name} reason={reason}",
                    extra={"extra_fields": {
                        "pid": pid, "action": "QUARANTINE",
                        "cgroup": cgroup_name, "reason": reason,
                    }},
                )
            return success

        except PermissionError:
            self.logger.error("需要 root 权限操作 cgroup（请使用 sudo 运行）")
            return False
        except Exception as e:
            self.logger.error(f"QUARANTINE 异常: {type(e).__name__}: {e}")
            return False

    def _detect_cgroup_version(self) -> int:
        """
        检测 cgroup 版本。

        cgroup v2 (unified): /sys/fs/cgroup/cgroup.controllers 存在
        cgroup v1 (legacy):  /sys/fs/cgroup/freezer/ 存在
        """
        # v2 特征文件
        if (CGROUP_ROOT / "cgroup.controllers").exists():
            return 2
        # v1 特征目录
        if (CGROUP_ROOT / "freezer").exists():
            return 1
        # 默认假设 v1
        return 1

    def _quarantine_v2(
        self, pid: int, cgroup_name: str,
        cpu_percent: int, memory_mb: int,
    ) -> bool:
        """
        cgroup v2 (unified hierarchy) 隔离实现。

        cgroup v2 结构（类似文件系统树）：
          /sys/fs/cgroup/
          ├── cgroup.controllers      ← 可用控制器列表
          ├── cgroup.subtree_control  ← 子组可用的控制器
          └── runtime-guardian-1234/  ← 为 PID 1234 创建的子组
              ├── cgroup.freeze       ← 写入 1 冻结，0 解冻
              ├── cpu.max             ← "$MAX $PERIOD" 格式
              ├── memory.max          ← 内存上限（字节）
              └── cgroup.procs        ← 进程 PID 列表
        """
        child_path = CGROUP_ROOT / cgroup_name

        # 步骤 1: 确保父组的 subtree_control 启用了所需控制器
        # 类比：你需要先在目录上 chmod +w 才能在里面创建文件
        controllers = ["+cpu", "+memory"]
        for ctrl in controllers:
            ctrl_file = CGROUP_ROOT / "cgroup.subtree_control"
            if ctrl_file.exists():
                current = ctrl_file.read_text().strip()
                ctrl_name = ctrl.lstrip("+")
                if ctrl_name not in current:
                    try:
                        ctrl_file.write_text(ctrl)
                    except PermissionError:
                        # 某些系统不允许修改根组，尝试继续
                        self.logger.debug(f"无法修改 subtree_control: {ctrl}")

        # 步骤 2: 创建子 cgroup
        child_path.mkdir(parents=True, exist_ok=True)

        # 步骤 3: 配置 CPU 限制
        # cpu.max 格式: "$MAX $PERIOD"（微秒）
        # 例如 "20000 100000" 表示每 100ms 周期内最多用 20ms（即 20%）
        if cpu_percent > 0 and cpu_percent < 100:
            period_us = 100_000  # 默认周期 100ms
            quota_us = int(period_us * cpu_percent / 100)
            cpu_max_file = child_path / "cpu.max"
            cpu_max_file.write_text(f"{quota_us} {period_us}")
            self.logger.debug(f"CPU 限制: {cpu_percent}% ({quota_us}us / {period_us}us)")

        # 步骤 4: 配置内存限制
        if memory_mb > 0:
            memory_max_file = child_path / "memory.max"
            memory_max_file.write_text(str(memory_mb * 1024 * 1024))  # MB → bytes
            self.logger.debug(f"内存限制: {memory_mb}MB")

        # 步骤 5: 将进程移入 cgroup
        procs_file = child_path / "cgroup.procs"
        procs_file.write_text(str(pid))
        self.logger.debug(f"PID={pid} 已移入 cgroup {cgroup_name}")

        # 步骤 6: 冻结 cgroup
        # cgroup.freeze: 写 1 冻结，写 0 解冻
        freeze_file = child_path / "cgroup.freeze"
        if freeze_file.exists():
            freeze_file.write_text("1")
            self.logger.debug(f"cgroup {cgroup_name} 已冻结")

        return True

    def _quarantine_v1(
        self, pid: int, cgroup_name: str,
        cpu_percent: int, memory_mb: int,
    ) -> bool:
        """
        cgroup v1 (legacy) 隔离实现。

        cgroup v1 结构（每个控制器独立目录）：
          /sys/fs/cgroup/freezer/runtime-guardian-1234/
          /sys/fs/cgroup/cpu/runtime-guardian-1234/
          /sys/fs/cgroup/memory/runtime-guardian-1234/
        """
        success = True

        # ---- Freezer ----
        freezer_path = CGROUP_ROOT / "freezer" / cgroup_name
        freezer_path.mkdir(parents=True, exist_ok=True)
        (freezer_path / "tasks").write_text(str(pid))

        # 冻结: 写入 FROZEN 到 freezer.state
        freeze_state_file = freezer_path / "freezer.state"
        if freeze_state_file.exists():
            freeze_state_file.write_text("FROZEN")
            self.logger.debug(f"cgroup v1 freezer: {cgroup_name} FROZEN")

        # ---- CPU ----
        cpu_path = CGROUP_ROOT / "cpu" / cgroup_name
        if cpu_path.exists() or cpu_path.parent.exists():
            cpu_path.mkdir(parents=True, exist_ok=True)
            (cpu_path / "tasks").write_text(str(pid))

            # cpu.cfs_period_us: 默认 100000 (100ms)
            # cpu.cfs_quota_us:  每周期可用的 CPU 微秒数
            if cpu_percent > 0 and cpu_percent < 100:
                period_us = 100_000
                quota_us = int(period_us * cpu_percent / 100)
                (cpu_path / "cpu.cfs_period_us").write_text(str(period_us))
                (cpu_path / "cpu.cfs_quota_us").write_text(str(quota_us))
                self.logger.debug(f"cgroup v1 CPU 限制: {cpu_percent}%")

        # ---- Memory ----
        memory_path = CGROUP_ROOT / "memory" / cgroup_name
        if memory_path.exists() or memory_path.parent.exists():
            memory_path.mkdir(parents=True, exist_ok=True)
            (memory_path / "tasks").write_text(str(pid))

            if memory_mb > 0:
                (memory_path / "memory.limit_in_bytes").write_text(
                    str(memory_mb * 1024 * 1024)
                )
                self.logger.debug(f"cgroup v1 Memory 限制: {memory_mb}MB")

        return success

    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        """
        简单的 IPv4 地址校验。

        不引入 re 模块，纯字符串处理（保持依赖精简）。
        类似 JS 的 net.isIP(ip) 但仅支持 IPv4。

        Args:
            ip: IP 地址字符串

        Returns:
            True 如果是合法 IPv4 地址
        """
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        for part in parts:
            if not part.isdigit():
                return False
            num = int(part)
            if num < 0 or num > 255:
                return False
            # 拒绝前导零（如 "01"），但 "0" 本身合法
            if len(part) > 1 and part[0] == "0":
                return False
        return True

    # ----------------------------------------------------------
    # 统计与状态导出
    # ----------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """
        导出统计信息和当前状态。

        Returns:
            包含统计、白名单大小、冷却条目数等的字典。
        """
        with self._cooldown_lock:
            cooldown_count = len(self._cooldown_map)
        return {
            "stats": dict(self.stats),
            "whitelist": {
                "pid_count": len(self._whitelist_pids),
                "name_count": len(self._whitelist_names),
                "uid_count": len(self._whitelist_uids),
            },
            "cooldown_entries": cooldown_count,
            "cooldown_seconds": self.cooldown_seconds,
            "dry_run": self.dry_run,
        }

    def print_stats(self) -> None:
        """以人类可读格式打印统计信息。"""
        s = self.get_stats()
        print("\n" + "=" * 55)
        print("  Runtime Guardian — Responder 统计")
        print("=" * 55)
        print(f"  模式:         {'演习 (DRY-RUN)' if s['dry_run'] else '生产 (LIVE)'}")
        print(f"  冷却时间:     {s['cooldown_seconds']} 秒")
        print(f"  总告警数:     {s['stats']['total_alerts']}")
        print(f"  白名单命中:   {s['stats']['whitelist_hits']}")
        print(f"  冷却命中:     {s['stats']['cooldown_hits']}")
        print(f"  已执行动作:   {s['stats']['actions_executed']}")
        print(f"  失败动作:     {s['stats']['actions_failed']}")
        print(f"  dry-run 跳过: {s['stats']['actions_skipped_dry_run']}")
        print(f"  白名单 PID:   {s['whitelist']['pid_count']}")
        print(f"  白名单 Name:  {s['whitelist']['name_count']}")
        print(f"  白名单 UID:   {s['whitelist']['uid_count']}")
        print(f"  冷却条目数:   {s['cooldown_entries']}")
        print("=" * 55 + "\n")

    def shutdown(self) -> None:
        """
        优雅关闭响应器。

        清理过期冷却记录，打印最终统计。
        类似 JS 的 process.on('exit', cleanup)。
        """
        self.logger.info("Responder 正在关闭...")
        self.clear_expired_cooldowns()
        self.print_stats()
        self.logger.info("Responder 已关闭")


# ================================================================
# 命令行自测入口
# ================================================================

def build_argument_parser() -> argparse.ArgumentParser:
    """
    构建命令行参数解析器。

    支持独立自测和集成调用两种使用方式。
    类似 Python 的 click 或 JS 的 yargs/commander。
    """
    parser = argparse.ArgumentParser(
        description="Runtime Guardian — 自动响应模块 (Responder) 命令行自测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 仅记录日志
  python3 responder.py --pid 1234 --action LOG --reason "test_alert"

  # 终止进程
  sudo python3 responder.py --pid 1234 --action SIGKILL --reason "ransomware"

  # 阻断外连 IP（需要 root）
  sudo python3 responder.py --pid 1234 --action BLOCK_IP \\
      --reason "c2_communication" --target-ip 10.0.0.99

  # 隔离进程（cgroup freezer）
  sudo python3 responder.py --pid 1234 --action QUARANTINE \\
      --reason "suspicious_behavior" --cpu-percent 10 --memory-mb 128

  # 多动作策略链（逗号分隔）
  sudo python3 responder.py --pid 1234 --action LOG,SIGSTOP,SIGKILL \\
      --reason "critical_threat"

  # 演习模式（仅打印不执行）
  python3 responder.py --pid 1234 --action SIGKILL --dry-run

  # 自定义冷却时间
  python3 responder.py --pid 1234 --action LOG --reason "test" --cooldown 120

  # 添加白名单
  python3 responder.py --pid 1234 --action LOG --reason "test" \\
      --whitelist-pid 1 --whitelist-name sshd --whitelist-uid 0
        """,
    )

    # ---- 必需参数 ----
    parser.add_argument(
        "--pid", type=int, required=True,
        help="目标进程 PID",
    )
    parser.add_argument(
        "--action", type=str, required=True,
        help=(
            "响应动作: LOG | SIGSTOP | SIGKILL | BLOCK_IP | QUARANTINE。"
            "多个动作用逗号分隔（如 LOG,SIGSTOP,SIGKILL）"
        ),
    )
    parser.add_argument(
        "--reason", type=str, required=True,
        help="告警原因（人类可读描述）",
    )

    # ---- 可选参数 ----
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="演习模式：仅打印日志，不实际执行响应动作",
    )
    parser.add_argument(
        "--cooldown", type=int, default=DEFAULT_COOLDOWN_SECONDS,
        help=f"冷却时间（秒），默认 {DEFAULT_COOLDOWN_SECONDS}",
    )
    parser.add_argument(
        "--target-ip", type=str, default="",
        help="目标 IP 地址（BLOCK_IP 动作需要）",
    )
    parser.add_argument(
        "--cpu-percent", type=int, default=10,
        help="QUARANTINE 的 CPU 限制百分比，默认 10",
    )
    parser.add_argument(
        "--memory-mb", type=int, default=128,
        help="QUARANTINE 的内存限制（MB），默认 128",
    )

    # ---- 白名单参数（用于测试白名单功能） ----
    parser.add_argument(
        "--whitelist-pid", type=int, default=None,
        help="添加白名单 PID（测试用）",
    )
    parser.add_argument(
        "--whitelist-name", type=str, default=None,
        help="添加白名单进程名（测试用）",
    )
    parser.add_argument(
        "--whitelist-uid", type=int, default=None,
        help="添加白名单 UID（测试用）",
    )

    return parser


def main() -> None:
    """
    命令行自测入口。

    这是一个独立可运行的 main 函数，用于验证 Responder 模块的
    各项功能。在集成环境中，Responder 类被直接导入使用而非通过
    命令行调用。

    流程：
      1. 解析命令行参数
      2. 初始化 Responder
      3. 配置白名单
      4. 解析响应动作（支持逗号分隔的多动作链）
      5. 执行响应
      6. 打印结果和统计
    """
    parser = build_argument_parser()
    args = parser.parse_args()

    # ---- 权限检查提示 ----
    if os.geteuid() != 0 and not args.dry_run:
        action_upper = args.action.upper()
        # 需要 root 的动作
        needs_root_actions = {"SIGSTOP", "SIGKILL", "BLOCK_IP", "QUARANTINE"}
        requested = set(a.strip() for a in action_upper.split(","))
        if requested & needs_root_actions:
            print("=" * 60)
            print("⚠️  警告：以下动作需要 root 权限，但当前非 root 用户：")
            print(f"   {requested & needs_root_actions}")
            print()
            print("   请使用 sudo 运行，或添加 --dry-run 进行演习。")
            print("=" * 60)
            print()
            # 不退出 — 让 Responder 内部处理权限错误

    # ---- 初始化 Responder ----
    responder = Responder(
        dry_run=args.dry_run,
        cooldown_seconds=args.cooldown,
    )

    # ---- 配置白名单 ----
    if args.whitelist_pid:
        responder.add_to_whitelist(pid=args.whitelist_pid)
    if args.whitelist_name:
        responder.add_to_whitelist(name=args.whitelist_name)
    if args.whitelist_uid:
        responder.add_to_whitelist(uid=args.whitelist_uid)

    # ---- 解析响应动作 ----
    action_names = [a.strip() for a in args.action.split(",")]
    actions: List[ResponseAction] = []
    for name in action_names:
        try:
            actions.append(ResponseAction.from_string(name))
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)

    print(f"\n📋 告警信息:")
    print(f"   PID:     {args.pid}")
    print(f"   动作:    {[a.name for a in actions]}")
    print(f"   原因:    {args.reason}")
    print(f"   模式:    {'演习 (DRY-RUN)' if args.dry_run else '生产 (LIVE)'}")
    print(f"   冷却:    {args.cooldown} 秒")
    if args.target_ip:
        print(f"   目标IP:  {args.target_ip}")
    print()

    # ---- 构建元数据 ----
    metadata: Dict[str, Any] = {}
    if args.target_ip:
        metadata["target_ip"] = args.target_ip
    if args.cpu_percent:
        metadata["cpu_percent"] = args.cpu_percent
    if args.memory_mb:
        metadata["memory_mb"] = args.memory_mb

    # ---- 尝试获取进程名和 UID（用于白名单匹配） ----
    try:
        import psutil
        try:
            proc = psutil.Process(args.pid)
            metadata["process_name"] = proc.name()
            metadata["uid"] = proc.uids().real if hasattr(proc, 'uids') else -1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except ImportError:
        # psutil 不是硬依赖（只是增强白名单匹配）
        pass

    # ---- 执行响应 ----
    results: List[ResponseResult] = []
    if len(actions) == 1:
        alert = Alert(
            pid=args.pid,
            action=actions[0],
            reason=args.reason,
            metadata=metadata,
        )
        results = responder.handle_alert(alert)
    else:
        results = responder.handle_alert_chain(
            pid=args.pid,
            actions=actions,
            reason=args.reason,
            metadata=metadata,
        )

    # ---- 打印结果 ----
    print("📊 执行结果:")
    print("-" * 50)
    for r in results:
        status_icon = "✅" if r.success else "❌"
        print(f"  {status_icon} {r.action.name:<12} {r.message}")
        print(f"     耗时: {r.duration_ms:.2f}ms  时间: {r.timestamp.isoformat()}")
    print("-" * 50)

    if not results:
        print("  ⏭️  未执行任何动作（可能命中白名单或冷却期）")

    # ---- 打印统计 ----
    responder.print_stats()

    # ---- 关闭 ----
    responder.shutdown()

    # 返回适当的退出码
    all_success = all(r.success for r in results) if results else True
    sys.exit(0 if all_success else 1)


# ================================================================
# 模块入口
# ================================================================

if __name__ == "__main__":
    """
    直接运行时执行命令行自测。

    类似 Python 的:
      if __name__ == "__main__":
          main()

    类似 Node.js 的:
      if (require.main === module) { main(); }

    在集成环境中，其他模块通过以下方式导入使用：
      from responder import Responder, Alert, ResponseAction
    """
    main()
