#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 热搜·全部链 前三名 -> Telegram 群（云端独立实例）

与本机 top1_bsc_push.py 同一业务口径，但只跑 API 源（gmgn-cli market hot-searches）：
- 一次请求返回多个链块；把所有链块的全部 token 摊平，按 visiting_count 降序取前 3 名。
  这与本机 hot_api_monitor.py 的 fetch() 完全一致，消除了两端「取榜算法不同」的差异。
- 前 3 名逐个检查：哪个还没推过就推哪个；一轮最多推 3 条，同一条不会重复。
- 一个合约永久只推一次（pushed 集合，与本地 top1_seen.json 同构、双向合并）。
  掉出前三再回来也不再推。
- 记录「首次进入前三」的时间戳（top1_first_at）。
- 每次运行前先读本机心跳，本机在线（心跳新鲜）则整轮跳过。

口径边界（必须如实说明）：
  现有资料只记录了两次「API 首位 = 网页『热搜·全部链』首行」的实测对照，
  并且明确记下网页热度数值与 visiting_count 不是同一统计。因此
  「按 visiting_count 全局降序」是否严格等价于网页「全部链」的排序筛选，
  目前没有证明；本脚本按本机既有口径执行，并在两种算法结果不一致时打告警。

Env:
  GITHUB_TOKEN        Actions 自动提供，读写仓库内状态文件
  REPO                仓库全名（默认 qianjia2021-coder/gmgn-top1-cloud）
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION
                      独立登录的第二份用户会话（与本机 web3_session 不同 auth key）
  TG_CHAT_ID          目标群，默认 -5589667040（GMGN热搜）
  HB_STALE            本机心跳过期阈值（秒），默认 120（与 Worker 端一致）
  LIMIT               每链取多少条，默认 30（与本机采集端一致）
  DRY_RUN             只打印将推送的消息，不发送、不写状态
  SELF_TEST           只发一条自检消息，不取数
  TOP1_DIAG           输出链块头名与全局前三，供验证轮核对口径

本轮优化：
  - 取榜从「每链块取头名再比较」改为「全局摊平降序」，与本机采集端对齐；
    旧口径保留为对照，结果不一致时告警（链块内并不保证按 visiting_count 降序）。
  - 增加「有效链 >= 2」硬闸，与本机 chainSet >= 2 的判定对齐。
  - 心跳读取失败或时间戳来自未来时，本轮不接管，避免 GitHub 抖动导致误推送。
  - 发送前再次读取云端状态二次确认，压缩切换窗口内的重复推送概率。
  - 状态写入改为「GET 最新 -> 并集 -> 带 sha PUT」的乐观锁；409 时重算并集，
    不再用旧 payload 覆盖 cloud_sync 刚合并进来的地址。
  - 消息已发出但状态未落盘时，明确报严重错误并给出人工补写地址。
