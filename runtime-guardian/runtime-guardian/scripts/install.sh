#!/usr/bin/env bash
# ================================================================
# Runtime Guardian — 企业级安装脚本
# ================================================================
# 用法:
#   sudo bash install.sh                        # 全量安装
#   sudo bash install.sh --modules core,protector  # 模块化安装
#   sudo bash install.sh --prefix /opt/rg          # 自定义路径
#   sudo bash install.sh --uninstall               # 卸载
#   sudo bash install.sh --dry-run                 # 演习（不实际安装）
#   sudo bash install.sh --help                    # 帮助
# ================================================================

set -euo pipefail

# ---- 默认配置 ----
VERSION="${VERSION:-2.0.0}"
PREFIX="${PREFIX:-/usr/local}"
BINDIR="${BINDIR:-$PREFIX/bin}"
LIBDIR="${LIBDIR:-$PREFIX/lib/runtime-guardian}"
CONFDIR="${CONFDIR:-/etc/runtime-guardian}"
DATADIR="${DATADIR:-/var/lib/runtime-guardian}"
LOGDIR="${LOGDIR:-/var/log/runtime-guardian}"
RUNDIR="${RUNDIR:-/var/run/runtime-guardian}"
SYSTEMDDIR="${SYSTEMDDIR:-/etc/systemd/system}"
USER_GUARDIAN="${USER_GUARDIAN:-root}"
GROUP_GUARDIAN="${GROUP_GUARDIAN:-root}"

# ---- 可用模块 ----
ALL_MODULES=(core protector rules docs)
SELECTED_MODULES=()
DO_UNINSTALL=false
DRY_RUN=false

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ================================================================
# 帮助
# ================================================================
show_help() {
    cat << EOF
${BOLD}Runtime Guardian v${VERSION} — 企业级安装脚本${NC}

用法: sudo bash install.sh [选项]

${BOLD}安装选项:${NC}
  --prefix PATH       安装根目录 (默认: /usr/local)
  --modules LIST      要安装的模块，逗号分隔 (默认: 全部)
                      可用: core, protector, rules, docs
  --dry-run           演习模式，仅显示将执行的操作
  --yes               跳过所有确认提示

${BOLD}管理选项:${NC}
  --uninstall         卸载 Runtime Guardian
  --status            显示当前安装状态
  --help              显示此帮助

${BOLD}示例:${NC}
  sudo bash install.sh                              # 全量安装
  sudo bash install.sh --modules core,protector     # 仅核心+保护
  sudo bash install.sh --prefix /opt/guardian       # 自定义路径
  sudo bash install.sh --uninstall                  # 卸载
EOF
    exit 0
}

# ================================================================
# 日志函数
# ================================================================
log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${CYAN}${BOLD}▶ $*${NC}"; }

# ================================================================
# 参数解析
# ================================================================
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prefix)    PREFIX="$2"; shift 2 ;;
            --modules)   IFS=',' read -ra SELECTED_MODULES <<< "$2"; shift 2 ;;
            --dry-run)   DRY_RUN=true; shift ;;
            --yes)       YES_FLAG=true; shift ;;
            --uninstall) DO_UNINSTALL=true; shift ;;
            --status)    show_status; exit 0 ;;
            --help|-h)   show_help ;;
            *)           log_error "未知参数: $1"; show_help ;;
        esac
    done
    BINDIR="$PREFIX/bin"
    LIBDIR="$PREFIX/lib/runtime-guardian"
}

# ================================================================
# 系统检查
# ================================================================
check_root() {
    if [[ "$(id -u)" != "0" ]] && [[ "$DRY_RUN" != "true" ]]; then
        log_error "需要 root 权限运行此脚本"
        echo "  sudo bash install.sh [选项]"
        exit 1
    fi
}

