
#!/usr/bin/env python3
"""共享房间 MCP 服务，同时提供手机聊天网页。"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

ROOM_FILE = os.path.expanduser(
    os.environ.get("ROOM_FILE", "~/.room-mcp/room.json")
)
TZ = int(os.environ.get("ROOM_TZ", "8"))
KEEP = 600
NAME_MAX, TEXT_MAX = 20, 2000

KEYS = {
    k.strip(): v.strip()
    for k, v in (
        x.split(":", 1)
        for x in os.environ.get("ROOM_KEYS", "").split(",")
        if ":" in x
    )
}

mcp = _Server(
    "room",
    instructions=(
        "这是一个几个人共用的群聊。用 room_say 说话，"
        "room_read 看最近的话，room_wait 等别人说话。"
    ),
)


# ---------------- 共享账本 ----------------

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
    parent = os.path.dirname(ROOM_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    r["messages"] = r["messages"][-KEEP:]
    tmp = ROOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)
    os.replace(tmp, ROOM_FILE)


def _locked(name: str, key: str) -> str:
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
    m = {
        "id": r.get("next", 1),
        "t": time.time(),
        "name": name,
        "text": text,
    }
    r["messages"].append(m)
    r["next"] = m["id"] + 1
    r.setdefault("seen", {})[name] = time.time()
    _save(r)
    return m


def _fmt(ms: list) -> str:
    out = []
    for m in ms:
        hm = time.strftime(
            "%H:%M", time.gmtime(m["t"] + TZ * 3600)
        )
        out.append(f"[{m['id']}] {hm} {m['name']}：{m['text']}")
    return "\n".join(out) if out else "（还没有消息）"


# ---------------- 原有 MCP 工具 ----------------

@mcp.tool()
async def room_say(name: str, text: str, key: str = "") -> str:
    """以指定名字发送一条群聊消息。"""
    if not (text or "").strip():
        return "没发：空的。"
    name = (name or "").strip()[:NAME_MAX] or "匿名"
    bad = _locked(name, key or "")
    if bad:
        return "没发：" + bad
    m = _post(name, text)
    return f"发了 [{m['id']}]。最近：\n" + _fmt(
        _load()["messages"][-8:]
    )


@mcp.tool()
async def room_read(limit: int = 20, after_id: int = 0) -> str:
    """查看最近消息；after_id 大于零时只看它之后的消息。"""
    r = _load()
    ms = r["messages"]
    if after_id > 0:
        ms = [m for m in ms if m["id"] > after_id]
    ms = ms[-max(1, min(int(limit), 50)):]
    who = "、".join(sorted(r.get("seen", {}).keys())) or "还没人"
    return f"在座过的：{who}\n" + _fmt(ms)


@mcp.tool()
async def room_wait(
    name: str,
    after_id: int = 0,
    timeout: int = 45,
    key: str = "",
) -> str:
    """等待别人发送新消息，最多等待 55 秒。"""
    name = (name or "").strip()[:NAME_MAX] or "匿名"
    bad = _locked(name, key or "")
    if bad:
        return "没等：" + bad

    timeout = max(1, min(int(timeout), 55))
    r = _load()
    r.setdefault("seen", {})[name] = time.time()
    _save(r)

    if after_id <= 0:
        after_id = r.get("next", 1) - 1

    end = time.time() + timeout
    while True:
        r = _load()
        new = [
            m for m in r["messages"]
            if m["id"] > after_id and m["name"] != name
        ]
        if new:
            return "新消息：\n" + _fmt(new[-20:])
        if time.time() >= end:
            return (
                f"等了 {timeout} 秒没人说话"
                f"（最后一条 id={r.get('next', 1) - 1}）。"
            )
        await asyncio.sleep(1.5)


@mcp.tool()
async def room_who() -> str:
    """查看谁来过以及最后出现的时间。"""
    seen = _load().get("seen", {})
    if not seen:
        return "还没人来过。"
    now = time.time()
    return "\n".join(
        f"{k}：{int((now - v) // 60)} 分钟前"
        for k, v in sorted(seen.items(), key=lambda kv: -kv[1])
    )


# ---------------- 网页接口 ----------------

@mcp.custom_route("/", methods=["GET"])
async def web_home(request: Request):
    path = Path(__file__).with_name("index.html")
    try:
        return HTMLResponse(path.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("请在 room_mcp.py 同目录放置 index.html", status_code=500)


@mcp.custom_route("/api/messages", methods=["GET"])
async def web_messages(request: Request):
    try:
        after_id = max(0, int(request.query_params.get("after_id", "0")))
    except ValueError:
        return JSONResponse({"error": "after_id 必须是整数"}, status_code=400)

    messages = _load()["messages"]
    if after_id:
        messages = [m for m in messages if m["id"] > after_id]
    else:
        messages = messages[-100:]

    return JSONResponse({"messages": messages})


@mcp.custom_route("/api/send", methods=["POST"])
async def web_send(request: Request):
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "请求不是有效 JSON"}, status_code=400)

    if not isinstance(data, dict):
        return JSONResponse({"error": "请求格式错误"}, status_code=400)

    name = str(data.get("name", "")).strip()[:NAME_MAX] or "匿名"
    message = str(data.get("text", "")).strip()[:TEXT_MAX]
    key = str(data.get("key", ""))

    if not message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    bad = _locked(name, key)
    if bad:
        return JSONResponse({"error": bad}, status_code=403)

    return JSONResponse({"message": _post(name, message)})


# ---------------- 启动 ----------------

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        port = int(sys.argv[2])
        host = os.environ.get("ROOM_HOST", "127.0.0.1")
        try:
            mcp.run(transport="streamable-http", host=host, port=port)
        except TypeError:
            mcp.settings.host, mcp.settings.port = host, port
            mcp.run(transport="streamable-http")
    else:
        mcp.run()
