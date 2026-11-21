"""Stdlib stdio MCP client for tests: newline-delimited JSON-RPC against `repairlib mcp`."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json


@dataclass
class Reply:
    is_error: bool
    structured_content: dict
    content: list
    raw: dict = field(repr=False)

    def model_dump_json(self) -> str:
        return json.dumps(self.raw)


@dataclass
class ToolView:
    name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    annotations: "Annotations | None"


@dataclass
class Annotations:
    read_only_hint: bool
    destructive_hint: bool
    idempotent_hint: bool
    open_world_hint: bool


@dataclass
class Initialized:
    server_info: "Named"
    capabilities: "Capabilities"


@dataclass
class Named:
    name: str


@dataclass
class Capabilities:
    tools: dict | None


class Session:
    def __init__(self, process, timeout):
        self.process, self.timeout, self.counter = process, timeout, 0

    async def request(self, method, params=None):
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()
        async with asyncio.timeout(self.timeout):
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise EOFError("MCP server closed stdout")
                reply = json.loads(line)
                if reply.get("id") == self.counter:
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    return reply["result"]

    async def notify(self, method):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        await self.process.stdin.drain()

    async def initialize(self):
        result = await self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "repairlib-test", "version": "0"}})
        await self.notify("notifications/initialized")
        return Initialized(Named(result["serverInfo"]["name"]), Capabilities(result["capabilities"].get("tools")))

    async def list_tools(self):
        result = await self.request("tools/list")
        tools = []
        for item in result["tools"]:
            hints = item.get("annotations")
            tools.append(ToolView(item["name"], item["description"], item["inputSchema"],
                                  item.get("outputSchema"),
                                  hints and Annotations(hints["readOnlyHint"], hints["destructiveHint"],
                                                        hints["idempotentHint"], hints["openWorldHint"])))
        return type("Listed", (), {"tools": tools})()

    async def call_tool(self, name, arguments):
        raw = await self.request("tools/call", {"name": name, "arguments": arguments})
        return Reply(raw["isError"], raw.get("structuredContent"), raw["content"], raw)


@asynccontextmanager
async def stdio_session(command, env, errlog, timeout=8):
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errlog, env=env, limit=1 << 24)
    try:
        yield Session(process, timeout)
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
