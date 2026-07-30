#!/usr/bin/env bash
# ============================================================
# Runtime Guardian — OMO 快速测试
# 用法: bash omo_test.sh
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Runtime Guardian OMO 攻击测试          ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---- 检查监控器是否在运行 ----
if ! pgrep -f ebpf_monitor.py > /dev/null 2>&1; then
    echo -e "${YELLOW}监控器未运行，请先在另一个终端执行:${NC}"
    echo "  sudo python3 ~/runtime-guardian/src/ebpf_monitor.py --no-response"
    echo ""
    read -p "按回车继续(确认监控器已启动)..."
fi

echo -e "${GREEN}━━━ 测试1: 敏感文件访问 ━━━${NC}"
echo "  模拟: cat /etc/passwd"
cat /etc/passwd > /dev/null 2>&1
echo "  模拟: cat /etc/shadow"
cat /etc/shadow > /dev/null 2>&1
echo "  预期: 监控器输出 🔴 敏感文件访问"
sleep 1

echo ""
echo -e "${GREEN}━━━ 测试2: 系统调用风暴 ━━━${NC}"
echo "  模拟: 150次快速文件打开..."
for i in $(seq 1 150); do
    cat /etc/hostname > /dev/null 2>&1
done
echo "  预期: 监控器输出 🟠 系统调用风暴"
sleep 1

echo ""
echo -e "${GREEN}━━━ 测试3: 行为链攻击 ━━━${NC}"
echo "  模拟: wget + chmod + exec (如果wget可用)"
TMPFILE="/tmp/rg_test_$$"
if command -v wget &>/dev/null; then
    wget -q -O "$TMPFILE" http://example.com 2>/dev/null || echo "test" > "$TMPFILE"
else
    echo "#!/bin/bash" > "$TMPFILE"
    echo "echo test" >> "$TMPFILE"
fi
chmod +x "$TMPFILE" 2>/dev/null
"$TMPFILE" 2>/dev/null || true
rm -f "$TMPFILE"
echo "  预期: 监控器输出 🟡 可疑行为链"
sleep 1

echo ""
echo -e "${GREEN}━━━ 测试4: 基线异常 ━━━${NC}"
echo "  模拟: 批量execve调用..."
for i in $(seq 1 30); do
    /usr/bin/true 2>/dev/null || true
done
echo "  预期: 监控器输出 🟢 基线异常"
sleep 1

echo ""
echo -e "${GREEN}━━━ 测试5: 网络扫描 ━━━${NC}"
echo "  模拟: 快速连接多个端口..."
for port in 22 80 443 8080 3306 6379; do
    timeout 0.3 bash -c "echo >/dev/tcp/127.0.0.1/$port" 2>/dev/null || true
done
echo "  预期: 监控器输出 🔵 网络异常"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  OMO 测试完成                            ║${NC}"
echo -e "${CYAN}║  检查监控器终端，应看到5种告警类型         ║${NC}"
echo -e "${CYAN}║  zcat /tmp/runtime_guardian_*.jsonl.gz    ║${NC}"
echo -e "${CYAN}║  查看详细日志                             ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
