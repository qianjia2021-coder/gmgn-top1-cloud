# gmgn-top1-cloud

GMGN「热搜·全部链」**前三名** → Telegram 群「GMGN热搜」（`-5589667040`）的**云端独立实例**。

推送规则：前 3 名中，哪个合约还没推过就推哪个，标题写明「第 N 名」。一个合约终身只推一次，
掉出前三再回到前三、或从第 3 名升到第 1 名，都不会再推。

## 与本地实例的关系

- 本地（Windows）跑 `top1_bsc_push.py`：优先用带 `top3` 的 API 源（10 秒级），网页源只存首行、
  没有前三数据，因此仅在 API 源过期时作为「第 1 名单条」兜底，不会被当成前三用。这是主运行方。
- 本仓库：只跑 API 源（`gmgn-cli market hot-searches`，约 20 秒级），**只在本地心跳过期时接管**。
- 互斥靠 `top1_heartbeat.json`：本地推送器每 60 秒写一次 `ts`，云端每轮先读，`age < HB_STALE(120s)` 就整轮跳过。
  不互斥的话两端会抢同一个 GMGN API 漏桶（权重 3，Free 5/5），并且可能对同一合约各推一次。

## 状态文件

`top1_state.json`（与本地 `top1_seen.json` 同构，双向合并取并集）：

```json
{"pushed": ["<小写地址>"], "updated": 1234, "top1_first_at": {"<小写地址>": 1234}}
```

- `pushed`：已推过的合约，永久只推一次（掉出前三再回来也不推）。
- `top1_first_at`：本机/云端首次观测到该合约进入前三的时间戳，用于事后回查"什么时候上榜"。

## Secrets

| 名称 | 说明 |
|---|---|
| `GMGN_API_KEY` | gmgn-cli 的 API Key |
| `TG_API_ID` / `TG_API_HASH` | Telegram 应用凭据 |
| `TG_STRING_SESSION` | **独立登录的第二份用户会话**，不能与本地 `web3_session` 共用同一 auth key |

## 触发

`*/5 * * * *`（GitHub 最小粒度，实际排队常 1–15 分钟）或手动 `workflow_dispatch`。
若要 1 分钟级，外部用 cron-job.org 打 `workflow_dispatch`，空洞可压到 1–3 分钟。

## 已知边界

- 云端接管后的推送延迟由 5–10 秒级降到 20–30 秒级（只有 API 源）。
- 切换空洞 = 心跳过期阈值 + 触发间隔，最差约 3–8 分钟。
- 本机与云端的 `pushed` 靠文件合并，切换前后 1–2 分钟内理论上仍可能重复一次。

## 配置方式（实测）

`gmgn-cli config --apply` 会要求 `~/.config/gmgn/keypair.pem` 存在，且用本地密钥对校验 API Key，
Actions 里既没有密钥对也会被判定不匹配。**正确做法是直接写配置文件**，不执行 `--apply`：

```bash
mkdir -p ~/.config/gmgn
printf 'GMGN_API_KEY=%s\n' "$GMGN_API_KEY" > ~/.config/gmgn/.env
```

只写 `.env` 时 `market hot-searches` 可正常取数（本地隔离目录实测通过），不需要 keypair。

## 调试开关（仅用于验证，日常不要开）

| 环境变量 | 作用 |
|---|---|
| `HB_STALE=0` | 强制云端忽略心跳、走接管路径 |
| `DRY_RUN=1` | 只打印将推送的消息，不实际发送、不写 `pushed` |
| `SELF_TEST=1` | 只发一条自检消息到 `TG_CHAT_ID` 指向的会话，不取数 |

验证记录（2026-11-11，前三规则生效前）：
- 心跳新鲜场景：`[00:52:59] 本机心跳龄 43 秒（阈值 180 秒）` → `本机在线，云端本轮不接管`。
- 接管场景：`榜首 sol 4CxjZyMNoF1p6eXHXHv1TWpS2X47CuUmLwtGe18spump visit=986.0（链块 10 个）` → `已推过，跳过`（该轮日志是旧口径，现在会打出「第 N 名」）。
- 发送链路：`SELF_TEST` 发到私聊，`msg_id=431379`，群未被打扰。

## 本轮改动后的验证方式（2026-11-11）

手动触发 `workflow_dispatch` 时可选用以下输入；定时触发时它们全部取默认值。

| 输入 | 默认值 | 作用 |
|---|---|---|
| `dry_run` | `0` | 设为 `1` 后只打印将推送的消息，不发送、不写 `pushed` |
| `self_test` | `0` | 设为 `1` 后只发一条 Telegram 自检消息，不取数 |
| `diag` | `0` | 设为 `1` 后输出各链块头名与全局前三，供核对取榜口径 |
| `force_takeover` | `0` | 设为 `1` 后把 `HB_STALE` 置为 `0`，强制走接管路径 |

