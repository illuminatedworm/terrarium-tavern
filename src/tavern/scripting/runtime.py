"""Isolated card-script executor used by Roleplay Runtime, not the orchestrator.

Browser-backed compatibility execution; network is denied at context level.
"""

import asyncio
import copy
import re
from pathlib import Path

from tavern.character.cards import card_scripts, lore_entries, normalize

SDK = Path(__file__).parents[1] / "static" / "runtime.js"


class ScriptRuntime:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.browser is None:
                from playwright.async_api import async_playwright

                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.playwright = None

    async def open(
        self,
        document,
        history,
        snapshot,
        allowed,
        host,
        scripts_override=None,
        prepare_markup=None,
        persona="旅人",
        opening=False,
        greeting_id=None,
    ):
        await self.start()
        if not SDK.exists():
            raise ValueError("兼容 SDK 尚未构建，请先运行 npm run build")
        context = await self.browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.abort())
        page = await context.new_page()
        await page.set_content(
            "<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; connect-src 'none'\"></head><body></body></html>"
        )
        runtime = RuntimeSession(context, page)

        async def boundary(method, args):
            if method == "event":
                return await runtime.dispatch(args["name"], args["args"])
            return await host(method, args)

        await page.expose_function("__tavernHost", boundary)
        await page.add_script_tag(content=SDK.read_text(encoding="utf-8"))
        await page.add_script_tag(content=(SDK.parent / "mvu-core.js").read_text(encoding="utf-8"))
        messages = [
            {
                "message_id": i,
                "id": m["id"],
                "role": m["role"],
                "message": m["raw"],
                "variables": copy.deepcopy(m["snapshot"].get("state", {})),
            }
            for i, m in enumerate(history)
        ]
        ext = normalize(document).get("extensions", {})
        scopes = copy.deepcopy(snapshot.get("scopes", {}))
        helper = ext.get("tavern_helper", {})
        if isinstance(helper, list):
            helper = dict(helper)
        scopes.setdefault(
            "character", helper.get("variables", ext.get("TavernHelper_characterScriptVariables", {}))
        )
        data = {
            "serverMain": True,
            "messages": messages,
            "state": copy.deepcopy(snapshot.get("state", {})),
            "scopes": scopes,
            "lore": lore_entries(document),
            "bookName": normalize(document).get("character_book", {}).get("name", "character"),
        }
        for entry in data["lore"]:
            if (entry.get("terrarium", {}).get("audience") == "runtime_init"
                    and entry.get("enabled", True) and not entry.get("disable", False)):
                entry["comment"] = "[initvar] " + entry.get("comment", "")
            if "[initvar]" in entry.get("comment", "").lower():
                entry["content"] = (
                    entry.get("content", "")
                    .replace("{{user}}", persona)
                    .replace("{{char}}", normalize(document).get("name", ""))
                )
        try:
            await runtime.call("install", data, install=True)
            scripts = scripts_override if scripts_override is not None else card_scripts(document)
            if scripts and not allowed:
                raise ValueError("此卡包含角色脚本，请在角色资料中启用脚本权限")
            if allowed:
                await runtime.call("runScripts", scripts)
                # Render message-local callbacks in server-owned sandboxes as well.
                # DOM closures stay in their original realm; mutable event arguments round-trip.
                for index, message in enumerate(history):
                    html = message.get("display", message.get("raw", ""))
                    if prepare_markup and "<" in html:
                        html = prepare_markup(html)
                    if "<script" not in html.lower():
                        continue
                    await runtime.add_message_frame(
                        html, {**data, "serverMain": False, "currentMessage": index}, host
                    )
            greeting = history[0]["raw"] if opening and history else ""
            from tavern.character.card_workflow import workflow
            metadata = workflow(document)
            migrated = None
            if opening and metadata:
                migrated = next((r for r in metadata["greetings"] if r["id"] == greeting_id), None)
                if migrated is None:
                    raise ValueError("找不到所选开场的初始化记录")
                greeting = migrated.get("initialization", "")
            greeting = greeting.replace("{{user}}", persona).replace("{{char}}", normalize(document).get("name", ""))
            await runtime.call("initMvu", greeting)
            if migrated and greeting.strip():
                # This side-channel is data-only and never becomes a story message.
                updates = re.sub(r"<initvar\b[^>]*>[\s\S]*?</initvar\s*>", "", greeting, flags=re.I)
                if updates.strip():
                    await runtime.call("initializeUpdates", updates, migrated["id"])
            return runtime
        except BaseException:
            await runtime.close()
            raise


class RuntimeSession:
    def __init__(self, context, page):
        self.context, self.page = context, page
        self.frames = []

    async def add_message_frame(self, html, data, host):
        frame = await self.context.new_page()
        await frame.set_content(
            "<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; connect-src 'none'\"></head><body></body></html>"
        )
        await frame.expose_function("__tavernHost", host)
        await frame.add_script_tag(content=SDK.read_text(encoding="utf-8"))
        await frame.add_script_tag(content=(SDK.parent / "mvu-core.js").read_text(encoding="utf-8"))
        await frame.evaluate("data=>{TavernRuntime.install(data,(m,a)=>__tavernHost(m,a));}", data)
        await frame.evaluate("html=>{document.body.innerHTML=html;}", html)
        await frame.evaluate(
            "()=>{if(!document.querySelector('#send_textarea')){const e=document.createElement('textarea');e.id='send_textarea';e.hidden=true;document.body.append(e);}}"
        )
        for tag in re.finditer(r"<script\b([^>]*)>([\s\S]*?)</script\s*>", html, re.I):
            if re.search(r"\bsrc\s*=", tag[1]):
                raise ValueError("消息脚本依赖尚未内联，请解析卡片依赖")
            await asyncio.wait_for(frame.evaluate("code=>{return (0,eval)(code);}", tag[2]), 10)
        self.frames.append(frame)

    async def dispatch(self, name, args):
        for frame in self.frames:
            args = await asyncio.wait_for(
                frame.evaluate(
                    "({name,args})=>TavernRuntime.dispatch(name,args)", {"name": name, "args": args}
                ),
                10,
            )
        return args

    async def call(self, name, *args, install=False):
        expression = (
            "data => TavernRuntime.install(data, (m,a) => __tavernHost(m,a))"
            if install
            else "({name,args}) => TavernRuntime[name](...args)"
        )
        payload = args[0] if install else {"name": name, "args": args}
        try:
            return await asyncio.wait_for(self.page.evaluate(expression, payload), timeout=10)
        except asyncio.TimeoutError:
            await self.context.close()
            raise ValueError("卡片脚本运行超时，回合未提交") from None

    async def close(self):
        await self.context.close()
