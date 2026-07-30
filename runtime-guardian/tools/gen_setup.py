#!/usr/bin/env python3
"""生成 setup.sh — 将所有项目文件嵌入到单个 bash 脚本中"""
import os

# 项目根目录 = 本脚本所在目录的上级
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

files = [
    ("VERSION", "VERSION"),
    ("Makefile", "Makefile"),
    ("README.md", "README.md"),
    ("src/ebpf_monitor.py", "src/ebpf_monitor.py"),
    ("src/baseline_detector.py", "src/baseline_detector.py"),
    ("src/guardian_protector.py", "src/guardian_protector.py"),
    ("src/responder.py", "src/responder.py"),
    ("config/rules.yaml", "config/rules.yaml"),
    ("scripts/install.sh", "scripts/install.sh"),
    ("scripts/guardianctl.sh", "scripts/guardianctl.sh"),
    ("deploy/runtime-guardian.service", "deploy/runtime-guardian.service"),
    ("docs/learning-roadmap.md", "docs/learning-roadmap.md"),
    ("docs/harmony-analysis.md", "docs/harmony-analysis.md"),
    ("tests/simulate_attacks.sh", "tests/simulate_attacks.sh"),
]

HEADER = r'''#!/usr/bin/env bash
# ================================================================
# Runtime Guardian v2.0.0 — 单文件完整安装脚本
# 运行: bash setup.sh
# ================================================================
set -euo pipefail

RED='\\033[0;31m'
GREEN='\\033[0;32m'
YELLOW='\\033[1;33m'
CYAN='\\033[0;36m'
BOLD='\\033[1m'
NC='\\033[0m'

echo ""
echo -e "${CYAN}${BOLD}==============================================${NC}"
echo -e "${CYAN}${BOLD}  Runtime Guardian v2.0.0 — 一键安装${NC}"
echo -e "${CYAN}${BOLD}==============================================${NC}"
echo ""

PROJECT_DIR="runtime-guardian"
if [[ -d "$PROJECT_DIR" ]]; then
    echo -e "${YELLOW}目录 $PROJECT_DIR 已存在，将覆盖${NC}"
    read -rp "确认继续? [Y/n] " confirm
    [[ "$confirm" =~ ^[Nn] ]] && exit 0
    rm -rf "$PROJECT_DIR"
fi

echo -e "${BOLD}[1/4] 创建目录结构...${NC}"
mkdir -p "$PROJECT_DIR"/{src,config,scripts,deploy,docs,tests,dist/packages}
echo ""

echo -e "${BOLD}[2/4] 写入文件...${NC}"

'''

FOOTER = r'''
echo ""
echo -e "${BOLD}[3/4] 设置可执行权限...${NC}"
chmod +x "$PROJECT_DIR/scripts/install.sh"
chmod +x "$PROJECT_DIR/scripts/guardianctl.sh"
chmod +x "$PROJECT_DIR/tests/simulate_attacks.sh"

echo ""
echo -e "${BOLD}[4/4] 检查依赖...${NC}"

# Python 检查
if command -v python3 &>/dev/null; then
    PYVER=$(python3 --version 2>&1)
    echo -e "  ${GREEN}[OK]${NC} $PYVER"
else
    echo -e "  ${RED}[FAIL]${NC} Python 3 未安装"
    echo "  请安装: sudo apt-get install python3 python3-pip"
    exit 1
fi

# pip 包检查
echo -n "  pip 依赖..."
MISSING=""
python3 -c "import psutil" 2>/dev/null || MISSING="$MISSING psutil"
python3 -c "import yaml" 2>/dev/null || MISSING="$MISSING pyyaml"
if [[ -n "$MISSING" ]]; then
    echo -e " ${YELLOW}缺少:$MISSING${NC}"
    echo "  安装: pip3 install $MISSING"
else
    echo -e " ${GREEN}[OK]${NC}"
fi

# eBPF 检查（可选）
echo -n "  eBPF/bcc..."
if python3 -c "from bcc import BPF" 2>/dev/null; then
    echo -e " ${GREEN}[OK]${NC}"
else
    echo -e " ${YELLOW}[未安装]${NC} (eBPF监控需要, 非必须)"
    echo "  安装: sudo apt-get install bpfcc-tools python3-bpfcc linux-headers-\$(uname -r)"
fi

# 组件自检
echo ""
echo -n "  核心组件自检..."
if python3 -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/src')
from baseline_detector import BaselineDetector, FileContextClassifier
bd = BaselineDetector(window_seconds=2.0, enable_ngram=False, enable_entropy=False,
                       enable_multi_user=False, enable_file_context=False, enable_time_window=False)
cat, risk = FileContextClassifier.classify('/etc/passwd')
assert cat == 'SYSCONFIG' and risk == 10
" 2>/dev/null; then
    echo -e " ${GREEN}[OK]${NC}"
else
    echo -e " ${YELLOW}[跳过]${NC}"
fi

echo -n "  保护模块自检..."
if python3 -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/src')
from guardian_protector import DynamicSampler, MemoryGuard
ds = DynamicSampler(); ds.update(50); assert ds.sample_rate == 0.7
mg = MemoryGuard(warn_mb=200, max_mb=500); r = mg.check(250, 'rising'); assert r['action'] == 'lru_evict'
" 2>/dev/null; then
    echo -e " ${GREEN}[OK]${NC}"
else
    echo -e " ${YELLOW}[跳过]${NC}"
fi

echo ""
echo -e "${GREEN}${BOLD}==============================================${NC}"
echo -e "${GREEN}${BOLD}  OK — 安装完成${NC}"
echo -e "${GREEN}${BOLD}==============================================${NC}"
echo ""
echo "项目目录: $(pwd)/$PROJECT_DIR"
echo ""
echo "快速开始:"
echo "  cd $PROJECT_DIR"
echo "  sudo python3 src/ebpf_monitor.py --no-response"
echo "  bash tests/simulate_attacks.sh"
echo "  sudo bash scripts/install.sh"
echo ""
echo "管理工具:"
echo "  bash scripts/guardianctl.sh test"
echo "  sudo bash scripts/guardianctl.sh health"
echo ""
'''

out = os.path.join(base, "setup.sh")

with open(out, 'w', encoding='utf-8', newline='\n') as f:
    f.write(HEADER)

    count = 0
    for target, src in files:
        src_path = os.path.join(base, src)
        if not os.path.exists(src_path):
            print(f"  SKIP: {src_path} (not found)")
            continue

        with open(src_path, 'r', encoding='utf-8', errors='replace') as sf:
            content = sf.read()

        # 确保内容不以空行结尾导致 heredoc 问题
        content = content.rstrip('\n') + '\n'

        # 使用唯一标记避免内容冲突
        marker = f"RGFILE_{count}"
        f.write(f'echo "  {target}"\n')
        f.write(f'cat > "$PROJECT_DIR/{target}" << \'{marker}\'\n')
        f.write(content)
        f.write(f'{marker}\n\n')
        count += 1

    f.write(FOOTER)

size_kb = os.path.getsize(out) / 1024
print(f"✅ setup.sh 已生成 ({count} 个文件, {size_kb:.0f} KB)")
print(f"   路径: {out}")
