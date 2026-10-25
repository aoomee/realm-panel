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

---

## 初始化规则

面板管理已有规则，不负责创建。首次使用需用原始脚本生成规则：

```bash
git clone https://github.com/zywe03/realm-xwPF
bash realm-xwPF/xwPF.sh
```

规则保存在 `/etc/realm/rules/rule-*.conf`，面板会自动读取。

---

## 功能页面

| 页面 | 功能 |
|---|---|
| 概览 | 规则数、VPS 数、realm 运行状态 |
| 转发规则 | 查看/启用/切换负载均衡与故障转移 |
| VPS 管理 | 填写到期日期、流量配额 |
| 权重引擎 | 预览权重评分，手动触发重算 |

---

## 动态权重

每 60 秒自动执行：

```
score = W_DAYS × (剩余天数 / MAX_DAYS) + W_TRAFFIC × (剩余流量 / 总流量)
```

- 已到期或流量耗尽的节点评分为 0，流量自动切走
- 权重写回规则文件后热重载 realm（不中断连接）

可在 `docker-compose.yml` 的 `environment` 中调整：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEIGHT_INTERVAL` | `60` | 重算间隔（秒） |
| `WEIGHT_MAX_DAYS` | `30` | 满分对应的剩余天数 |
| `WEIGHT_W_DAYS` | `0.5` | 到期时间权重 |
| `WEIGHT_W_TRAFFIC` | `0.5` | 流量权重 |

---

## 数据持久化

所有数据存在 `realm_data` 命名卷中，删除容器不会丢失规则。  
彻底清除：`docker volume rm realm_data`

## 更新

```bash
docker compose pull
docker compose up -d
```