安全的验证组合是 `dry_run=1` + `diag=1` + `force_takeover=1`：该组合会取数并打印
「链块头名」与「全局前三」，但不会发送任何消息，也不会改动 `top1_state.json`。

取榜口径说明：本机 `hot_api_monitor.py` 与本仓库 `cloud_top1.py` 现在都把全部链块的
全部 token 摊平，再按 `visiting_count` 降序取**前 3 名**。旧实现只在每个链块取第一个 token
后互相比较；已知链块内并不保证按该字段降序，所以两种算法可能选出不同合约，脚本会在
结果不一致时输出告警。网页「热搜·全部链」的排序键是否与 `visiting_count` 全局降序
严格等价，现有资料没有给出证明；本仓库沿用本机既有口径，并把等价性列为待验证项。

本机采集端 `hot_api_monitor.py` 在 `trend_top1_api.json` 里写 `top3` 数组（前三名的完整
字段）。推送器有 `top3` 就逐个检查，没有就退化为只处理第一名——所以历史数据不会导致
整轮跳过。网页源 `trend_top1_v2.json` 只有首行、没有前三，无法提供前三判定。

状态合并说明：`top1_state.json` 的写入采用「取最新远端与 sha，冲突后重算并集」的
乐观锁；本机 `cloud_sync.py` 与推送器都采用「写前重读磁盘取并集 + 原子替换」，两端
在发送消息前还会二次确认去重表。跨网络的「读取—发送—写回」仍然不是一个事务，
切换窗口内的重复推送概率被压缩，但没有被完全消除。

## 外部调度器（替代不生效的 GitHub schedule）

### 问题

仓库建立后连续观察约 62 分钟，8 次运行 event **全部是 `workflow_dispatch`**，
`cron: '*/5 * * * *'` 一次都没有自行触发。workflow `state=active`、默认分支 `main`、
`pushed_at` 正常，因此不是配置错误，而是 GitHub 对新建/低活动仓库的 schedule 延迟与丢弃。
后果：本机关机后云端不会自行醒来，「关机接管」在该问题修复前不成立。

### 过渡方案：云主机常驻定时器（已停用，见下一节现用方案）

部署位置 `/home/user/gmgn_top1_cron/`，脚本 `cron.py`，凭据 `token`（600 权限）。

每 120 秒执行一次：

1. 读 `top1_heartbeat.json`，心跳龄 `< 180` 秒 → 本机在线，跳过，不打 dispatch
2. 心跳龄 `>= 180` 秒 → 判定本机离线，POST `workflow_dispatch`
3. 心跳读不到 → 按离线处理并触发（宁可多跑一轮，也不让关机后永远不醒）

只在本机离线时才消耗 Actions 运行；本机在线时每 2 分钟仅一次 GitHub Contents 读取。

实测记录（2026-11-11）：

```
09:37:10 调度器启动 | 检查间隔 120s | 心跳阈值 180s | 进程 1228
09:37:11 本机在线，跳过（心跳龄 16s，累积跳过 1 次）
```

受控测试（临时目录、`HB_STALE=0`，验证触发分支）：

```
09:37:32 本机心跳龄 37s 超过阈值 0s，判定离线，触发云端接管
09:37:33 触发成功 | heartbeat-37s
```

该次触发产生 run `42940309005`，云端日志为：

```
[01:37:51] 本机心跳龄 56 秒（阈值 180 秒）
[01:37:51] 本机在线，云端本轮不接管
```

即触发链路打通、且没有误发消息，互斥逻辑仍然生效。

进程存活校验：`cat /home/user/gmgn_top1_cron/cron.pid`，日志 `/home/user/gmgn_top1_cron/cron.log`。
启动命令：

```bash
cd /home/user/gmgn_top1_cron && setsid nohup python3 cron.py >/dev/null 2>&1 < /dev/null &
```

已知限制：云主机的 cron 守护进程无权限启动，因此该进程靠 `nohup` 常驻，**宿主机重启后需要重新执行上面的启动命令**；日志与 pid 文件会保留，便于判断是否掉线。

### 现用方案：Cloudflare Worker（已部署并实测）

云主机进程只能在沙箱存活期内有效（沙箱重启后 cron 守护起不来、`nohup` 进程不会自恢复），
因此正式调度器改为 Cloudflare Worker，它不依赖任何常驻机器。

部署记录（2026-11-11）：

