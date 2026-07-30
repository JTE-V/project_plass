#!/usr/bin/env bash
# ==============================================================================
# Runtime Guardian — 攻击模拟脚本
# ==============================================================================
# 用于测试 Runtime Guardian 监控器对各类攻击行为的检测能力。
#
# 使用方式:
#   sudo bash simulate_attacks.sh                  # 运行全部攻击（需 root）
#   sudo bash simulate_attacks.sh --attack 1       # 仅运行攻击1: 敏感文件访问
#   sudo bash simulate_attacks.sh --attack 2       # 仅运行攻击2: 系统调用风暴
#   sudo bash simulate_attacks.sh --attack 3       # 仅运行攻击3: 下载执行攻击
#   sudo bash simulate_attacks.sh --attack 4       # 仅运行攻击4: 网络扫描
#   sudo bash simulate_attacks.sh --attack 1,3     # 运行攻击1和攻击3
#   sudo bash simulate_attacks.sh --delay 2        # 攻击间延迟2秒（默认1秒）
#
# 攻击类型:
#   攻击1 - 🔴 敏感文件访问: cat /etc/passwd, cat /etc/shadow
#   攻击2 - 🟠 系统调用风暴: 循环 open/close 文件 200 次
#   攻击3 - 🟡 下载执行攻击: wget + chmod + exec 行为链
#   攻击4 - 🔵 网络扫描:     循环连接多个端口
# ==============================================================================

set -euo pipefail

# -------------------------------------------------------------------
# 默认配置
# -------------------------------------------------------------------
DELAY=1             # 攻击间延迟（秒）
TARGET_ATTACKS=""   # 要运行的攻击编号列表
STORM_COUNT=200     # 风暴攻击的循环次数
SCAN_PORTS=(22 80 443 3306 6379 8080 8443 9090 5432 27017)  # 扫描端口列表
STORM_FILE="/tmp/rg_storm_test_file_$$"
DOWNLOAD_URL="http://httpbin.org/get"  # 安全的测试下载地址（不会真执行恶意代码）
DOWNLOAD_FILE="/tmp/rg_download_test_$$"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# ==============================================================================
# 辅助函数
# ==============================================================================

print_banner() {
    local title="$1"
    local color="${2:-$CYAN}"
    echo ""
    echo -e "${color}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${color}${BOLD}║${NC}  ${BOLD}${title}${NC}"
    echo -e "${color}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

print_separator() {
    echo ""
    echo -e "${CYAN}────────────────────────────────────────────────────────────────${NC}"
    echo ""
}

print_result() {
    local attack_num="$1"
    local desc="$2"
    echo -e "  ${YELLOW}→${NC} 攻击${attack_num} [${desc}] ${GREEN}执行完毕${NC}"
}

check_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo -e "${RED}${BOLD}⚠ 警告: 当前非 root 用户，部分攻击模拟可能受限。${NC}"
        echo -e "${YELLOW}  建议使用 sudo 运行以获得完整测试效果。${NC}"
        echo ""
    fi
}