"""
import asyncio
import base64
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

REPO = os.environ.get("REPO", "qianjia2021-coder/gmgn-top1-cloud")
STATE_PATH = "top1_state.json"
HB_PATH = "top1_heartbeat.json"
TG_CHAT = int(os.environ.get("TG_CHAT_ID", "-5589667040"))
HB_STALE = int(os.environ.get("HB_STALE", "120"))
HB_FUTURE_TOLERANCE = 300
LIMIT = int(os.environ.get("LIMIT", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")
SELF_TEST = os.environ.get("SELF_TEST", "") == "1"
TOP1_DIAG = os.environ.get("TOP1_DIAG", "") in ("1", "true", "True")
GH_API = "https://api.github.com"

CHAIN_RE = re.compile(r"^[a-z0-9]{2,16}$")
ADDR_RE = re.compile(r"^(?:0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$")


def log(msg):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- GitHub 状态文件
def gh_headers():
    return {
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "gmgn-top1-cloud",
    }


def read_remote(path):
    r = requests.get("{}/repos/{}/contents/{}".format(GH_API, REPO, path),
                     headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]


def merged_state(remote, pushed_extra=(), first_extra=None):
    """基于「此刻的最新远端」生成并集状态；不丢任何一端新增的地址。"""
    rp = {str(x).strip().lower() for x in (remote.get("pushed") or [])
          if isinstance(x, str) and x.strip()}
    rf = {}
    raw_first = remote.get("top1_first_at") or {}
    if isinstance(raw_first, dict):
        for k, v in raw_first.items():
            if isinstance(k, str) and k.strip():
                try:
                    rf[k.strip().lower()] = int(v)
                except (TypeError, ValueError):
                    continue
    rp |= {str(x).strip().lower() for x in pushed_extra if str(x).strip()}
    for k, v in (first_extra or {}).items():
        kl = str(k).strip().lower()
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if kl and (kl not in rf or iv < rf[kl]):
            rf[kl] = iv
    try:
        old_updated = int(remote.get("updated") or 0)
    except (TypeError, ValueError):
        old_updated = 0
    return {"pushed": sorted(rp),
            "updated": max(old_updated, int(time.time())),
            "top1_first_at": {k: rf[k] for k in sorted(rf)}}


def write_remote_merged(path, merge, msg, max_attempts=3):
    """乐观锁写入：每轮先取最新内容与 sha，用 merge(remote) 重新生成内容再 PUT。"""
    for attempt in range(1, max_attempts + 1):
        try:
            remote, sha = read_remote(path)
        except Exception as e:
            log("状态读取失败({}/{})：{}；本轮放弃写入".format(
                attempt, max_attempts, type(e).__name__))
            return None
        obj = merge(remote if isinstance(remote, dict) else {})
        payload = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        body = {"message": msg, "content": base64.b64encode(payload).decode("utf-8")}
        if sha:
            body["sha"] = sha
        try:
            r = requests.put("{}/repos/{}/contents/{}".format(GH_API, REPO, path),
                             headers=gh_headers(), json=body, timeout=25)
        except Exception as e:
            log("状态写入网络失败({}/{})：{}".format(attempt, max_attempts, type(e).__name__))
            time.sleep(3)
            continue
        if r.status_code in (200, 201):
            return r.json()["content"]["sha"]
        if r.status_code in (409, 422):
            log("状态写入冲突({}/{}) HTTP {}；重取并集后重试".format(
                attempt, max_attempts, r.status_code))
            time.sleep(1)
            continue
        log("状态写入失败 HTTP {} {}".format(r.status_code, r.text[:200]))
        return None
    log("状态写入连续 {} 次未成功".format(max_attempts))
    return None


def local_online():
    """返回 True（本机在线）/ False（可接管）/ None（心跳不可信，本轮不接管）。"""
    try:
        hb, _ = read_remote(HB_PATH)
    except Exception as e:
        log("心跳读取失败，本轮不接管：{}".format(type(e).__name__))
        return None
    if not isinstance(hb, dict):
        return None
    try:
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    age = time.time() - ts
    if age < -HB_FUTURE_TOLERANCE:
        log("心跳时间戳来自未来（偏差 {:.0f} 秒），疑似时钟不同步，本轮不接管".format(-age))
        return None
    log("本机心跳龄 {:.0f} 秒（阈值 {} 秒）".format(age, HB_STALE))
    return age < HB_STALE


# ---------------------------------------------------------------- 榜单
def _visiting(t):
    try:
        return float(t.get("visiting_count") or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_top1():
    """与本机 hot_api_monitor.py 同口径：摊平全部 token，按 visiting_count 降序取第一。"""
    args = ["gmgn-cli", "market", "hot-searches",
            "--interval", "1h", "--limit", str(LIMIT), "--raw"]
    out = None
    for attempt in (1, 2):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=180)
        except Exception as e:
            raise RuntimeError("gmgn-cli 调用异常：{} {}".format(type(e).__name__, str(e)[:120]))
        if p.returncode == 0:
            out = p.stdout
            break
        err = (p.stderr or p.stdout or "")[:300]
        if ("429" in err or "RATE_LIMIT" in err) and attempt == 1:
            log("命中限流，等待 35 秒重试")
            time.sleep(35)
            continue
        raise RuntimeError("gmgn-cli 失败 rc={} {}".format(p.returncode, err))
    if out is None:
        raise RuntimeError("gmgn-cli 两次调用均失败")
    try:
        blocks = json.loads(out)
    except Exception as e:
        raise RuntimeError("返回不是合法 JSON：{}".format(e))
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeError("返回结构异常：期望非空链块数组")

    flat, heads, chains = [], [], set()
    for b in blocks:
        if not isinstance(b, dict):
            continue
        chain = str(b.get("chain") or "").strip().lower()
        toks = b.get("tokens") or []
        if not isinstance(toks, list):
            continue
        if chain:
            chains.add(chain)
        for i, t in enumerate(toks):
            if not isinstance(t, dict):
                continue
            flat.append({"chain": chain, "token": t,
                         "visiting_count": _visiting(t), "block_rank": i})
        if toks and isinstance(toks[0], dict):
            heads.append({"chain": chain, "token": toks[0],
                          "visiting_count": _visiting(toks[0])})
    if not flat:
        raise RuntimeError("榜单里没有任何 token")
    if len(chains) < 2:
        raise RuntimeError("有效链只有 {} 条，不构成「全部链」混排榜，拒绝推送".format(len(chains)))

    flat.sort(key=lambda x: x["visiting_count"], reverse=True)
    best = flat[0]

    # 旧口径对照：链块内并不保证按 visiting_count 降序，两者可能选出不同合约。
    if heads:
        heads.sort(key=lambda x: x["visiting_count"], reverse=True)
        addr_new = str(best["token"].get("address") or "").lower()
        addr_old = str(heads[0]["token"].get("address") or "").lower()
        if addr_new != addr_old:
            log("口径告警：全局扁平榜首 {} 与链块头名榜首 {} 不一致".format(
                addr_new[:14], addr_old[:14]))
    if TOP1_DIAG:
        log("诊断：链块 {} 条，有效链 {} 条，token {} 个".format(
            len(blocks), len(chains), len(flat)))
        for h in heads[:10]:
            log("  链块头名 {:<12} visit={:<8} addr={}".format(
                h["chain"], h["visiting_count"], str(h["token"].get("address") or "")[:14]))
        for x in flat[:3]:
            log("  全局前三 {:<12} visit={:<8} addr={}".format(
                x["chain"], x["visiting_count"], str(x["token"].get("address") or "")[:14]))
    return flat[:3], len(blocks)


# ---------------------------------------------------------------- 消息
def fmt_money(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    if not (-1e18 < x < 1e18) or x <= 0:
        return "-"
    if x >= 1e9:
        return "${:.2f}B".format(x / 1e9)
    if x >= 1e6:
        return "${:.2f}M".format(x / 1e6)
    if x >= 1e3:
        return "${:.1f}K".format(x / 1e3)
    return "${:.0f}".format(x)


def fmt_pct(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    if not (-1e9 < x < 1e9):
        return "-"
    return "{:+.1f}%".format(x) if abs(x) < 1000 else "{:+.0f}%".format(x)


def info_from_token(t):
    one_h = t.get("price_change_percent1h")
    if one_h is not None:
        chg_val, chg_label = one_h, "1h涨幅"
    else:
        # 统计区间无法从现有返回确认，沿用中性标签。
        chg_val, chg_label = t.get("price_change_percent"), "涨幅"
    sym = str(t.get("symbol") or "").strip()
    nm = str(t.get("name") or "").strip()
    display = "{}  {}".format(sym, nm) if sym and nm and nm.lower() != sym.lower() else (sym or nm or "-")
    return {
        "name": display,
        "hot": str(t.get("visiting_count") if t.get("visiting_count") is not None else "-"),
        "mcap": fmt_money(t.get("market_cap")),
        "chg": fmt_pct(chg_val),
        "chg_label": chg_label,
        "liquidity": fmt_money(t.get("liquidity")),
        "vol1h": fmt_money(t.get("volume")),
    }


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def build_message(chain, address, info, rank=1):
    url = "https://gmgn.ai/{}/token/{}".format(
        urllib.parse.quote(chain, safe=""), urllib.parse.quote(address, safe=""))
    lines = [
        "🔥 <b>热搜·全部链 第 {} 名</b>".format(rank),
        "",
        "名称：<b>{}</b>".format(esc(info.get("name") or "-")),
        "链：<b>{}</b>".format(esc(chain.upper())),
        "热度：<b>{}</b>".format(esc(info.get("hot") or "-")),
        "市值：{}　{}：{}".format(esc(info.get("mcap") or "-"),
                                esc(info.get("chg_label") or "涨幅"),
                                esc(info.get("chg") or "-")),
        "池子：{}　成交额：{}".format(esc(info.get("liquidity") or "-"),
                                 esc(info.get("vol1h") or "-")),
        "",
        "合约：<code>{}</code>".format(esc(address)),
        "查看：{}".format(esc(url)),
        "时间：{}".format(time.strftime("%Y-%m-%d %H:%M:%S")),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 发送
async def send_message(msg):
    client = TelegramClient(StringSession(os.environ["TG_STRING_SESSION"]),
                            int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("TG 会话未授权（StringSession 无效或已吊销）")
        entity = await client.get_entity(TG_CHAT)
        sent = await client.send_message(entity, msg, parse_mode="html", link_preview=False)
        return sent.id
    finally:
        await client.disconnect()


# ---------------------------------------------------------------- 主流程
def load_state():
    try:
        data, sha = read_remote(STATE_PATH)
    except Exception as e:
        log("状态读取失败，按空状态处理（本轮只做首次记录，不推送）：{}".format(type(e).__name__))
        return None, None
    if not isinstance(data, dict):
        return {}, sha
    pushed = {str(x).strip().lower() for x in (data.get("pushed") or [])
              if isinstance(x, str) and x.strip()}
    first = {}
    for k, v in (data.get("top1_first_at") or {}).items():
        if isinstance(k, str) and k.strip():
            try:
                first[k.strip().lower()] = int(v)
            except (TypeError, ValueError):
                continue
    return {"pushed": pushed, "top1_first_at": first}, sha


def remote_pushed_set():
    """推送前二次确认用；返回 (pushed 集合, sha) 或 (None, None) 表示不可读。"""
    try:
        data, sha = read_remote(STATE_PATH)
    except Exception as e:
        log("推送前状态复核失败：{}".format(type(e).__name__))
        return None, None
    if not isinstance(data, dict):
        return set(), sha
    pushed = {str(x).strip().lower() for x in (data.get("pushed") or [])
              if isinstance(x, str) and x.strip()}
    return pushed, sha


def main():
    if SELF_TEST:
        try:
            msg_id = asyncio.run(send_message(
                "云端自检：Telegram 会话可用 {}".format(time.strftime("%Y-%m-%d %H:%M:%S"))))
            log("自检发送成功 msg_id={}".format(msg_id))
            return 0
        except Exception as e:
            log("自检发送失败：{} {}".format(type(e).__name__, str(e)[:200]))
            return 1

    online = local_online()
    if online is None:
        log("心跳状态不可信，本轮退出且绝不推送")
        return 1
    if online:
        log("本机在线，云端本轮不接管")
        return 0

    try:
        tops, nblocks = fetch_top1()
    except Exception as e:
        log("取榜失败：{} {}".format(type(e).__name__, str(e)[:200]))
        return 1

    state, _ = load_state()
    if state is None:
        log("状态不可读，本轮只记录不推送")
        return 1
    pushed, first = state["pushed"], state["top1_first_at"]

    rc = 0
    for idx, best in enumerate(tops):
        rank = idx + 1
        chain, t = best["chain"], best["token"]
        address = str(t.get("address") or "").strip()
        if not CHAIN_RE.match(chain) or not ADDR_RE.match(address):
            log("第 {} 名字段不合法，跳过：chain={} addr={}".format(rank, chain, address[:20]))
            continue
        addr_l = address.lower()
        log("第 {} 名：{} {} visit={}（链块 {} 个）".format(
            rank, chain, address, best["visiting_count"], nblocks))

        # 推送前二次确认：cloud_sync 可能在取榜期间把本机刚推的地址合并进云端。
        rp, _ = remote_pushed_set()
        if rp is None:
            log("推送前状态复核不可读，本轮停止推送（宁可漏一轮，不可重复推）")
            return 1
        if addr_l in rp or addr_l in pushed:
            log("已推过（推送前复核），跳过：第 {} 名 {} {}".format(rank, chain, address))
            if addr_l not in pushed:
                pushed.add(addr_l)
            continue

        if addr_l not in first:
            first[addr_l] = int(time.time())
            log("首次进入前三：第 {} 名 {} {}".format(rank, chain, address))

        info = info_from_token(t)
        msg = build_message(chain, address, info, rank)
        if DRY_RUN:
            log("[DRY] 第 {} 名不发送、不写 pushed；以下是将推送的内容".format(rank))
            print(msg, flush=True)
            continue
        try:
            msg_id = asyncio.run(send_message(msg))
        except Exception as e:
            log("TG 推送失败（第 {} 名）：{} {}".format(rank, type(e).__name__, str(e)[:200]))
            rc = 1
            continue

        new_sha = write_remote_merged(
            STATE_PATH,
            lambda remote, a=addr_l: merged_state(remote, pushed_extra=(a,), first_extra=first),
            "push top3 record [skip ci]")
        if new_sha is None:
            log("严重：消息已发出 msg_id={} 但去重状态未落盘；下一轮可能重复推送，"
                "请把 {} 人工补进 top1_state.json 的 pushed".format(msg_id, addr_l))
            return 1
        pushed.add(addr_l)
        log("TG 推送成功 msg_id={} | 第 {} 名 {} {}".format(msg_id, rank, chain, address))

    if DRY_RUN and first:
        log("DRY 轮结束，未写 pushed、未发消息")
    return rc


if __name__ == "__main__":
    sys.exit(main())
