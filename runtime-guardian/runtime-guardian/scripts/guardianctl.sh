#!/usr/bin/env bash
# ================================================================
# guardianctl — Runtime Guardian CLI 管理工具
# ================================================================
# 用法:
#   guardianctl status     — 查看运行状态
#   guardianctl start      — 启动监控
#   guardianctl stop       — 停止监控
#   guardianctl reload     — 重载配置
#   guardianctl health     — 健康检查
#   guardianctl stats      — 统计信息
#   guardianctl dump       — 导出基线模型
#   guardianctl logs       — 查看日志
#   guardianctl test       — 运行自检
#   guardianctl version    — 版本信息
# ================================================================

set -euo pipefail

# ---- 配置 ----
VERSION="2.0.0"
LIBDIR="${RG_LIBDIR:-/usr/local/lib/runtime-guardian}"
CONFDIR="${RG_CONFDIR:-/etc/runtime-guardian}"
DATADIR="${RG_DATADIR:-/var/lib/runtime-guardian}"
LOGDIR="${RG_LOGDIR:-/var/log/runtime-guardian}"
RUNDIR="${RG_RUNDIR:-/var/run/runtime-guardian}"
PIDFILE="$RUNDIR/guardian.pid"
SOCKFILE="$RUNDIR/guardian.sock"

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ================================================================
# 帮助
# ================================================================
show_help() {
    cat << EOF
${BOLD}guardianctl v${VERSION}${NC} — Runtime Guardian 管理工具

${BOLD}命令:${NC}
  status     查看守护进程运行状态
  start      启动监控守护进程
  stop       停止监控守护进程
  restart    重启监控守护进程
  reload     重载规则配置（不中断监控）
  health     运行健康检查
  stats      显示检测统计
  dump       导出当前基线模型
  logs [-f]  查看监控日志
  test       运行组件自检
  version    版本信息

${BOLD}示例:${NC}
  guardianctl status
  guardianctl logs -f
  guardianctl dump --output /tmp/baseline.json
EOF
    exit 0
}

# ================================================================
# 状态
# ================================================================
cmd_status() {
    echo -e "${BOLD}Runtime Guardian — 运行状态${NC}"
    echo "═══════════════════════════════════════"
    echo ""
    
    # 服务状态
    if systemctl is-active runtime-guardian &>/dev/null; then
        echo -e "  服务:    ${GREEN}● 运行中${NC}"
        local since
        since=$(systemctl show runtime-guardian -p ActiveEnterTimestamp --value 2>/dev/null | cut -d= -f2)
        echo -e "  启动于:  $since"
    else
        echo -e "  服务:    ${RED}○ 已停止${NC}"
    fi
    
    # PID 文件
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "  进程:    ${GREEN}$pid (运行中)${NC}"
            # CPU/内存
            if command -v ps &>/dev/null; then
                local ps_info
                ps_info=$(ps -p "$pid" -o %cpu,%mem,rss --no-headers 2>/dev/null || echo "- - -")
                echo -e "  资源:    CPU=$(echo "$ps_info" | awk '{print $1}')%  MEM=$(echo "$ps_info" | awk '{print $3/1024 "MB"}')"
            fi
        else
            echo -e "  进程:    ${RED}PID $pid 已失效${NC}"
        fi
    else
        echo -e "  进程:    无 PID 文件"
    fi
    
    # 模块
    echo ""
    echo -e "${BOLD}已安装模块:${NC}"
    for mod in core protector rules docs; do
        case "$mod" in
            core)
                [[ -f "$LIBDIR/ebpf_monitor.py" ]] && echo -e "  ${GREEN}✅${NC} core (ebpf_monitor/baseline_detector/responder)" || echo -e "  ${RED}❌${NC} core"
                ;;
            protector)
                [[ -f "$LIBDIR/guardian_protector.py" ]] && echo -e "  ${GREEN}✅${NC} protector (guardian_protector)" || echo -e "  ${RED}❌${NC} protector"
                ;;
            rules)
                [[ -f "$CONFDIR/rules.yaml" ]] && echo -e "  ${GREEN}✅${NC} rules" || echo -e "  ${RED}❌${NC} rules"
                ;;
            docs)
                [[ -d "$LIBDIR/docs" ]] && echo -e "  ${GREEN}✅${NC} docs" || echo -e "  ${RED}❌${NC} docs"
                ;;
        esac
    done
}

# ================================================================
# 启动/停止/重启
# ================================================================
cmd_start() {
    if systemctl is-active runtime-guardian &>/dev/null; then
        echo "已在运行中"
        return
    fi
    echo "正在启动..."
    systemctl start runtime-guardian
    sleep 1
    cmd_status
}

cmd_stop() {
    if ! systemctl is-active runtime-guardian &>/dev/null; then
        echo "未在运行"
        return
    fi
    echo "正在停止..."
    systemctl stop runtime-guardian
    echo -e "${GREEN}✅ 已停止${NC}"
}

