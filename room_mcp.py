#!/usr/bin/env python3
"""
room_mcp — 让几个 AI（和你）在同一个群里说话的最小 MCP 服务。

原理只有一句：一张共用的纸。谁调 room_say 就往纸上写一行，
谁调 room_read 就看纸上最近几行，谁调 room_wait 就坐着等别人写下一行。
把这个服务接进每个 AI 的连接器，它们就在一个群里了。

用法：
  python room_mcp.py                 # stdio 模式（给本机的 Claude Code / Claude Desktop / Codex 这类）
  python room_mcp.py --http 8765     # HTTP 模式，地址 http://127.0.0.1:8765/mcp（给网页版 AI 的连接器；要配反向代理和 https）
  ROOM_FILE=~/room.json 改账本位置；ROOM_TZ=8 改显示时区（默认北京时间）
  ROOM_KEYS="Noir:abc,star:xyz" 可选：给名字配钥匙。配了以后只有带对钥匙的才能用这个名字说话；不配就谁都能用任何名字（朋友之间默认这样）

依赖：pip install "mcp>=2"   （旧版 mcp 1.x 也能跑，代码里自动兼容）
"""
import asyncio
import json
import os
import sys
import time

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

ROOM_FILE = os.path.expanduser(os.environ.get("ROOM_FILE", "~/.room-mcp/room.json"))
TZ = int(os.environ.get("ROOM_TZ", "8"))
KEEP = 600            # 账本只留最近这么多条
NAME_MAX, TEXT_MAX = 20, 2000
# 可选的门锁：名字 -> 钥匙。为空 = 不锁，谁都能用任何名字（一屋子自己人的默认）。
KEYS = {k.strip(): v.strip() for k, v in (x.split(":", 1) for x in os.environ.get("ROOM_KEYS", "").split(",") if ":" in x)}

mcp = _Server(
    "room",
    instructions=(
        "这是一个几个人共用的群聊。用 room_say 说话（name 填你自己的名字），"
        "room_read 看最近的话，room_wait 等别人说话。说话要短，像发消息，一次一两句。"
    ),
)


# ---------------- 账本：一个 JSON 文件 ----------------
def _load() -> dict:
    try:
        with open(ROOM_FILE, encoding="utf-8") as f:
            r = json.load(f)
        if isinstance(r.get("messages"), list):
            return r
    except (OSError, ValueError):
        pass
    return {"messages": [], "next": 1, "seen": {}}


def _save(r: dict) -> None:
    os.makedirs(os.path.dirname(ROOM_FILE), exist_ok=True)
    r["messages"] = r["messages"][-KEEP:]
    tmp = ROOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)
    os.replace(tmp, ROOM_FILE)


def _locked(name: str, key: str) -> str:
    """配了 ROOM_KEYS 才检查：名字不在名单上或钥匙不对，返回一句拒绝的话；否则返回空串。"""
    if not KEYS:
        return ""
    if name not in KEYS:
        return f"这个群锁了名字，「{name}」不在名单上。"
    if key != KEYS[name]:
        return f"「{name}」的钥匙不对。"
    return ""


def _post(name: str, text: str) -> dict:
    name = (name or "").strip()[:NAME_MAX] or "匿名"
    text = (text or "").strip()[:TEXT_MAX]
    r = _load()
    m = {"id": r["next"], "t": time.time(), "name": name, "text": text}
    r["messages"].append(m)
    r["next"] += 1
    r.setdefault("seen", {})[name] = time.time()
    _save(r)
    return m


def _fmt(ms: list) -> str:
    out = []
    for m in ms:
        hm = time.strftime("%H:%M", time.gmtime(m["t"] + TZ * 3600))
        out.append(f"[{m['id']}] {hm} {m['name']}：{m['text']}")
    return "\n".join(out) if out else "（还没有消息）"


# ---------------- 三个半工具 ----------------
@mcp.tool()
async def room_say(name: str, text: str, key: str = "") -> str:
    """群聊：以 name 的身份说一句话（name 填你自己的名字；群锁了名字的话还要带 key）。说完会返回最近几条。说话要短，一次一两句。"""
    if not (text or "").strip():
        return "没发：空的。"
    bad = _locked((name or "").strip()[:NAME_MAX], key or "")
    if bad:
        return "没发：" + bad
    m = _post(name, text)
    return f"发了 [{m['id']}]。最近：\n" + _fmt(_load()["messages"][-8:])


@mcp.tool()
async def room_read(limit: int = 20, after_id: int = 0) -> str:
    """群聊：看消息。limit 最近几条（上限 50）；after_id > 0 时只看这个 id 之后的新消息。"""
    r = _load()
    ms = r["messages"]
    if after_id > 0:
        ms = [m for m in ms if m["id"] > after_id]
    ms = ms[-max(1, min(int(limit), 50)):]
    who = "、".join(sorted(r.get("seen", {}).keys())) or "还没人"
    return f"在座过的：{who}\n" + _fmt(ms)


@mcp.tool()
async def room_wait(name: str, after_id: int = 0, timeout: int = 45, key: str = "") -> str:
    """群聊：以 name 的身份等别人的新消息（id > after_id 且不是自己发的），有就立刻返回；最多等 timeout 秒（上限 55）。
    等不到就停下来告诉人类，等人类说继续再调。不要连续反复调用。群锁了名字的话要带 key。"""
    name = (name or "").strip()[:NAME_MAX] or "匿名"
    bad = _locked(name, key or "")
    if bad:
        return "没等：" + bad
    timeout = max(1, min(int(timeout), 55))
    r = _load()
    r.setdefault("seen", {})[name] = time.time()
    _save(r)
    if after_id <= 0:
        after_id = r["next"] - 1
    end = time.time() + timeout
    while True:
        r = _load()
        new = [m for m in r["messages"] if m["id"] > after_id and m["name"] != name]
        if new:
            return "新消息：\n" + _fmt(new[-20:])
        if time.time() >= end:
            return f"等了 {timeout} 秒没人说话（最后一条 id={r['next'] - 1}）。停下，告诉人类你在等，等人类说继续再来。"
        await asyncio.sleep(1.5)


@mcp.tool()
async def room_who() -> str:
    """群聊：看谁来过、最后一次是什么时候。"""
    seen = _load().get("seen", {})
    if not seen:
        return "还没人来过。"
    now = time.time()
    return "\n".join(f"{k}：{int((now - v) // 60)} 分钟前" for k, v in sorted(seen.items(), key=lambda kv: -kv[1]))


# ---------------- 启动 ----------------
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        port = int(sys.argv[2])
        host = os.environ.get("ROOM_HOST", "127.0.0.1")
        try:
            mcp.run(transport="streamable-http", host=host, port=port)
        except TypeError:  # mcp 1.x 的写法
            mcp.settings.host, mcp.settings.port = host, port
            mcp.run(transport="streamable-http")
    else:
        mcp.run()
