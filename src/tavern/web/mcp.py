"""Small Streamable HTTP client restricted to Exa search and page fetch."""
import json

URL = "https://mcp.exa.ai/mcp"
VERSION = "2025-03-26"


class ExaMCP:
    def __init__(self, client, request, auth):
        self.client, self.request = client, request
        self.headers = {"Accept": "application/json, text/event-stream", **auth}
        self.serial = 0

    async def rpc(self, method, params=None, *, notify=False):
        self.serial += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            body["id"] = self.serial
        response = await self.request(self.client, "POST", URL, headers=self.headers, body=body)
        if method == "initialize":
            if response.headers.get("mcp-session-id"):
                self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notify and not response.content:
            return {}
        if "text/event-stream" in response.headers.get("content-type", ""):
            packets = []
            for block in response.text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
                if data:
                    packets.append(json.loads(data))
        else:
            packets = [response.json()]
        if notify:
            return {}
        matches = [p for p in packets if isinstance(p, dict) and p.get("id") == self.serial]
        if len(matches) != 1 or matches[0].get("jsonrpc") != "2.0":
            raise ValueError("Exa MCP 响应关联无效")
        packet = matches[0]
        if "error" in packet or not isinstance(packet.get("result"), dict):
            raise ValueError("Exa MCP 拒绝请求或协议响应无效")
        return packet["result"]

    async def call(self, name, arguments, *, objective=None):
        if name not in {"web_search_exa", "web_fetch_exa"}:
            raise ValueError("Exa 工具不在允许范围内")
        try:
            init = await self.rpc("initialize", {"protocolVersion": VERSION, "capabilities": {},
                "clientInfo": {"name": "TerrariumTavern", "version": "1.0"}})
            if init.get("protocolVersion") not in {VERSION, "2025-06-18", "2024-11-05"}:
                raise ValueError("Exa MCP 协议版本不受支持")
            self.headers["MCP-Protocol-Version"] = init["protocolVersion"]
            await self.rpc("notifications/initialized", notify=True)
            catalog = await self.rpc("tools/list")
            tool = next((tool for tool in catalog.get("tools", []) if tool.get("name") == name), None)
            if tool is None:
                raise ValueError("Exa 未提供所需的搜索或阅读工具")
            schema = tool.get("inputSchema", {})
            if name == "web_search_exa" and "objective" in schema.get("properties", {}):
                arguments = {**arguments, "objective": objective}
            if set(schema.get("required", [])) - set(arguments):
                raise ValueError("Exa 工具参数已变化，当前版本无法调用")
            result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
            if result.get("isError"):
                # Upstream text may contain credentials; only expose a classified error.
                text = self.text(result).lower()
                if any(word in text for word in ("rate limit", "quota", "credit", "429")):
                    reason = "服务限流或额度不足"
                elif any(word in text for word in ("unauthorized", "invalid api key", "forbidden")):
                    reason = "密钥无效或未获授权"
                elif "invalid request" in text or "validation error" in text or "maxcharacters must" in text:
                    reason = "服务不接受当前工具参数"
                else:
                    reason = "工具执行失败"
                raise ValueError("Exa " + reason)
            return result
        finally:
            if "Mcp-Session-Id" in self.headers:
                # Session deletion is best effort; cancellation still closes the HTTP client.
                try:
                    await self.client.delete(URL, headers=self.headers, timeout=2)
                except Exception:
                    pass

    @staticmethod
    def text(result):
        content = result.get("content")
        if not isinstance(content, list) or any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in content):
            raise ValueError("Exa 返回了无法完整读取的内容格式")
        return "\n".join(part["text"] for part in content)