cleanup() {
    rm -f "$STORM_FILE" "$DOWNLOAD_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# ==============================================================================
# 攻击1: 敏感文件访问
# ==============================================================================
attack_sensitive_files() {
    print_banner "🔴 攻击1: 敏感文件访问" "$RED"

    echo -e "${BOLD}描述:${NC} 尝试读取系统敏感文件，测试 eBPF openat 钩子能否捕获。"
    echo -e "${BOLD}检测点:${NC} sys_enter_openat → 路径匹配 → 敏感文件告警"
    print_separator

    # 1.1 访问 /etc/passwd
    echo -e "${YELLOW}[1.1]${NC} 尝试读取 ${RED}/etc/passwd${NC} ..."
    cat /etc/passwd > /dev/null 2>&1 && \
        echo -e "      ${GREEN}✓ /etc/passwd 读取成功${NC} (监控器应触发 🔴 告警)" || \
        echo -e "      ${RED}✗ /etc/passwd 读取失败${NC}"

    sleep 0.5

    # 1.2 访问 /etc/shadow
    echo -e "${YELLOW}[1.2]${NC} 尝试读取 ${RED}/etc/shadow${NC} (需要 root)..."
    cat /etc/shadow > /dev/null 2>&1 && \
        echo -e "      ${GREEN}✓ /etc/shadow 读取成功${NC} (监控器应触发 🔴 告警)" || \
        echo -e "      ${YELLOW}⚠  /etc/shadow 读取失败${NC} (非 root 用户正常，仍尝试了 openat)"

    sleep 0.5

    # 1.3 访问 /root/.ssh/authorized_keys
    echo -e "${YELLOW}[1.3]${NC} 尝试读取 ${RED}/root/.ssh/authorized_keys${NC} ..."
    cat /root/.ssh/authorized_keys > /dev/null 2>&1 && \
        echo -e "      ${GREEN}✓ authorized_keys 读取成功${NC} (监控器应触发 🔴 告警)" || \
        echo -e "      ${YELLOW}⚠  authorized_keys 读取失败${NC} (文件可能不存在)"

    sleep 0.5

    # 1.4 访问 /etc/sudoers
    echo -e "${YELLOW}[1.4]${NC} 尝试读取 ${RED}/etc/sudoers${NC} ..."
    cat /etc/sudoers > /dev/null 2>&1 && \
        echo -e "      ${GREEN}✓ /etc/sudoers 读取成功${NC} (监控器应触发 🔴 告警)" || \
        echo -e "      ${YELLOW}⚠  /etc/sudoers 读取失败${NC}"

    print_separator
    print_result "1" "敏感文件访问"
}

# ==============================================================================
# 攻击2: 系统调用风暴
# ==============================================================================
attack_syscall_storm() {
    print_banner "🟠 攻击2: 系统调用风暴" "$YELLOW"

    echo -e "${BOLD}描述:${NC} 1秒内循环 open/close 文件 ${STORM_COUNT} 次，模拟勒索软件扫描行为。"
    echo -e "${BOLD}检测点:${NC} eBPF 内核态 LRU 计数器 → 超阈值推送 → 用户态二次验证"
    echo -e "${BOLD}阈值:${NC} 默认为 50 次/秒（本次模拟 ${STORM_COUNT} 次）"
    print_separator

    # 创建测试文件
    echo "test data for storm attack" > "$STORM_FILE"

    echo -e "${YELLOW}[2.1]${NC} 开始系统调用风暴（${STORM_COUNT} 次 open/close）..."
    echo -e "      ${BLUE}(实时计数器每 20 次打印一个点)${NC}"

    local start_time
    start_time=$(date +%s%N)

    local i
    for ((i = 1; i <= STORM_COUNT; i++)); do
        # 使用 exec 重定向方式快速 open/close
        exec 3<>"$STORM_FILE" 2>/dev/null && exec 3>&- 2>/dev/null || true

        # 每 20 次打印进度
        if ((i % 20 == 0)); then
            echo -ne "      ."
        fi
    done
    echo ""

    local end_time
    end_time=$(date +%s%N)
    local elapsed_ms=$(( (end_time - start_time) / 1000000 ))

    echo ""
    echo -e "      ${GREEN}✓ 完成${NC} ${STORM_COUNT} 次 open/close，耗时 ${BOLD}${elapsed_ms}ms${NC}"
    if ((elapsed_ms > 0)); then
        local rate=$(( STORM_COUNT * 1000 / elapsed_ms ))
        echo -e "      ${GREEN}  速率: ~${rate} 次/秒${NC} ${RED}(远超默认阈值 50 次/秒)${NC}"
    fi
    echo -e "      ${YELLOW}  监控器应触发 🟠 系统调用风暴告警${NC}"

    print_separator
    print_result "2" "系统调用风暴"
}

# ==============================================================================
# 攻击3: 下载执行攻击行为链
# ==============================================================================
attack_download_exec() {
    print_banner "🟡 攻击3: 下载执行攻击（行为链）" "$YELLOW"

    echo -e "${BOLD}描述:${NC} 模拟 wget 下载 → chmod 加权限 → 执行 的完整攻击链。"
    echo -e "${BOLD}检测点:${NC} 用户态状态机追踪: open → read → write → exec 序列"
    echo -e "${BOLD}行为链:${NC} socket/connect(网络下载) → open(O_CREAT,写文件) → write(写入内容) → "
    echo -e "         close → chmod/fchmod(加执行权限) → execve(执行)"
    print_separator

    # 3.1 模拟下载（wget/curl）
    echo -e "${YELLOW}[3.1]${NC} 模拟下载文件 (wget → ${DOWNLOAD_FILE}) ..."
    if command -v wget &>/dev/null; then
        wget -q -O "$DOWNLOAD_FILE" "$DOWNLOAD_URL" 2>&1 && \
            echo -e "      ${GREEN}✓ wget 下载成功${NC}" || \
            echo -e "      ${YELLOW}⚠  wget 下载失败${NC} (网络不可用，但行为链已触发)"
    elif command -v curl &>/dev/null; then
        curl -s -o "$DOWNLOAD_FILE" "$DOWNLOAD_URL" 2>&1 && \
            echo -e "      ${GREEN}✓ curl 下载成功${NC}" || \
            echo -e "      ${YELLOW}⚠  curl 下载失败${NC} (网络不可用，但行为链已触发)"
    else
        # 没有 wget/curl：手动模拟网络下载行为
        echo -e "      ${YELLOW}⚠  未找到 wget/curl，使用 Python 模拟网络下载...${NC}"
        python3 -c "
import urllib.request
urllib.request.urlretrieve('$DOWNLOAD_URL', '$DOWNLOAD_FILE')
" 2>/dev/null && echo -e "      ${GREEN}✓ Python 下载成功${NC}" || \
            echo -e "      ${YELLOW}⚠  Python 下载失败${NC} (行为链仍部分触发)"
        # 即使下载失败也创建文件来触发后续行为链
        echo "#!/bin/bash\necho test" > "$DOWNLOAD_FILE" 2>/dev/null || true
    fi

    sleep 0.5

    # 3.2 加执行权限（chmod）
    echo -e "${YELLOW}[3.2]${NC} 加执行权限 (chmod +x ${DOWNLOAD_FILE}) ..."
    chmod +x "$DOWNLOAD_FILE" 2>/dev/null && \
        echo -e "      ${GREEN}✓ chmod +x 成功${NC} (监控器应记录: fchmod)" || \
        echo -e "      ${RED}✗ chmod 失败${NC}"

    sleep 0.3

    # 3.3 执行文件（execve）
    echo -e "${YELLOW}[3.3]${NC} 执行下载的文件 (execve ${DOWNLOAD_FILE}) ..."
    "$DOWNLOAD_FILE" 2>/dev/null && \
        echo -e "      ${GREEN}✓ 文件执行成功${NC} (监控器应触发 🟡 行为链告警)" || \
        echo -e "      ${YELLOW}⚠  文件执行完毕${NC} (行为链已完成)"

    print_separator
    echo -e "  ${BOLD}完整行为链:${NC}"
    echo -e "    socket/connect → open(O_CREAT) → write → close → chmod → execve"
    echo -e "  ${RED}  监控器应触发 🟡 可疑行为链告警${NC}"
    print_separator
    print_result "3" "下载执行攻击行为链"
}

# ==============================================================================
# 攻击4: 网络扫描
# ==============================================================================
attack_network_scan() {
    print_banner "🔵 攻击4: 网络扫描（端口扫描模拟）" "$BLUE"

    echo -e "${BOLD}描述:${NC} 短时间内循环连接多个端口，模拟端口扫描/挖矿蠕虫传播行为。"
    echo -e "${BOLD}检测点:${NC} eBPF connect 跟踪 → 用户态 HyperLogLog 基数估计 → 高频连接告警"
    echo -e "${BOLD}目标端口:${NC} ${SCAN_PORTS[*]}"
    print_separator

    local target_host="127.0.0.1"
    local start_time
    start_time=$(date +%s%N)

    echo -e "${YELLOW}[4.1]${NC} 开始端口扫描 (目标: ${target_host})..."

    local connected=0
    local failed=0
    local port
    for port in "${SCAN_PORTS[@]}"; do
        # 使用 bash 内置 /dev/tcp 进行快速连接测试
        # 设置超时 0.5 秒避免卡住
        if timeout 0.5 bash -c "exec 3<>/dev/tcp/${target_host}/${port}" 2>/dev/null; then
            echo -e "      ${GREEN}  ✓${NC} 端口 ${BOLD}${port}${NC} — 连接成功"
            ((connected++)) || true
        else
            echo -e "      ${YELLOW}  ✗${NC} 端口 ${port} — 连接失败/拒绝"
            ((failed++)) || true
        fi
    done

    echo ""
    echo -e "      ${GREEN}✓ 扫描完成${NC} — ${connected} 个成功, ${failed} 个失败"

    local end_time
    end_time=$(date +%s%N)
    local elapsed_ms=$(( (end_time - start_time) / 1000000 ))

    echo -e "      ${GREEN}  总耗时: ${elapsed_ms}ms${NC}"

    # 第二轮：对同一端口快速重复连接（模拟蠕虫传播）
    echo ""
    echo -e "${YELLOW}[4.2]${NC} 第二轮：对端口 80 快速重复连接 10 次（模拟蠕虫传播）..."

    local j
    for ((j = 1; j <= 10; j++)); do
        timeout 0.3 bash -c "exec 3<>/dev/tcp/${target_host}/80" 2>/dev/null && true
    done
    echo -e "      ${GREEN}✓ 完成 10 次重复连接${NC}"
    echo -e "      ${RED}  监控器应触发 🔵 网络异常告警${NC}"

    print_separator
    print_result "4" "网络端口扫描"
}

# ==============================================================================
# 参数解析
# ==============================================================================
usage() {
    echo "用法: sudo bash $0 [选项]"
    echo ""
    echo "选项:"
    echo "  --attack N      仅运行攻击 N (1-4)，多个用逗号分隔，如 --attack 1,3"
    echo "  --delay N       攻击间延迟秒数（默认: ${DELAY}）"
    echo "  --storm-count N 系统调用风暴次数（默认: ${STORM_COUNT}）"
    echo "  --help          显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  sudo bash $0                       # 运行全部攻击"
    echo "  sudo bash $0 --attack 2            # 仅运行系统调用风暴"
    echo "  sudo bash $0 --attack 1,3 --delay 3"
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --attack)
                TARGET_ATTACKS="$2"
                shift 2
                ;;
            --delay)
                DELAY="$2"
                shift 2
                ;;
            --storm-count)
                STORM_COUNT="$2"
                shift 2
                ;;
            --help|-h)
                usage
                ;;
            *)
                echo -e "${RED}未知参数: $1${NC}"
                usage
                ;;
        esac
    done
}

