# realm-panel

基于 Docker 的 [Realm](https://github.com/zhboner/realm) 转发规则管理面板，支持根据 VPS 到期时间和剩余流量动态调整负载均衡权重。

---

## 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
```

## 安装 Docker Compose

```bash
# Docker Desktop 已内置；纯 Linux 服务器执行：
apt-get install -y docker-compose-plugin   # Debian/Ubuntu
# 或
yum install -y docker-compose-plugin       # CentOS/RHEL
```

验证：

```bash
docker -v && docker compose version
```

---

## 快速部署

```bash
git clone https://github.com/aoomee/realm-panel
cd realm-panel
docker compose up -d
```

浏览器打开 `http://你的服务器IP:5000`

> 如需修改端口，编辑 `docker-compose.yml` 中 `ports` 字段再启动。

也可以不克隆仓库，新建一个目录后手动创建以下两个文件：

**`docker-compose.yml`**

```yaml
services:
  realm:
    build: ./realm-container
    container_name: realm
    restart: unless-stopped
    network_mode: host
    volumes:
      - realm_data:/etc/realm
    command: ["-c", "/etc/realm/config.json"]

  panel:
    build:
      context: .
      dockerfile: realm-panel/Dockerfile
    container_name: realm-panel
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - realm_data:/etc/realm
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - RULES_DIR=/etc/realm/rules
      - META_PATH=/etc/realm/panel/vps_meta.json
      - CONFIG_JSON=/etc/realm/config.json
      - REALM_BACKEND=docker
      - REALM_CONTAINER=realm
      - WEIGHT_INTERVAL=60
      - WEIGHT_MAX_DAYS=30
      - WEIGHT_W_DAYS=0.5
      - WEIGHT_W_TRAFFIC=0.5
      - PANEL_PASSWORD=          # 强烈建议设置！面板登录密码（用户名 admin）
      - PANEL_PUBLIC_HOST=       # A 机公网 IP/域名：本机做中转时的入口地址
    depends_on:
      - realm

volumes:
  realm_data:
```

推荐直接克隆仓库部署，所有文件已包含：

```bash
git clone https://github.com/aoomee/realm-panel
cd realm-panel
docker compose up -d
```

---

---

## 功能页面

| 页面 | 功能 |
|---|---|
| 概览 | 规则数、VPS 数、realm 运行状态 |
| 节点管理 | 添加中转/落地机、一键生成部署命令、批量导入、健康状态 |
| 线路部署 | 选中转+落地，粘贴节点链接，一键下发整条线路并返回新链接 |
| 转发规则 | 查看/启用/切换负载均衡与故障转移（可指定所属节点） |
| VPS 管理 | 填写到期日期、流量配额 |
| 权重引擎 | 预览权重评分，手动触发重算 |
| 链路监控 | 节点可达性探测、延迟历史、规则自检 |

---

## 链路监控

每 30 秒自动 TCP 探测所有规则中的 `REMOTE_HOST:REMOTE_PORT`，记录延迟历史（最近 20 次）与在线率。

「规则自检」对每条启用规则执行 4 步诊断：

| 步骤 | 说明 |
|---|---|
| realm 进程 | 容器/服务是否 running |
| 配置含该端点 | config.json 中是否有该监听端口 |
| 远端可达 | TCP 连通目标 Host:Port |
| 本地端口已绑定 | 面板所在主机上该端口是否处于 LISTEN 状态 |

可在 `docker-compose.yml` 的 `environment` 中调整：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MONITOR_INTERVAL` | `30` | 探测间隔（秒） |
| `MONITOR_TIMEOUT` | `3` | 单次 TCP 超时（秒） |
| `MONITOR_RING` | `20` | 每节点延迟历史长度 |

---

## 动态权重

每 60 秒自动执行：

```
score = W_DAYS × (剩余天数 / MAX_DAYS) + W_TRAFFIC × (剩余流量 / 总流量)
```

- 已到期或流量耗尽的节点评分为 0，流量自动切走
- 权重变化时重写规则文件并下发；realm 无热重载，仅当配置确实变化时才重启对应节点的 realm

可在 `docker-compose.yml` 的 `environment` 中调整：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEIGHT_INTERVAL` | `60` | 重算间隔（秒） |
| `WEIGHT_MAX_DAYS` | `30` | 满分对应的剩余天数 |
| `WEIGHT_W_DAYS` | `0.5` | 到期时间权重 |
| `WEIGHT_W_TRAFFIC` | `0.5` | 流量权重 |

---

## 节点管理 & 线路部署（管理 12 台中转/落地机）

面板跑在 A 机，其余每台机器通过**面板生成的一键命令**安装 realm + 轻量 Agent。之后在面板上点几下，就能在中转机与落地机之间部署一条转发线路，并得到改好入口地址的客户端链接。

### 第一步：在面板添加节点

「节点管理」→「添加节点」或「批量导入」，只需填**名称 + 公网 IP**（批量导入格式：`名称 角色 IP [Agent端口]`，角色填 `中转` 或 `落地`）。Token 由面板自动生成。

### 第二步：复制部署命令到各机器

点节点行的「部署命令」（或「全部部署命令」一次全量复制）。SSH 登录对应机器，**root** 下粘贴执行：

```bash
curl -fsSL http://<A机IP>:5000/agent/install.sh | PANEL=http://<A机IP>:5000 TOKEN=<自动生成的token> PORT=5001 bash
```

脚本会自动：装依赖 → 下载 realm（优先从面板下载，失败回退 GitHub，可配 `GH_PROXY` 加速）→ 写入 realm 服务 → 安装并启动 Agent。**重复执行即升级。**

回到面板「节点管理」点「刷新列表」，状态变「在线」即可。

### 第三步：一键建线路

「线路部署」→「新建线路」：选中转机 + 落地机（多选 = 负载均衡），粘贴落地机上的节点链接（vless/vmess/trojan/ss/hy2/tuic…），点「一键部署」。面板自动：

1. 分配中转监听端口（和链式落地端口）
2. 生成两端 realm 规则并下发（realm 无热重载，会瞬时重启该节点的 realm 来应用新配置）
3. 返回改好入口地址的**新节点链接**，直接导入客户端

- **直连模式**：中转 → 落地服务端口（落地无需 realm 规则）
- **链式模式**：中转 → 落地 realm → 落地本机服务（落地服务端口不必公开，默认 127.0.0.1）

多落地时权重随「VPS 资产」里按落地 IP 填写的到期时间/剩余流量自动调整。每条线路都能「测试」逐跳连通性、「重新下发」或「删除」。

### 防火墙

节点需放行：Agent 端口（建议仅允许 A 机 IP）+ 线路端口（默认 20000-59999 的 TCP/UDP）。云厂商安全组同理：

```bash
ufw allow from <A机IP> to any port 5001 proto tcp
ufw allow 20000:59999/tcp && ufw allow 20000:59999/udp
```

### 面板鉴权（强烈建议）

公网部署务必在 `docker-compose.yml` 里设置 `PANEL_PASSWORD`（用户名 `admin`）。`/agent/*` 安装文件保持公开（节点部署命令需要它），其余页面与 API 需登录。

### 手动安装（不想用一键命令时）

```bash
# 1. 装 realm 二进制到 /usr/local/bin/realm，写 realm.service（或用 Docker）
# 2. 复制 agent.py
cp realm-agent/agent.py /usr/local/bin/realm-agent && chmod +x /usr/local/bin/realm-agent
# 3. 写 token
mkdir -p /etc/realm && echo 'AGENT_TOKEN=你的token' > /etc/realm/agent.env && chmod 600 /etc/realm/agent.env
# 4. 装 systemd 服务
cp realm-agent/realm-agent.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now realm-agent
```

### 新增环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD` | 空 | 面板登录密码（用户名 admin），留空 = 不鉴权 |
| `PANEL_PUBLIC_HOST` | 空 | A 机公网 IP/域名（本机做中转时的入口地址） |
| `LINES_PATH` | `/etc/realm/panel/lines.json` | 线路存储 |
| `LINE_PORT_MIN` / `LINE_PORT_MAX` | `20000` / `59999` | 线路自动分配端口范围 |

---

## 数据持久化

所有数据存在 `realm_data` 命名卷中，删除容器不会丢失规则。  
彻底清除：`docker volume rm realm_data`

## 更新

```bash
git pull
docker compose up -d --build
```