#!/usr/bin/env bash
# 公开仓库脱敏扫描（2026-10-25）
#
# 为什么不用现成的 gitleaks：这个仓库很小，且曾经发生过"私有测试文件被整文件复制进公开提交"
# 的事故（2026-10-23）。除了通用密钥模式，这里还会扫"生产标识"特征（手机号/QQ 邮箱/长数字号段），
# 并且**同时扫工作区与全部历史**，避免只扫 HEAD 漏掉历史提交。
#
# 用法：
#   ./scripts/scan-secrets.sh          # 工作区 + 历史
#   ./scripts/scan-secrets.sh --head   # 只扫当前 HEAD
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 通用密钥/凭据模式（POSIX ERE）
PATTERNS=(
  'sk-[A-Za-z0-9]{16,}'                 # OpenAI/DeepSeek 风格 key
  'AKIA[0-9A-Z]{16}'                    # AWS access key
  'ghp_[A-Za-z0-9]{20,}'                # GitHub token
  'xox[baprs]-[A-Za-z0-9-]{10,}'        # Slack token
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'  # 私钥
  'Bearer [A-Za-z0-9._-]{24,}'          # 长 Bearer token
  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.' # JWT
  'pass(word|wd)?\s*[:=]\s*["'"'"'][^"'"'"']{6,}' # 明文口令赋值
)

# 生产标识：真实 QQ 号/手机号/自建域名邮箱不应进仓库（占位符除外）
IDENTIFY=(
  '[1-9][0-9]{8,10}@qq\.com'            # 真实 QQ 邮箱（xxx@qq.com 占位符不会命中）
  '\b1[3-9][0-9]{9}\b'                  # 中国大陆手机号
)

status=0
scan() {
  local label="$1"; shift
  for pat in "${PATTERNS[@]}" "${IDENTIFY[@]}"; do
    local hits
    hits=$(grep -rInE --exclude-dir=.git --exclude='scan-secrets.sh' -- "$pat" . 2>/dev/null | head -20)
    if [ -n "$hits" ]; then
      echo "[FAIL] $label 命中模式：$pat"
      echo "$hits"
      status=1
    fi
  done
}

scan "工作区"

if [ "${1:-}" != "--head" ]; then
  # 扫全部历史（只输出命中的提交与文件，不打印整段 diff）
  for pat in "${PATTERNS[@]}" "${IDENTIFY[@]}"; do
    if git log -p --all 2>/dev/null | grep -qE -- "$pat"; then
      echo "[FAIL] 历史命中模式：$pat"
      status=1
    fi
  done
fi

if [ "$status" -eq 0 ]; then
  if [ "${1:-}" = "--head" ]; then
    echo "脱敏扫描通过：工作区无命中"
  else
    echo "脱敏扫描通过：工作区与全部历史无命中"
  fi
fi
exit "$status"
