# ================================================================
# Runtime Guardian — 企业级构建系统
# ================================================================
# 用法:
#   make all           — 编译检查 + 打包所有模块
#   make check         — 语法检查
#   make package       — 生成分发包
#   make install       — 安装到系统
#   make uninstall     — 从系统卸载
#   make clean         — 清理构建产物
#   make dist          — 生成完整发布包
#   make help          — 显示帮助
# ================================================================

SHELL := /bin/bash
PYTHON := python3
PIP := pip3

# 版本信息
VERSION := $(shell cat VERSION 2>/dev/null || echo "2.0.0")
BUILD_ID := $(shell date +%Y%m%d%H%M%S)
ARCH := $(shell uname -m)

# 目录
PREFIX ?= /usr/local
BINDIR := $(PREFIX)/bin
LIBDIR := $(PREFIX)/lib/runtime-guardian
CONFDIR := /etc/runtime-guardian
DATADIR := /var/lib/runtime-guardian
LOGDIR := /var/log/runtime-guardian
RUNDIR := /var/run/runtime-guardian
SYSTEMDDIR := /etc/systemd/system

# 源文件目录
SRCDIR := src
DOCSDIR := docs
CONFIGDIR := config
TESTDIR := tests
SCRIPTDIR := scripts
DEPLOYDIR := deploy

# 打包输出目录
BUILDDIR := build
DISTDIR := dist
PACKAGES := $(DISTDIR)/packages

# ---- 模块定义 ----
MODULES := core protector rules docs

# 各模块包含的文件
MODULE_FILES_core := src/ebpf_monitor.py src/baseline_detector.py src/responder.py
MODULE_FILES_protector := src/guardian_protector.py
MODULE_FILES_rules := config/rules.yaml
MODULE_FILES_docs := README.md docs/learning-roadmap.md docs/harmony-analysis.md

# ================================================================
# 默认目标
# ================================================================
.PHONY: all
all: check package

# ================================================================
# 帮助
# ================================================================
.PHONY: help
help:
	@echo "Runtime Guardian v$(VERSION) — 构建系统"
	@echo ""
	@echo "目标:"
	@echo "  make check        — Python 语法检查"
	@echo "  make package      — 生成模块化分发包 (.tar.gz)"
	@echo "  make install      — 安装到系统 (需要 root)"
	@echo "  make uninstall    — 从系统卸载"
	@echo "  make dist         — 生成完整发布包"
	@echo "  make clean        — 清理构建产物"
	@echo "  make distclean    — 清理所有（含分发包）"
	@echo "  make version      — 显示版本信息"
	@echo ""
	@echo "模块:"
	@echo "  make package-core       — 仅打包核心引擎"
	@echo "  make package-protector  — 仅打包保护模块"
	@echo "  make package-rules      — 仅打包规则配置"
	@echo "  make package-docs       — 仅打包文档"
	@echo ""
	@echo "安装选项:"
	@echo "  make install MODULES=core,protector  — 选择性安装"
	@echo "  PREFIX=/opt make install             — 自定义安装路径"

# ================================================================
# 版本
# ================================================================
.PHONY: version
version:
	@echo "Runtime Guardian v$(VERSION) (build $(BUILD_ID))"
	@echo "Python: $$($(PYTHON) --version 2>&1)"
	@echo "Arch: $(ARCH)"

VERSION:
	echo "$(VERSION)" > VERSION

