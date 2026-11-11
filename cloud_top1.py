#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMGN 热搜·全部链 总榜第一名 -> Telegram 群（云端独立实例）
=========================================================================
与本机 top1_bsc_push.py 同一口径，但只跑 API 源（gmgn-cli market hot-searches）：
- 取 10 个链块各自的第一名，按 visiting_count 取最大值 = 全部链混排第一
- 一个合约永久只推一次（pushed 集合，与本地 top1_seen.json 同构、双向合并）
- 记录「首次观测到第一」的时间戳（top1_first_at）
- 每次运行前先读本机心跳，本机在线（心跳新鲜）则整轮跳过：不抢 API 限流、不重复推送

Env:
  GITHUB_TOKEN        Actions 自动提供，读写仓库内状态文件
  REPO                仓库全名（默认 qianjia2021-coder/gmgn-top1-cloud）
  TG_API_ID / TG_API_HASH / TG_STRING_SESSION
                      独立登录的第二份用户会话（与本机 web3_session 不同 auth key）
  TG_CHAT_ID          目标群，默认 -5589667040（GMGN热搜）
  HB_STALE            本机心跳过期阈值（秒），默认 180
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
HB_STALE = int(os.environ.get("HB_STALE", "180"))
LIMIT = int(os.environ.get("LIMIT", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")
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


def write_remote(path, data, sha, msg):
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    body = {"message": msg,
            "content": base64.b64encode(payload).decode("utf-8")}
    if sha:
        body["sha"] = sha
    for attempt in (1, 2):
        r = requests.put("{}/repos/{}/contents/{}".format(GH_API, REPO, path),
                         headers=gh_headers(), json=body, timeout=25)
        if r.status_code in (200, 201):
            return r.json()["content"]["sha"]
        if r.status_code == 409 and attempt == 1:      # sha 过期，重新取一次
            _, sha2 = read_remote(path)
            if sha2:
                body["sha"] = sha2
                continue
            body.pop("sha", None)
            continue
        log("状态写入失败 HTTP {} {}".format(r.status_code, r.text[:200]))
        return None
    return None


def local_online():
    """本机心跳新鲜 => 本机推送器在线，云端不接管。"""
    try:
        hb, _ = read_remote(HB_PATH)
    except Exception as e:
        log("心跳读取失败，按「本机离线」处理：{}".format(type(e).__name__))
        return False
    if not isinstance(hb, dict):
        return False
    try:
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    age = time.time() - ts
    log("本机心跳龄 {:.0f} 秒（阈值 {} 秒）".format(age, HB_STALE))
    return 0 <= age < HB_STALE


# ---------------------------------------------------------------- 榜单
def fetch_top1():
    args = ["gmgn-cli", "market", "hot-searches",
            "--interval", "1h", "--limit", str(LIMIT), "--raw"]
    out = None
    for attempt in (1, 2):
        p = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if p.returncode == 0:
            out = p.stdout
            break
        err = (p.stderr or p.stdout or "")[:300]
        if ("429" in err or "RATE_LIMIT" in err) and attempt == 1:
            log("命中限流，等待 35 秒重试")
            time.sleep(35)
            continue
        raise RuntimeError("gmgn-cli 失败 rc={} {}".format(p.returncode, err))
    try:
        blocks = json.loads(out)
    except Exception as e:
        raise RuntimeError("返回不是合法 JSON：{}".format(e))
    if not isinstance(blocks, list):
        raise RuntimeError("返回结构异常")

    best = None
    for b in blocks:
        if not isinstance(b, dict):
            continue
        chain = str(b.get("chain") or "").strip().lower()
        toks = b.get("tokens") or []
        if not toks or not isinstance(toks[0], dict):
            continue
        t = toks[0]
        try:
            v = float(t.get("visiting_count") or 0)
        except (TypeError, ValueError):
            v = 0.0
        if best is None or v > best["visiting_count"]:
            best = {"chain": chain, "token": t, "visiting_count": v}
    if best is None:
        raise RuntimeError("榜单里没有任何 token")
    return best, len(blocks)


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
    return html.escape(str(v), quote=False)


def build_message(chain, address, info):
    url = "https://gmgn.ai/{}/token/{}".format(
        urllib.parse.quote(chain, safe=""), urllib.parse.quote(address, safe=""))
    lines = [
        "🔥 <b>热搜·全部链 第 1 名</b>",
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


def main():
    if local_online():
        log("本机在线，云端本轮不接管")
        return 0

    best, nblocks = fetch_top1()
    chain, t = best["chain"], best["token"]
    address = str(t.get("address") or "").strip()
    if not CHAIN_RE.match(chain) or not ADDR_RE.match(address):
        log("榜首字段不合法，跳过：chain={} addr={}".format(chain, address[:20]))
        return 1
    addr_l = address.lower()
    log("榜首：{} {} visit={}（链块 {} 个）".format(chain, address, best["visiting_count"], nblocks))

    state, sha = load_state()
    if state is None:
        log("状态不可读，本轮只记录不推送")
        return 1

    pushed, first = state["pushed"], state["top1_first_at"]
    changed = False
    if addr_l not in first:
        first[addr_l] = int(time.time())
        changed = True
        log("首次观测登顶：{} {}".format(chain, address))

    if addr_l in pushed:
        if changed:
            write_remote(STATE_PATH, {"pushed": sorted(pushed), "updated": int(time.time()),
                                      "top1_first_at": {k: first[k] for k in sorted(first)}},
                         sha, "record first-top1 time [skip ci]")
        log("已推过，跳过：{} {}".format(chain, address))
        return 0

    info = info_from_token(t)
    msg = build_message(chain, address, info)
    if DRY_RUN:
        log("[DRY] 云端将推送以下内容（本轮不实际发送、不写入 pushed）")
        print(msg, flush=True)
        return 0
    try:
        msg_id = asyncio.run(send_message(msg))
    except Exception as e:
        log("TG 推送失败：{} {}".format(type(e).__name__, str(e)[:200]))
        return 1

    pushed.add(addr_l)
    write_remote(STATE_PATH, {"pushed": sorted(pushed), "updated": int(time.time()),
                              "top1_first_at": {k: first[k] for k in sorted(first)}},
                 sha, "push top1 record [skip ci]")
    log("TG 推送成功 msg_id={} | {} {}".format(msg_id, chain, address))
    return 0


if __name__ == "__main__":
    sys.exit(main())
