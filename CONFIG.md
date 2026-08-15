# 双机部署约定（Pro + Air）

两台 Mac 各自使用独立 relay token；手机端两个 profile 分别保存对应的 host 与 token。

## 固定参数

| 机器 | 隧道名 | Host（手机 profile） |
|---|---|---|
| MacBook Pro（本机） | `herdr-pro` | `https://macbook-pro.kunkunzheten.top` |
| MacBook Air | `herdr-air` | `https://macbook-air.kunkunzheten.top` |

Token：见各机 `~/.config/herdr-remote/secrets.env` 的 `HERDR_RELAY_TOKEN`。该文件由
`install-service.sh` 生成并设为 0600；不要手工复制另一台机器的 token。mono 的设备 profile
会分别恢复两台机器的受管 token / VAPID 配置。

## 安装 / 重启服务（每台机器）

```bash
cd <本机 herdr-remote-phone>/relay
./install-service.sh
```

生产运行由 launchd/systemd 分别托管 relay 与 tunnel；不要用 `nohup start.sh &` 作为守护方式。
`start.sh` 只用于前台调试。macOS 可用以下命令重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.herdr-remote.relay
launchctl kickstart -k gui/$(id -u)/com.herdr-remote.tunnel
curl -fsS http://127.0.0.1:8375/healthz
```

隧道默认使用 HTTP/2，避开代理/fake-IP 环境里容易超时的 QUIC。若系统代理返回
`198.18.*` fake-IP，还需设置 `HERDR_TUNNEL_PROXY=http://127.0.0.1:7890`，因为 launchd
不会自动继承“系统设置”里的代理。需要恢复 QUIC 时设置 `HERDR_TUNNEL_PROTOCOL=quic`
后重装服务。Web Push 的 `HERDR_VAPID_PUBLIC` / `HERDR_VAPID_SUBJECT` 写在
`config.env`，`HERDR_VAPID_PRIVATE` 写在 0600 的 `secrets.env`。

## 鉴权分层（v0.1+ fork）

- 静态页面 / `/api/vapid-public-key`：公开
- WebSocket 连接、事件推送（`?d=`）：必须带 token（`?token=` 或 `Authorization: Bearer`）

## 手机配置

PWA → ⚙ Settings：
- Pro：Host `macbook-pro.kunkunzheten.top`，Token 统一值
- Air：Host `macbook-air.kunkunzheten.top`，Token 统一值
- 推送：Enable Push（VAPID 已配）

## 收件箱 / 通知语义

- blocked（中断）→ 收件箱未处理区 + 推送
- working→idle/done（完成）→ 收件箱已完成区（✅）+ 推送
- pi 内部 subagent 同 pane 运行，天然不触发完成通知
