#!/usr/bin/env bash
# realm-panel 节点一键部署脚本（中转机 / 落地机通用）
#
# 面板「节点管理 → 部署命令」会生成完整命令，形如：
#   curl -fsSL http://A机IP:5000/agent/install.sh | PANEL=http://A机IP:5000 TOKEN=xxxx PORT=5001 bash
#
# 做的事：
#   1. 安装依赖（curl / tar / python3）
#   2. 安装 realm 二进制（优先从面板下载，失败回退 GitHub，可设 GH_PROXY 加速）
#   3. 写入 realm.service（已存在则保留）和空的 /etc/realm/config.json
#   4. 安装 realm-agent 并以 systemd 服务启动（重复执行 = 升级）
#
# 可选环境变量：
#   GH_PROXY=https://ghproxy.example.com/   GitHub 下载加速前缀
#   REALM_VERSION=v2.9.6                     realm 版本
#   REINSTALL_REALM=1                        强制重新安装 realm 二进制
set -euo pipefail

PANEL="${PANEL:-}"
PANEL="${PANEL%/}"
TOKEN="${TOKEN:-}"
PORT="${PORT:-5001}"
GH_PROXY="${GH_PROXY:-}"
REALM_VERSION="${REALM_VERSION:-v2.9.6}"
REALM_CONFIG="/etc/realm/config.json"
REALM_SERVICE="realm"
REPO_RAW="https://raw.githubusercontent.com/aoomee/realm-panel/main/realm-agent"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
step()   { echo; yellow "==> $*"; }

[[ $EUID -eq 0 ]] || { red "请用 root 执行（sudo -i 后再粘贴命令）"; exit 1; }
[[ -n "$TOKEN" ]] || { red "缺少 TOKEN，请从面板「节点管理 → 部署命令」复制完整命令"; exit 1; }
command -v systemctl >/dev/null || { red "需要 systemd"; exit 1; }

# ---------------------------------------------------------------- deps
step "检查依赖"
need=()
for b in curl tar python3; do command -v "$b" >/dev/null || need+=("$b"); done
if ((${#need[@]})); then
  echo "安装: ${need[*]}"
  if command -v apt-get >/dev/null; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
  elif command -v dnf >/dev/null; then dnf install -y -q "${need[@]}"
  elif command -v yum >/dev/null; then yum install -y -q "${need[@]}"
  elif command -v apk >/dev/null; then apk add --no-cache "${need[@]}"
  else red "无法自动安装 ${need[*]}，请手动安装后重试"; exit 1; fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || { red "需要 Python 3.8+"; exit 1; }

# fetch <panel-path> <github-url> <dest>
fetch() {
  if [[ -n "$PANEL" ]] && curl -fsSL --max-time 60 "${PANEL}$1" -o "$3" 2>/dev/null; then
    return 0
  fi
  echo "  面板下载失败，回退 GitHub: ${GH_PROXY}$2"
  curl -fsSL --max-time 120 "${GH_PROXY}$2" -o "$3"
}

# ---------------------------------------------------------------- realm
step "安装 realm ${REALM_VERSION}"
case "$(uname -m)" in
  x86_64|amd64)   TRIPLE="x86_64-unknown-linux-musl" ;;
  aarch64|arm64)  TRIPLE="aarch64-unknown-linux-musl" ;;
  armv7l|armv7)   TRIPLE="armv7-unknown-linux-musleabihf" ;;
  *) red "不支持的架构: $(uname -m)"; exit 1 ;;
esac
if command -v realm >/dev/null && [[ "${REINSTALL_REALM:-0}" != "1" ]]; then
  echo "已安装: $(command -v realm) ($(realm --version 2>/dev/null | head -1 || echo '?'))"
else
  TMP="$(mktemp -d)"
  fetch "/agent/realm/realm-${TRIPLE}.tar.gz" \
        "https://github.com/zhboner/realm/releases/download/${REALM_VERSION}/realm-${TRIPLE}.tar.gz" \
        "$TMP/realm.tar.gz"
  tar -xzf "$TMP/realm.tar.gz" -C "$TMP"
  install -m 0755 "$TMP/realm" /usr/local/bin/realm
  rm -rf "$TMP"
  green "✓ realm 安装到 /usr/local/bin/realm"
fi
REALM_BIN="$(command -v realm)"

mkdir -p /etc/realm
[[ -f "$REALM_CONFIG" ]] || echo '{"network":{"no_tcp":false,"use_udp":true},"endpoints":[]}' > "$REALM_CONFIG"

if [[ -f /etc/systemd/system/realm.service || -f /lib/systemd/system/realm.service ]]; then
  yellow "已存在 realm.service，保留原文件（之后由面板接管 ${REALM_CONFIG}）"
else
  cat > /etc/systemd/system/realm.service <<EOF
[Unit]
Description=realm port forwarder (managed by realm-panel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${REALM_BIN} -c ${REALM_CONFIG}
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
  green "✓ 写入 realm.service"
fi

# ---------------------------------------------------------------- agent
step "安装 realm-agent"
fetch "/agent/agent.py" "${REPO_RAW}/agent.py" /usr/local/bin/realm-agent.new
python3 -c "import ast,sys; ast.parse(open('/usr/local/bin/realm-agent.new').read())" \
  || { red "agent.py 下载内容异常"; exit 1; }
mv /usr/local/bin/realm-agent.new /usr/local/bin/realm-agent
chmod 0755 /usr/local/bin/realm-agent

umask 077
cat > /etc/realm/agent.env <<EOF
AGENT_TOKEN=${TOKEN}
AGENT_PORT=${PORT}
REALM_CONFIG=${REALM_CONFIG}
REALM_SERVICE=${REALM_SERVICE}
EOF
umask 022

cat > /etc/systemd/system/realm-agent.service <<EOF
[Unit]
Description=realm-panel agent (receives config from panel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/realm/agent.env
ExecStart=$(command -v python3) /usr/local/bin/realm-agent
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable realm-agent >/dev/null 2>&1
systemctl restart realm-agent
sleep 1
if ! systemctl is-active --quiet realm-agent; then
  red "✗ realm-agent 启动失败："
  journalctl -u realm-agent --no-pager -n 20 || true
  exit 1
fi
green "✓ realm-agent 运行中，端口 ${PORT}"

if curl -fsS --max-time 3 -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/health" >/dev/null; then
  green "✓ 本地自检通过"
fi

# ---------------------------------------------------------------- firewall hint
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  yellow "检测到 ufw 已启用，请放行 Agent 端口及线路端口，例如："
  echo "  ufw allow from <A机IP> to any port ${PORT} proto tcp"
  echo "  ufw allow 20000:59999/tcp && ufw allow 20000:59999/udp"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  yellow "检测到 firewalld 已启用，请放行 Agent 端口 ${PORT} 及线路端口（20000-59999）"
fi

echo
green "✓ 部署完成。回到面板「节点管理」点「刷新列表」，状态显示在线即可创建线路。"
