"""Materials Hub — MCP stdio server(零依赖,Python 标准库)。

让 AI 助手(Claude Desktop / CodeBuddy 等)通过 MCP 直接检索素材库。
注册示例(claude_desktop_config.json / CodeBuddy MCP 配置):
  {"mcpServers": {"materials-hub": {
      "command": "python",
      "args": ["d:/MineWeb/2026/Vitual/materials_hub/mcp_server.py", "--token", "<TOKEN>"],
      "env": {"VITUAL_HUB_TOKEN": "<TOKEN>"}}}}
  # 未设 VITUAL_HUB_TOKEN 时不需要 --token(本机开放);一旦设了 token,MCP 客户端必须传一致的 --token 才能启动。

协议:MCP stdio 传输 = 按行分隔的 JSON-RPC 2.0(每行一条,行内不得有换行)。
stdout 只走协议;日志一律 stderr。语义后端(ollama)没起会顺手自动拉起。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

# 鉴权(与 HTTP server 共用 VITUAL_HUB_TOKEN):若环境变量设了 token,MCP 客户端必须在启动参数里
# 传一致的 --token,否则拒绝启动 —— 防止本机任何进程都能无鉴权调起素材中心 Agent 接口。
HUB_TOKEN = os.environ.get("VITUAL_HUB_TOKEN", "").strip()
_mcp_tok = None
for _i, _a in enumerate(sys.argv):
    if _a == "--token" and _i + 1 < len(sys.argv):
        _mcp_tok = sys.argv[_i + 1]
if HUB_TOKEN and _mcp_tok is None:
    sys.stderr.write("[materials-hub] VITUAL_HUB_TOKEN is set but MCP client did not pass "
                     "--token; refusing to start (add \"--token\" \"<same>\" to mcp args).\n")
    sys.exit(2)
if HUB_TOKEN and _mcp_tok != HUB_TOKEN:
    sys.stderr.write("[materials-hub] MCP --token mismatch; refusing to start.\n")
    sys.exit(2)


def _brief(m):
    """工具返回的精简条目(不带巨型字段,省 token)。"""
    out = {k: m.get(k, "") for k in
           ("id", "kind", "ext", "name", "size", "tags", "description", "location")}
    if m.get("location") == "external":
        out["external_path"] = m.get("external_path", "")
    return out


def t_search(a):
    rows = core.search(a.get("q", ""), a.get("kind", ""), a.get("tag", ""),
                       limit=int(a.get("limit") or 10),
                       mode=a.get("mode", "auto"))
    return [_brief(m) for m in rows]


def t_get(a):
    m = core.get_material(a.get("id", ""))
    if not m:
        raise ValueError("not found: %s" % a.get("id", ""))
    return m


def t_tags(a):
    n = int(a.get("limit") or 50)
    return [{"tag": t, "count": c} for t, c in core.distinct_tags()[:n]]


def t_stats(a):
    return core.health()


HANDLERS = {"search_materials": t_search, "get_material": t_get,
            "list_tags": t_tags, "hub_stats": t_stats}
# __PART2__
_SCHEMA_OBJ = {"type": "object", "properties": {
    "q": {"type": "string", "description": "关键词或中文自然语言问句"},
    "kind": {"type": "string", "enum": ["images", "videos", "docs", "audio", "subs", "other"]},
    "tag": {"type": "string"},
    "mode": {"type": "string", "enum": ["auto", "lexical", "semantic"]},
    "limit": {"type": "integer"}}}

TOOLS = [
    {"name": "search_materials", "description": "检索素材库(344+ 条,支持中文自然语言问句,内置中英同义词+语义混合排序)",
     "inputSchema": _SCHEMA_OBJ},
    {"name": "get_material", "description": "按 id 取素材完整记录",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "list_tags", "description": "列出全部标签及计数",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "hub_stats", "description": "素材库健康快照(总量/种类/重复/引用完整性/封面/字节)",
     "inputSchema": {"type": "object", "properties": {}}},
]
# __PART3__
def _dispatch(req):
    """处理一条 JSON-RPC 请求;notification(无 id)返回 None。"""
    if "id" not in req:
        return None                      # notification: initialized 等,不回包
    m = req.get("method", "")
    if m == "initialize":
        v = req.get("params", {}).get("protocolVersion", "2024-11-05")
        return {"protocolVersion": v, "capabilities": {"tools": {}},
                "serverInfo": {"name": "materials-hub", "version": "1.0"}}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        p = req.get("params", {})
        fn = HANDLERS.get(p.get("name", ""))
        if fn is None:
            raise ValueError("unknown tool: %s" % p.get("name"))
        res = fn(p.get("arguments") or {})
        return {"content": [{"type": "text",
                             "text": json.dumps(res, ensure_ascii=False)}]}
    if m == "ping":
        return {}
    raise ValueError("unknown method: %s" % m)


def main():
    sys.stdout.reconfigure(encoding="utf-8")   # 管道下默认 GBK,写中文会 UnicodeEncodeError
    sys.stdin.reconfigure(encoding="utf-8")
    core.init_hub()
    st = core.ensure_ollama()            # 语义检索后端顺手自愈(失败不阻塞)
    print("mcp: embed %s" % ("ok:" + st["model"] if st["ok"] else "unavailable"),
          file=sys.stderr)
    while True:                          # 必须 readline() 循环:for-in 迭代
        line = sys.stdin.readline()      # 对管道有预读缓冲,会卡到 EOF 才出第一行
        if not line:
            break
        line = line.strip()
        if not line or line.startswith("Content-"):   # 兼容带 LSP 头的客户端
            continue
        try:
            req = json.loads(line)
            res = _dispatch(req)
            if res is None:
                continue
            out = {"jsonrpc": "2.0", "id": req.get("id"), "result": res}
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                   "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