check_os() {
    log_step "系统环境检查"
    
    # 内核版本
    local kernel_ver
    kernel_ver=$(uname -r)
    log_info "内核版本: $kernel_ver"
    
    # 检查是否是 Linux
    if [[ "$(uname -s)" != "Linux" ]]; then
        log_warn "非 Linux 系统，eBPF 功能将不可用"
    fi
    
    # 检查 eBPF 支持
    if [[ -f /sys/kernel/btf/vmlinux ]] 2>/dev/null; then
        log_ok "BTF 可用 — eBPF CO-RE 支持"
    else
        log_warn "BTF 不可用 — eBPF 功能可能需要额外配置"
    fi
    
    # Python
    if command -v python3 &>/dev/null; then
        local pyver
        pyver=$(python3 --version 2>&1)
        log_ok "$pyver"
    else
        log_error "Python 3 未安装"
        exit 1
    fi
    
    # bcc (可选)
    if python3 -c "from bcc import BPF" 2>/dev/null; then
        log_ok "bcc (BPF Compiler Collection) 可用"
    else
        log_warn "bcc 未安装 — eBPF 监控器不可用"
        echo "  安装: sudo apt-get install bpfcc-tools python3-bpfcc"
    fi
}

# ================================================================
# 创建目录
# ================================================================
create_directories() {
    log_step "创建目录结构"
    
    local dirs=("$BINDIR" "$LIBDIR" "$CONFDIR" "$DATADIR" "$LOGDIR" "$RUNDIR")
    
    for d in "${dirs[@]}"; do
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "[DRY-RUN] mkdir -p $d"
        else
            mkdir -p "$d"
            log_ok "$d"
        fi
    done
}