| 项 | 值 |
|---|---|
| Worker 名 | `gmgn-top1-cron` |
| 生产地址 | `https://gmgn-top1-cron.qianjia2021.workers.dev` |
| 定时 | `*/2 * * * *`（每 2 分钟） |
| Version ID | `8554df79-9bdf-41cb-8708-24f8654162c8` |
| 环境变量 | `HB_STALE=120` |
| 密钥 | `GH_TOKEN`（GitHub PAT，仅存于 Worker secret，不落仓库文件） |
| 账号 | 与历史的 `onchain-alpha-cron` 同账号，但 Worker 与 secret 完全独立 |

行为验证：

```
GET https://gmgn-top1-cron.qianjia2021.workers.dev
{"ok":true,"repo":"qianjia2021-coder/gmgn-top1-cloud","workflow_id":414008272,
 "heartbeat_age_sec":8,"stale_threshold_sec":180,"would_dispatch":false}
```

本机在线时 `would_dispatch=false`，即不消耗 Actions。

强制触发验证（`?force=1`）：

```
GET https://gmgn-top1-cron.qianjia2021.workers.dev?force=1
{"dispatched":true,"heartbeat_age":17}
```

产生 run `42943428664`（`workflow_dispatch`，success），该轮云端日志：

```
[02:04:37] 本机心跳龄 33 秒（阈值 180 秒）
[02:04:37] 本机在线，云端本轮不接管
```

即「Worker 定时器真正触发 → 云端收到并执行 → 因本机心跳新鲜而正确跳过 → 未发消息」四件事同时成立。

### 真接管验证（2026-11-11 10:48–10:53）

`force=1` 只证明「能触发」，不能证明「关机后真的会接」。为此做了一次断心跳实测：
停掉本机 `cloud_sync.py`（心跳停写），推送器继续跑，观察 Worker 是否自主触发、云端是否真的接管。

时间线：

| 时刻（本地） | 事件 |
|---|---|
| 10:48:04 | 停掉 `cloud_sync`，远端心跳不再更新（最后一次写于 10:47:14） |
| 10:52:04 | Worker 自主触发（无人工干预），run `42948902734` |
| 10:52:19 | 云端判定离线并接管 |

该轮云端日志：

```
[02:52:19] 本机心跳龄 305 秒（阈值 180 秒）
[02:52:19] 榜首：sol 4CxjZyMNoF1p6eXHXHv1TWpS2X47CuUmLwtGe18spump visit=1040.0（链块 10 个）
[02:52:20] 已推过（推送前复核），跳过：sol 4CxjZyMNoF1p6e...pump
```

判定离线、取到真实榜单、算出全局前三、逐条去重复核后跳过，四件事在真接管路径上全部走通，
且没有重发已推过的合约（该合约 08:00 已推）。该轮日志是前三规则上线前录下的，现在同样的
路径会打出 `第 1 名：...`、`第 2 名：...`、`第 3 名：...`，逐个判断是否需要推送。

**实测空洞 4 分 15 秒**，由三段叠加造成：最后一次成功心跳在停写前 50 秒 + 阈值 180 秒 + Worker 2 分钟节拍错过一个窗口。

### 阈值调优（180s → 120s）

以上实测暴露空洞偏长，故两端阈值统一改为 120 秒：

| 位置 | 改动 |
|---|---|
| `worker/wrangler.toml` | `HB_STALE = "120"`，重新部署后 Version ID `c47d86cf-8584-465b-9906-813d13e676f9` |
| `cloud_top1.py` | `HB_STALE` 默认值 120（与 Worker 端保持一致，避免 Worker 触发后云端因阈值更高而误判在线又跳过） |

调整后理论空洞：最长约 `60(心跳间隔) + 120(阈值) + 120(Worker 节拍) ≈ 4分钟`，典型 2.5–3.5 分钟。
代价：本机网络抖动导致心跳晚写超过 2 分钟时，Worker 会多触发一轮，云端看到新鲜心跳即跳过，
只消耗一次 Actions 配额，不会误发消息。

维护命令：

```powershell
cd top1_cloud\worker
wrangler deploy                 # 改 worker.js 后重新部署
wrangler secret put GH_TOKEN    # 换 PAT
wrangler tail                   # 实时看 cron 日志
```

### 云主机定时器（已停用，仅留档）

`/home/user/gmgn_top1_cron/` 内的 `cron.py` 与 `token` 仍保留，进程已于 2026-11-11 10:05 停止，
`crontab` 中的自愈条目也已清除，避免与 Worker 每 2 分钟同时触发造成 Actions 队列翻倍。
需要临时启用时：

```bash
cd /home/user/gmgn_top1_cron && setsid nohup python3 cron.py >/dev/null 2>&1 < /dev/null &
```

### 并发与节拍

`cloud.yml` 已有 `concurrency: group: top1-cloud / cancel-in-progress: false`，因此每 2 分钟的触发不会并发执行，
只会串行排队；单轮约 60–90 秒，关机期间的接管间隔实际为 2–3 分钟。

