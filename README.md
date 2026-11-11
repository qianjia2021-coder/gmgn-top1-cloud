# gmgn-top1-cloud

GMGN「热搜·全部链」总榜第一名 → Telegram 群「GMGN热搜」（`-5589667040`）的**云端独立实例**。

## 与本地实例的关系

- 本地（Windows）跑 `top1_bsc_push.py`：网页源优先（5–10 秒级）+ API 源兜底，这是主运行方。
- 本仓库：只跑 API 源（`gmgn-cli market hot-searches`，约 20 秒级），**只在本地心跳过期时接管**。
- 互斥靠 `top1_heartbeat.json`：本地推送器每 60 秒写一次 `ts`，云端每轮先读，`age < HB_STALE(180s)` 就整轮跳过。
  不互斥的话两端会抢同一个 GMGN API 漏桶（权重 3，Free 5/5），并且可能对同一合约各推一次。

## 状态文件

`top1_state.json`（与本地 `top1_seen.json` 同构，双向合并取并集）：

```json
{"pushed": ["<小写地址>"], "updated": 1234, "top1_first_at": {"<小写地址>": 1234}}
```

- `pushed`：已推过的合约，永久只推一次（重回第一也不推）。
- `top1_first_at`：本机/云端首次观测到该合约排第一的时间戳，用于事后回查"什么时候登顶"。

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

验证记录（2026-11-11）：
- 心跳新鲜场景：`[00:52:59] 本机心跳龄 43 秒（阈值 180 秒）` → `本机在线，云端本轮不接管`。
- 接管场景：`榜首 sol 4CxjZyMNoF1p6eXHXHv1TWpS2X47CuUmLwtGe18spump visit=986.0（链块 10 个）` → `已推过，跳过`。
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
全部 token 摊平，再按 `visiting_count` 降序取第一名。旧实现只在每个链块取第一个 token
后互相比较；已知链块内并不保证按该字段降序，所以两种算法可能选出不同合约，脚本会在
结果不一致时输出告警。网页「热搜·全部链」的排序键是否与 `visiting_count` 全局降序
严格等价，现有资料没有给出证明；本仓库沿用本机既有口径，并把等价性列为待验证项。

状态合并说明：`top1_state.json` 的写入采用「取最新远端与 sha，冲突后重算并集」的
乐观锁；本机 `cloud_sync.py` 与推送器都采用「写前重读磁盘取并集 + 原子替换」，两端
在发送消息前还会二次确认去重表。跨网络的「读取—发送—写回」仍然不是一个事务，
切换窗口内的重复推送概率被压缩，但没有被完全消除。