cmd_restart() {
    echo "正在重启..."
    systemctl restart runtime-guardian
    sleep 1
    cmd_status
}

cmd_reload() {
    echo "正在重载配置..."
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE")
        kill -HUP "$pid" 2>/dev/null && echo -e "${GREEN}✅ 配置已重载${NC}" || echo -e "${RED}❌ 重载失败${NC}"
    else
        echo -e "${RED}❌ 未找到运行中的进程${NC}"
    fi
}

# ================================================================
# 健康检查
# ================================================================
cmd_health() {
    echo -e "${BOLD}Runtime Guardian — 健康检查${NC}"
    echo "═══════════════════════════════════════"
    echo ""
    
    local ok=0
    local fail=0
    
    # 1. Python 组件
    echo -n "  Python 组件... "
    if python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from baseline_detector import BaselineDetector
print('OK')
" 2>/dev/null; then
        echo -e "${GREEN}✅${NC}"
        ok=$((ok + 1))
    else
        echo -e "${RED}❌${NC}"
        fail=$((fail + 1))
    fi
    
    # 2. 保护模块
    echo -n "  保护模块... "
    if python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from guardian_protector import GuardianProtector
print('OK')
" 2>/dev/null; then
        echo -e "${GREEN}✅${NC}"
        ok=$((ok + 1))
    else
        echo -e "${RED}❌${NC}"
        fail=$((fail + 1))
    fi
    
    # 3. eBPF 支持
    echo -n "  eBPF 内核支持... "
    if python3 -c "from bcc import BPF; BPF(text='int kprobe__sys_sync(void *c) { return 0; }')" 2>/dev/null; then
        echo -e "${GREEN}✅${NC}"
        ok=$((ok + 1))
    else
        echo -e "${YELLOW}⚠️${NC} (非致命)"
    fi
    
    # 4. 配置文件
    echo -n "  规则配置... "
    if [[ -f "$CONFDIR/rules.yaml" ]]; then
        echo -e "${GREEN}✅${NC} ($(wc -l < "$CONFDIR/rules.yaml") 行)"
        ok=$((ok + 1))
    else
        echo -e "${YELLOW}⚠️${NC} 使用内置默认规则"
    fi
    
    # 5. 磁盘空间
    echo -n "  磁盘空间... "
    local avail
    avail=$(df -h "$DATADIR" 2>/dev/null | tail -1 | awk '{print $4}')
    echo -e "${GREEN}${avail:-未知}${NC}"
    
    echo ""
    echo -e "  结果: ${GREEN}$ok 通过${NC}, ${RED}$fail 失败${NC}"
    if [[ $fail -gt 0 ]]; then
        echo -e "  ${RED}存在 $fail 个问题需要处理${NC}"
        return 1
    fi
}

# ================================================================
# 统计
# ================================================================
cmd_stats() {
    echo -e "${BOLD}Runtime Guardian — 检测统计${NC}"
    echo "═══════════════════════════════════════"
    
    if ! systemctl is-active runtime-guardian &>/dev/null; then
        echo -e "${YELLOW}守护进程未运行，显示历史统计${NC}"
        echo ""
    fi
    
    # 从日志中提取统计
    echo "最近告警 (journalctl):"
    journalctl -u runtime-guardian --since "1 hour ago" 2>/dev/null | \
        grep -E "🔴|🟠|🟡|🔵|🟢|基线异常" | \
        tail -20 || echo "  (无 journalctl 记录)"
    
    echo ""
    echo "告警计数 (最近1小时):"
    for level in "🔴 敏感文件" "🟠 调用风暴" "🟡 行为链" "🔵 网络异常" "🟢 基线异常"; do
        local count
        count=$(journalctl -u runtime-guardian --since "1 hour ago" 2>/dev/null | grep -c "$level" || echo 0)
        echo "  $level: $count"
    done
    
    echo ""
    echo "内存使用:"
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid=$(cat "$PIDFILE")
        ps -p "$pid" -o rss,vsz --no-headers 2>/dev/null | \
            awk '{printf "  RSS: %.1f MB  VSZ: %.1f MB\n", $1/1024, $2/1024}'
    fi
}

# ================================================================
# 导出基线
# ================================================================
cmd_dump() {
    local output="${1:-/tmp/runtime_guardian_baseline_$(date +%Y%m%d_%H%M%S).json}"
    
    echo "导出基线模型..."
    python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from baseline_detector import BaselineDetector
import json

# 尝试从 DATADIR 加载最新基线
import glob
baseline_files = sorted(glob.glob('$DATADIR/baseline_*.json'))
if baseline_files:
    bd = BaselineDetector.load(baseline_files[-1])
    bd.save('$output')
    print(f'✅ 基线已导出: $output')
else:
    print('⚠️  未找到基线文件，请先运行监控器以生成基线')
" 2>/dev/null || echo -e "${RED}❌ 导出失败${NC}"
}