# ================================================================
# 安装各模块
# ================================================================
install_module() {
    local module=$1
    local pkg_file=""
    
    # 查找分发包（优先本地 dist/packages，其次当前目录）
    for loc in "dist/packages" "."; do
        local candidate
        candidate=$(ls "$loc"/runtime-guardian-${module}-v*.tar.gz 2>/dev/null | head -1)
        if [[ -n "$candidate" ]]; then
            pkg_file="$candidate"
            break
        fi
    done
    
    if [[ -z "$pkg_file" ]]; then
        log_warn "未找到 $module 分发包，尝试从源码安装..."
        install_module_from_source "$module"
        return
    fi
    
    log_info "安装 $module (来自: $pkg_file)"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] tar xzf $pkg_file -C /tmp && cp ... $LIBDIR/"
        return
    fi
    
    local tmpdir
    tmpdir=$(mktemp -d)
    tar xzf "$pkg_file" -C "$tmpdir"
    
    case "$module" in
        core)
            cp "$tmpdir"/runtime-guardian/src/ebpf_monitor.py "$LIBDIR/"
            cp "$tmpdir"/runtime-guardian/src/baseline_detector.py "$LIBDIR/"
            cp "$tmpdir"/runtime-guardian/src/responder.py "$LIBDIR/"
            log_ok "核心引擎已安装: ebpf_monitor.py, baseline_detector.py, responder.py"
            ;;
        protector)
            cp "$tmpdir"/runtime-guardian/src/guardian_protector.py "$LIBDIR/"
            log_ok "保护模块已安装: guardian_protector.py"
            ;;
        rules)
            cp "$tmpdir"/runtime-guardian/config/rules.yaml "$CONFDIR/"
            log_ok "规则配置已安装: $CONFDIR/rules.yaml"
            ;;
        docs)
            mkdir -p "$LIBDIR/docs"
            cp "$tmpdir"/runtime-guardian/README.md "$LIBDIR/docs/" 2>/dev/null || true
            cp "$tmpdir"/runtime-guardian/docs/*.md "$LIBDIR/docs/" 2>/dev/null || true
            log_ok "文档已安装: $LIBDIR/docs/"
            ;;
    esac
    
    rm -rf "$tmpdir"
}

install_module_from_source() {
    local module=$1
    log_info "从源码安装 $module..."
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] cp src/*.py → $LIBDIR/"
        return
    fi
    
    # 从当前源码目录安装
    local src_dir
    src_dir="$(cd "$(dirname "$0")/.." && pwd)"
    
    case "$module" in
        core)
            cp "$src_dir/src/ebpf_monitor.py" "$LIBDIR/"
            cp "$src_dir/src/baseline_detector.py" "$LIBDIR/"
            cp "$src_dir/src/responder.py" "$LIBDIR/"
            ;;
        protector)
            cp "$src_dir/src/guardian_protector.py" "$LIBDIR/"
            ;;
        rules)
            cp "$src_dir/config/rules.yaml" "$CONFDIR/"
            ;;
        docs)
            mkdir -p "$LIBDIR/docs"
            cp "$src_dir/README.md" "$LIBDIR/docs/" 2>/dev/null || true
            cp "$src_dir/docs/"*.md "$LIBDIR/docs/" 2>/dev/null || true
            ;;
    esac
}

# ================================================================
# 创建启动器脚本
# ================================================================
create_launcher() {
    log_step "创建 CLI 启动器"
    
    local launcher="$BINDIR/runtime-guardian"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] 写入 $launcher"
        return
    fi
    
    cat > "$launcher" << 'LAUNCHER_EOF'
#!/usr/bin/env bash
# Runtime Guardian CLI 启动器
LIBDIR="/usr/local/lib/runtime-guardian"
CONFDIR="/etc/runtime-guardian"

case "${1:-start}" in
    start)
        exec python3 "$LIBDIR/ebpf_monitor.py" \
            --rules "$CONFDIR/rules.yaml" \
            "${@:2}"
        ;;
    check)
        python3 -c "
import sys; sys.path.insert(0, '$LIBDIR')
from guardian_protector import GuardianProtector
from baseline_detector import BaselineDetector
print('✅ Runtime Guardian 组件全部可用')
"
        ;;
    version)
        echo "Runtime Guardian v${VERSION:-2.0.0}"
        ;;
    *)
        echo "用法: runtime-guardian {start|check|version} [参数]"
        echo ""
        echo "  start    — 启动监控器"
        echo "  check    — 检查组件可用性"
        echo "  version  — 显示版本"
        exit 1
        ;;
esac
LAUNCHER_EOF

    chmod +x "$launcher"
    log_ok "启动器已创建: $launcher"
}

# ================================================================
# 安装 systemd 服务
# ================================================================
install_systemd_service() {
    log_step "安装 systemd 服务"
    
    local service_file="$SYSTEMDDIR/runtime-guardian.service"
    local src_root
    src_root="$(cd "$(dirname "$0")/.." && pwd)"
    
    if [[ -f "$src_root/deploy/runtime-guardian.service" ]]; then
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "[DRY-RUN] cp deploy/runtime-guardian.service → $service_file"
        else
            cp "$src_root/deploy/runtime-guardian.service" "$service_file"
            sed -i "s|/usr/local|$PREFIX|g" "$service_file"
            systemctl daemon-reload
            log_ok "systemd 服务已注册: $service_file"
        fi
    else
        log_warn "systemd 服务文件不存在，跳过（不影响手动启动）"
    fi
    
    # logrotate 配置
    local logrotate_file="/etc/logrotate.d/runtime-guardian"
    if [[ "$DRY_RUN" != "true" ]]; then
        cat > "$logrotate_file" << EOF
/var/log/runtime-guardian/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
    postrotate
        systemctl reload runtime-guardian 2>/dev/null || true
    endscript
}
EOF
        log_ok "logrotate 配置已安装: $logrotate_file"
    fi
}

# ================================================================
# 安装后检查
# ================================================================
post_install_check() {
    log_step "安装后验证"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] 跳过验证"
        return
    fi
    
    local errors=0
    
    # 检查文件
    for f in "$LIBDIR/ebpf_monitor.py" "$LIBDIR/baseline_detector.py"; do
        if [[ -f "$f" ]]; then
            log_ok "$f"
        else
            log_error "缺失: $f"
            errors=$((errors + 1))
        fi
    done
    
    # Python 语法检查
    python3 -m py_compile "$LIBDIR/ebpf_monitor.py" 2>/dev/null && \
        log_ok "ebpf_monitor.py 语法正确" || \
        { log_error "ebpf_monitor.py 语法错误"; errors=$((errors + 1)); }
    
    if [[ $errors -eq 0 ]]; then
        log_ok "安装验证全部通过"
    else
        log_error "$errors 项验证失败"
        exit 1
    fi
}

# ================================================================
# 卸载
# ================================================================
do_uninstall() {
    log_step "卸载 Runtime Guardian"
    
    if [[ "$DRY_RUN" != "true" ]]; then
        # 停止服务
        systemctl stop runtime-guardian 2>/dev/null || true
        systemctl disable runtime-guardian 2>/dev/null || true
        rm -f "$SYSTEMDDIR/runtime-guardian.service"
        systemctl daemon-reload 2>/dev/null || true
        
        # 删除文件
        rm -rf "$LIBDIR"
        rm -f "$BINDIR/runtime-guardian"
        rm -f "$CONFDIR/rules.yaml"
        rm -f "/etc/logrotate.d/runtime-guardian"
        
        log_ok "卸载完成"
    else
        log_info "[DRY-RUN] 将删除以上文件"
    fi
}

# ================================================================
# 状态检查
# ================================================================
show_status() {
    echo "Runtime Guardian — 安装状态"
    echo "============================="
    echo ""
    
    echo "安装路径:"
    for d in "$BINDIR" "$LIBDIR" "$CONFDIR" "$DATADIR" "$LOGDIR"; do
        if [[ -d "$d" ]]; then
            echo "  ✅ $d ($(find "$d" -type f 2>/dev/null | wc -l) 个文件)"
        else
            echo "  ❌ $d (不存在)"
        fi
    done
    
    echo ""
    echo "关键文件:"
    for f in "$LIBDIR/ebpf_monitor.py" "$LIBDIR/baseline_detector.py" \
             "$LIBDIR/guardian_protector.py" "$LIBDIR/responder.py" \
             "$CONFDIR/rules.yaml" "$BINDIR/runtime-guardian"; do
        if [[ -f "$f" ]]; then
            echo "  ✅ $f"
        else
            echo "  ❌ $f (缺失)"
        fi
    done
    
    echo ""
    echo "服务状态:"
    if systemctl is-active runtime-guardian &>/dev/null; then
        echo "  ✅ runtime-guardian.service 运行中"
    else
        echo "  ⬜ runtime-guardian.service 未运行"
    fi
}

# ================================================================
# 主流程
# ================================================================
main() {
    echo ""
    echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}║   Runtime Guardian v${VERSION} — 企业级安装器  ║${NC}"
    echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
    echo ""
    
    parse_args "$@"
    
    if [[ "$DO_UNINSTALL" == "true" ]]; then
        do_uninstall
        exit 0
    fi
    
    check_root
    
    # 确定要安装的模块
    if [[ ${#SELECTED_MODULES[@]} -eq 0 ]]; then
        SELECTED_MODULES=("${ALL_MODULES[@]}")
    fi
    
    echo -e "安装配置:"
    echo -e "  版本:     ${GREEN}v${VERSION}${NC}"
    echo -e "  安装路径: ${GREEN}$PREFIX${NC}"
    echo -e "  模块:     ${GREEN}${SELECTED_MODULES[*]}${NC}"
    echo -e "  模式:     ${YELLOW}$([[ "$DRY_RUN" == "true" ]] && echo "演习" || echo "实际安装")${NC}"
    echo ""
    
    if [[ "${YES_FLAG:-false}" != "true" ]] && [[ "$DRY_RUN" != "true" ]]; then
        read -rp "确认安装? [Y/n] " confirm
        if [[ "$confirm" =~ ^[Nn] ]]; then
            echo "已取消"
            exit 0
        fi
    fi
    
    check_os
    create_directories
    
    for mod in "${SELECTED_MODULES[@]}"; do
        install_module "$mod"
    done
    
    create_launcher
    install_systemd_service
    post_install_check
    
    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}${BOLD}║          ✅  安装完成！                       ║${NC}"
    echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "快速开始:"
    echo -e "  检查组件:  ${BOLD}runtime-guardian check${NC}"
    echo -e "  启动监控:  ${BOLD}sudo runtime-guardian start${NC}"
    echo -e "  系统服务:  ${BOLD}sudo systemctl start runtime-guardian${NC}"
    echo -e "  查看日志:  ${BOLD}journalctl -u runtime-guardian -f${NC}"
    echo ""
}

main "$@"