# ==============================================================================
# 主流程
# ==============================================================================
main() {
    parse_args "$@"

    # 确定要运行的攻击
    local attacks_to_run=()
    if [[ -n "$TARGET_ATTACKS" ]]; then
        IFS=',' read -ra attack_nums <<< "$TARGET_ATTACKS"
        for num in "${attack_nums[@]}"; do
            # 去除空白
            num="${num// /}"
            if [[ "$num" =~ ^[1-4]$ ]]; then
                attacks_to_run+=("$num")
            else
                echo -e "${RED}错误: 无效的攻击编号 '$num'，有效值为 1-4${NC}"
                exit 1
            fi
        done
    else
        attacks_to_run=(1 2 3 4)
    fi

    # ==========================================================================
    # 总横幅
    # ==========================================================================
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║${NC}     ${RED}Runtime Guardian${NC} — ${BOLD}攻击模拟测试套件${NC}                      ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}     用于验证 eBPF 监控器的 4 种攻击检测能力               ${BOLD}║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}目标攻击:${NC} ${attacks_to_run[*]}"
    echo -e "  ${BOLD}攻击延迟:${NC} ${DELAY}s"
    echo -e "  ${BOLD}风暴次数:${NC} ${STORM_COUNT}"
    echo -e "  ${BOLD}当前时间:${NC} $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    check_root

    # ==========================================================================
    # 逐个运行攻击
    # ==========================================================================
    local total=${#attacks_to_run[@]}
    local idx=1
    for attack_num in "${attacks_to_run[@]}"; do
        echo -e "${BOLD}${CYAN}[${idx}/${total}]${NC} 正在准备攻击 ${attack_num}..."
        sleep "$DELAY"

        case "$attack_num" in
            1) attack_sensitive_files ;;
            2) attack_syscall_storm ;;
            3) attack_download_exec ;;
            4) attack_network_scan ;;
        esac

        ((idx++))
    done

    # ==========================================================================
    # 完成
    # ==========================================================================
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║${NC}  ${GREEN}✓ 所有攻击模拟执行完毕${NC}                                    ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}                                                              ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}  检查 Runtime Guardian 输出以验证检测结果:                    ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}    🔴 敏感文件访问告警                                       ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}    🟠 系统调用风暴告警                                       ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}    🟡 可疑行为链告警                                         ${BOLD}║${NC}"
    echo -e "${BOLD}║${NC}    🔵 网络异常告警                                           ${BOLD}║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

main "$@"
