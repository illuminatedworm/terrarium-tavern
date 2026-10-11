"""Isolated card-script executor used by Roleplay Runtime, not the orchestrator.

Browser-backed compatibility execution; network is denied at context level.
"""

import asyncio
import copy
import json
import re
import secrets
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from tavern.character.cards import card_scripts, lore_entries, normalize

SDK = Path(__file__).parents[1] / "static" / "runtime.js"


@lru_cache(maxsize=4)
def _source(path, stamp):
    return path.read_text(encoding="utf-8")


def script_source(path):
    return _source(path, path.stat().st_mtime_ns)


class RuntimeRollbackError(RuntimeError):
    """A failed script patch cannot safely be retried in this runtime."""


class ScriptRuntime:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.lock = asyncio.Lock()
        self.documents = OrderedDict()
        self.document_bytes = 0

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
        self.documents.clear()
        self.document_bytes = 0

    async def open(
        self,
        document,
        history,
        snapshot,
        allowed,
        host,
        scripts_override=None,
        prepare_markup=None,
        persona="用户",
        opening=False,
        greeting_id=None,
    ):
        await self.start()
        if not SDK.exists():
            raise ValueError("兼容 SDK 尚未构建，请先运行 npm run build")
        context = await self.browser.new_context(service_workers="block")
        try:
            return await self._open_context(context, document, history, snapshot, allowed, host,
                                            scripts_override, prepare_markup, persona, opening, greeting_id)
        except BaseException:
            # Setup failures and cancellation before install have the same lifetime
            # boundary as script failures. No orphan browser contexts may survive.
            await context.close()
            raise

    async def _open_context(self, context, document, history, snapshot, allowed, host,
                            scripts_override, prepare_markup, persona, opening, greeting_id):
        prefix = f"https://tavern.invalid/{secrets.token_urlsafe(24)}/"
        messages = [
            {"message_id": i, "id": m["id"], "role": m["role"], "message": m["raw"],
             "variables": copy.deepcopy(m["snapshot"].get("state", {}))}
            for i, m in enumerate(history)
        ]
        # Shallow prefix maps retain immutable values once. Reading the combined
        # variable view must not load every historical message into every realm.
        variables = []
        combined = {}
        for message in messages:
            combined = {**combined, **message["variables"]}
            variables.append(combined)

        async def resource(route):
            url = route.request.url
            if not url.startswith(prefix) or route.request.method != "GET":
                return await route.abort()
            key = url[len(prefix):]
            if key == "core.js":
                body, mime = script_source(SDK.parent / "mvu-core.js"), "text/javascript"
            else:
                match = re.fullmatch(r"(messages|variables)/(\d+)", key)
                if not match or int(match[2]) >= len(messages):
                    return await route.abort()
                body = json.dumps((messages if match[1] == "messages" else variables)[int(match[2])], ensure_ascii=False)
                mime = "application/json"
            await route.fulfill(body=body, content_type=mime,
                                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

        await context.route("**/*", resource)
        page = await context.new_page()
        await page.set_content(
            "<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; connect-src 'none'\"></head><body></body></html>"
        )
        runtime = RuntimeSession(context, page, prefix, len(messages), self)

        async def boundary(method, args):
            if method == "event":
                return await runtime.dispatch(args["name"], args["args"])
            return await host(method, args)

        await page.expose_function("__tavernHost", boundary)
        await page.add_script_tag(content=script_source(SDK))
        await page.add_script_tag(content=script_source(SDK.parent / "mvu-core.js"))
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
            "persona": persona,
            "character": normalize(document).get("nickname") or normalize(document).get("name", ""),
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
                        html, {**{k: v for k, v in data.items() if k != "messages"},
                               "serverMain": False, "currentMessage": index}, host
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
    def __init__(self, context, page, prefix="", history_length=0, owner=None):
        self.context, self.page = context, page
        self.frames = []
        self.prefix, self.history_length, self.owner = prefix, history_length, owner

    async def add_message_frame(self, html, data, host):
        # Reuse the presentation parser, including code-fence language and full
        # document boundaries; narrative/code examples must never become scripts.
        documents = self.owner.documents.get(html) if self.owner else None
        if documents is None:
            documents = await self.call("htmlDocuments", html)
            # Cache only immutable parsing, never runtime closures or transactional state.
            size = len(html.encode("utf-8")) + sum(len(d.encode("utf-8")) for d in documents)
            if self.owner and size <= 16 * 1024 * 1024 and html not in self.owner.documents:
                self.owner.documents[html] = documents
                self.owner.document_bytes += size
                while self.owner.document_bytes > 16 * 1024 * 1024 or len(self.owner.documents) > 128:
                    old, docs = self.owner.documents.popitem(last=False)
                    self.owner.document_bytes -= len(old.encode("utf-8")) + sum(len(d.encode("utf-8")) for d in docs)
        elif self.owner:
            self.owner.documents.move_to_end(html)
        for document in documents:
            if "<script" in document.lower():
                capabilities = await self.call("frameCapabilities", document)
                if capabilities["displayOnly"]:
                    continue
                document = await self.call("snapshotMacros", document, data.get("state", {}), data.get("scopes", {}))
                document = document.replace("{{user}}", data.get("persona", "用户")).replace("{{char}}", data.get("character", ""))
                await self._add_message_frame(document, data, host)

    async def _add_message_frame(self, html, data, host):
        frame = await self.context.new_page()
        await frame.set_content(
            f"<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {self.prefix}; style-src 'unsafe-inline' data:; connect-src {self.prefix}\"></head><body></body></html>"
        )
        await frame.expose_function("__tavernHost", host)
        await frame.add_script_tag(content=script_source(SDK))
        await frame.evaluate("""({data,prefix,length})=>{
            const read=(kind,index)=>{
                const request=new XMLHttpRequest();
                request.open('GET',prefix+kind+'/'+index,false); request.send();
                if(request.status!==200)throw Error('历史数据读取失败');
                return JSON.parse(request.responseText);
            };
            TavernRuntime.install(data,(m,a)=>__tavernHost(m,a),{
                history:{length,read:i=>read('messages',i),variables:i=>read('variables',i)},
                loadCore:()=>new Promise((resolve,reject)=>{
                    const script=document.createElement('script');script.src=prefix+'core.js';
                    script.onload=resolve;script.onerror=()=>reject(Error('MVU 核心加载失败'));
                    document.head.append(script);
                })
            });
        }""", {"data": data, "prefix": self.prefix, "length": self.history_length})
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
        # Full Agent patches may invoke MVU callbacks in message-local realms.
        # Restore every mirror on failure, not just the server's stat_data.
        patch_call = name == "process" and len(args) >= 3 and args[2] == "" and bool(args[1])
        checkpoints = [(page, await page.evaluate("() => TavernRuntime.checkpoint()"))
                       for page in [self.page, *self.frames]] if patch_call else []
        try:
            return await asyncio.wait_for(self.page.evaluate(expression, payload), timeout=10)
        except asyncio.TimeoutError:
            await self.context.close()
            raise ValueError("卡片脚本运行超时，回合未提交") from None
        except Exception:
            try:
                for page, checkpoint in checkpoints:
                    await page.evaluate("data => TavernRuntime.restore(data)", checkpoint)
            except Exception as error:
                raise RuntimeRollbackError("脚本镜像回滚失败，必须停止当前回合") from error
            raise

    async def close(self):
        await self.context.close()