# ================================================================
# 语法检查
# ================================================================
.PHONY: check
check:
	@echo "🔍 语法检查..."
	@errors=0; \
	for f in $(SRCDIR)/*.py; do \
		$(PYTHON) -m py_compile $$f 2>&1 || errors=$$((errors+1)); \
	done; \
	for f in $(SCRIPTDIR)/*.sh; do \
		[ -f $$f ] && bash -n $$f 2>&1 || true; \
	done; \
	if [ $$errors -eq 0 ]; then \
		echo "✅ 全部通过"; \
	else \
		echo "❌ $$errors 个文件有错误"; \
		exit 1; \
	fi

# ================================================================
# 打包
# ================================================================
.PHONY: package package-core package-protector package-rules package-docs

package: package-core package-protector package-rules package-docs
	@echo ""
	@echo "========================================"
	@echo "📦 打包完成 — $(DISTDIR)/packages/"
	@echo "========================================"
	@ls -lh $(PACKAGES)/*.tar.gz 2>/dev/null | awk '{print "  " $$NF " (" $$5 ")"}'
	@echo ""
	@echo "安装: sudo bash scripts/install.sh"
	@echo "模块化安装: sudo bash scripts/install.sh --modules core,protector"

package-core: VERSION
	@mkdir -p $(PACKAGES)
	@echo "📦 打包: runtime-guardian-core-v$(VERSION).tar.gz"
	@tar czf $(PACKAGES)/runtime-guardian-core-v$(VERSION).tar.gz \
		--transform 's,^,runtime-guardian/,' \
		VERSION \
		src/ebpf_monitor.py \
		src/baseline_detector.py \
		src/responder.py
	@echo "   ├── ebpf_monitor.py (eBPF 监控引擎)"
	@echo "   ├── baseline_detector.py (七维基线检测)"
	@echo "   └── responder.py (自动响应模块)"

package-protector: VERSION
	@mkdir -p $(PACKAGES)
	@echo "📦 打包: runtime-guardian-protector-v$(VERSION).tar.gz"
	@tar czf $(PACKAGES)/runtime-guardian-protector-v$(VERSION).tar.gz \
		--transform 's,^,runtime-guardian/,' \
		VERSION \
		src/guardian_protector.py
	@echo "   └── guardian_protector.py (5层防死机保护)"

package-rules: VERSION
	@mkdir -p $(PACKAGES)
	@echo "📦 打包: runtime-guardian-rules-v$(VERSION).tar.gz"
	@tar czf $(PACKAGES)/runtime-guardian-rules-v$(VERSION).tar.gz \
		--transform 's,^,runtime-guardian/,' \
		VERSION \
		config/rules.yaml
	@echo "   └── rules.yaml (检测规则+白名单+多用户配置)"

package-docs: VERSION
	@mkdir -p $(PACKAGES)
	@echo "📦 打包: runtime-guardian-docs-v$(VERSION).tar.gz"
	@tar czf $(PACKAGES)/runtime-guardian-docs-v$(VERSION).tar.gz \
		--transform 's,^,runtime-guardian/,' \
		VERSION \
		README.md \
		docs/learning-roadmap.md \
		docs/harmony-analysis.md
	@echo "   ├── README.md"
	@echo "   ├── learning-roadmap.md"
	@echo "   └── harmony-analysis.md"

# ================================================================
# 完整发布包（包含安装脚本 + systemd + 所有模块）
# ================================================================
.PHONY: dist
dist: check package
	@echo ""
	@echo "📦 生成完整发布包: runtime-guardian-v$(VERSION).tar.gz"
	@mkdir -p $(DISTDIR)
	@tar czf $(DISTDIR)/runtime-guardian-v$(VERSION).tar.gz \
		--transform 's,^,runtime-guardian-v$(VERSION)/,' \
		VERSION \
		README.md \
		Makefile \
		src/ \
		config/ \
		docs/ \
		scripts/ \
		deploy/ \
		tests/
	@echo "✅ 发布包: $(DISTDIR)/runtime-guardian-v$(VERSION).tar.gz"
	@ls -lh $(DISTDIR)/runtime-guardian-v$(VERSION).tar.gz | awk '{print "  大小: " $$5}'

# ================================================================
# 安装
# ================================================================
.PHONY: install
install:
	@if [ "$$(id -u)" != "0" ]; then \
		echo "❌ 安装需要 root 权限: sudo make install"; \
		exit 1; \
	fi
	@bash scripts/install.sh --prefix $(PREFIX) --modules $(MODULES)

# ================================================================
# 卸载
# ================================================================
.PHONY: uninstall
uninstall:
	@if [ "$$(id -u)" != "0" ]; then \
		echo "❌ 卸载需要 root 权限: sudo make uninstall"; \
		exit 1; \
	fi
	@bash scripts/install.sh --uninstall --prefix $(PREFIX)

# ================================================================
# 清理
# ================================================================
.PHONY: clean distclean
clean:
	@echo "🧹 清理构建产物..."
	@rm -rf $(BUILDDIR)
	@rm -f VERSION
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ 清理完成"

distclean: clean
	@echo "🧹 清理分发包..."
	@rm -rf $(DISTDIR)
	@echo "✅ 全部清理完成"