# ================================================================
# 日志
# ================================================================
cmd_logs() {
    local follow=""
    if [[ "${1:-}" == "-f" ]]; then
        follow="-f"
    fi
    
    if systemctl is-active runtime-guardian &>/dev/null 2>&1; then
        journalctl -u runtime-guardian $follow --no-hostname -o short-iso 2>/dev/null || \
            echo "无法访问 journalctl，请检查 /var/log/runtime-guardian/"
    else
        echo "守护进程未运行"
    fi
}

# ================================================================
# 自检
# ================================================================
cmd_test() {
    echo -e "${BOLD}Runtime Guardian — 组件自检${NC}"
    echo "═══════════════════════════════════════"
    echo ""
    
    echo "1. 基线检测器自测..."
    python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from baseline_detector import BaselineDetector, FileContextClassifier, MultiUserProfiler, TimeWindowedProfiler, LoadAwareness, AdaptiveThresholdController
# 快速功能验证
bd = BaselineDetector(window_seconds=2.0, enable_ngram=True, enable_entropy=True,
                       enable_multi_user=True, enable_file_context=True, enable_time_window=True)
# 注入少量训练数据
import time
for i in range(500):
    bd.train(257, pid=100, uid=33, timestamp=time.time() + i*0.01, filename='/var/www/index.html')
    bd.train(0, pid=100, uid=33, timestamp=time.time() + i*0.01)
score = bd.check(257, pid=200, uid=0, filename='/etc/shadow')
print(f'  基线检测器: OK (越权检测分数={score:.2f})')
# 文件上下文
cat, risk = FileContextClassifier.classify('/etc/passwd')
print(f'  文件分类器: OK ({cat}, 风险={risk})')
# 负载感知
la = LoadAwareness()
for _ in range(50): la.record(time.time())
la.update_baseline()
level, dev = la.get_load_level()
print(f'  负载感知器: OK (级别={level})')
" 2>/dev/null && echo -e "  ${GREEN}✅ 全部通过${NC}" || echo -e "  ${RED}❌ 失败${NC}"
    
    echo ""
    echo "2. 保护模块自测..."
    python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from guardian_protector import DynamicSampler, BackpressureGuard, MemoryGuard, Watchdog, GracefulDegrader, DegradationLevel
# 动态采样
ds = DynamicSampler()
ds.update(50)
assert ds.sample_rate == 0.7, '采样率错误'
# 内存保护
mg = MemoryGuard(warn_mb=200, max_mb=500)
r = mg.check(250, 'rising')
assert r['action'] == 'lru_evict', 'LRU淘汰未触发'
# 降级
gd = GracefulDegrader()
assert gd.level == DegradationLevel.FULL
print(f'  动态采样器: OK (CPU50%→采样{ds.sample_rate:.0%})')
print(f'  内存保护器: OK (250M→{r[\"action\"]})')
print(f'  降级调度器: OK (级别={gd.level.name})')
" 2>/dev/null && echo -e "  ${GREEN}✅ 全部通过${NC}" || echo -e "  ${RED}❌ 失败${NC}"
    
    echo ""
    echo -e "${GREEN}✅ 自检完成${NC}"
}

# ================================================================
# 版本
# ================================================================
cmd_version() {
    echo -e "${BOLD}Runtime Guardian${NC}"
    echo "  版本:     v$VERSION"
    echo "  安装路径: $LIBDIR"
    echo "  配置路径: $CONFDIR"
    echo "  数据路径: $DATADIR"
    echo ""
    echo "已安装模块:"
    [[ -f "$LIBDIR/ebpf_monitor.py" ]] && echo "  ✅ core v$VERSION" || echo "  ❌ core"
    [[ -f "$LIBDIR/guardian_protector.py" ]] && echo "  ✅ protector v$VERSION" || echo "  ❌ protector"
    [[ -f "$CONFDIR/rules.yaml" ]] && echo "  ✅ rules" || echo "  ❌ rules"
}

# ================================================================
# 主入口
# ================================================================
main() {
    local cmd="${1:-status}"
    shift || true
    
    case "$cmd" in
        status)   cmd_status "$@" ;;
        start)    cmd_start "$@" ;;
        stop)     cmd_stop "$@" ;;
        restart)  cmd_restart "$@" ;;
        reload)   cmd_reload "$@" ;;
        health)   cmd_health "$@" ;;
        stats)    cmd_stats "$@" ;;
        dump)     cmd_dump "$@" ;;
        logs)     cmd_logs "$@" ;;
        test)     cmd_test "$@" ;;
        version)  cmd_version "$@" ;;
        help|-h|--help) show_help ;;
        *)
            echo -e "${RED}未知命令: $cmd${NC}"
            echo "运行 'guardianctl help' 查看可用命令"
            exit 1
            ;;
    esac
}

main "$@"
