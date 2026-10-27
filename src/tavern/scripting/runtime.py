"""Isolated card-script executor used by Roleplay Runtime, not the orchestrator.

Browser-backed compatibility execution through a realm backend; network is denied
at context level.
"""

import asyncio
import copy
import json
import re
import secrets
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from tavern.character.cards import card_scripts, is_initvar, lore_entries, normalize
from tavern.scripting.realm import RealmBackend, default_backend

SDK = Path(__file__).parents[1] / "static" / "runtime.js"


@lru_cache(maxsize=4)
def _source(path, stamp):
    return path.read_text(encoding="utf-8")


def script_source(path):
    return _source(path, path.stat().st_mtime_ns)


class RuntimeRollbackError(RuntimeError):
    """A failed script patch cannot safely be retried in this runtime."""


class ScriptRuntime:
    def __init__(self, backend: RealmBackend | None = None):
        self.backend = backend or default_backend()
        self.documents = OrderedDict()
        self.document_bytes = 0

    async def close(self):
        await self.backend.close()
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
        eager=False,
    ):
        """Open a script session for one turn or action.

        A card with no scripts, message scripts or MVU initialization gets a deferred
        session that starts the realm only when a call needs JavaScript. ``eager`` is
        for callers that drive the realm page or context directly.
        """
        if not SDK.exists():
            raise ValueError("兼容 SDK 尚未构建，请先运行 npm run build")
        prefix = f"https://tavern.invalid/{secrets.token_urlsafe(24)}/"
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
        # Shallow prefix maps retain immutable values once. Reading the combined
        # variable view must not load every historical message into every realm.
        variables = []
        combined = {}
        for message in messages:
            combined = {**combined, **message["variables"]}
            variables.append(combined)

        def resource(url, method):
            if not url.startswith(prefix) or method != "GET":
                return None
            key = url[len(prefix) :]
            if key == "core.js":
                return script_source(SDK.parent / "mvu-core.js"), "text/javascript"
            match = re.fullmatch(r"(messages|variables)/(\d+)", key)
            if not match or int(match[2]) >= len(messages):
                return None
            body = json.dumps(
                (messages if match[1] == "messages" else variables)[int(match[2])], ensure_ascii=False
            )
            return body, "application/json"

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
            if is_initvar(entry):
                entry["content"] = (
                    entry.get("content", "")
                    .replace("{{user}}", persona)
                    .replace("{{char}}", normalize(document).get("name", ""))
                )
        scripts = scripts_override if scripts_override is not None else card_scripts(document)
        frames = None
        if not eager and not scripts:
            # Without card scripts nothing observable runs before message frames,
            # so preparing their markup first matches the realm's own order.
            frames = list(message_frames(history, prepare_markup)) if allowed else []
            greeting = history[0]["raw"] if opening and history else ""
            if not frames and initialize_without_realm(data, greeting):
                return DeferredSession(self, data, resource, prefix, len(messages), host)
        context = await self.backend.new_context(resource)
        try:
            return await self._open_context(
                context,
                prefix,
                len(messages),
                data,
                document,
                history,
                scripts,
                frames,
                allowed,
                host,
                prepare_markup,
                persona,
                opening,
            )
        except BaseException:
            # Setup failures and cancellation before install have the same lifetime
            # boundary as script failures. No orphan browser contexts may survive.
            await context.close()
            raise

    async def _start(self, context, prefix, history_length, host, data):
        # The page runs no script until runtime is bound below, so boundary never sees it unset.
        async def boundary(method, args):
            if method == "event":
                return await runtime.dispatch(args["name"], args["args"])
            return await host(method, args)

        page = await context.open_page(
            "<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline' data:; connect-src 'none'\"></head><body></body></html>",
            boundary,
        )
        runtime = RuntimeSession(context, page, prefix, history_length, self)
        await page.add_script(script_source(SDK))
        await page.add_script(script_source(SDK.parent / "mvu-core.js"))
        await runtime.call("install", data, install=True)
        return runtime

    async def _open_context(
        self,
        context,
        prefix,
        history_length,
        data,
        document,
        history,
        scripts,
        frames,
        allowed,
        host,
        prepare_markup,
        persona,
        opening,
    ):
        runtime = await self._start(context, prefix, history_length, host, data)
        try:
            if scripts and not allowed:
                raise ValueError("此卡包含角色脚本，请在角色资料中启用脚本权限")
            if allowed:
                await runtime.call("runScripts", scripts)
                # Render message-local callbacks in server-owned sandboxes as well.
                # DOM closures stay in their original realm; mutable event arguments round-trip.
                for index, html in frames if frames is not None else message_frames(history, prepare_markup):
                    await runtime.add_message_frame(
                        html,
                        {
                            **{k: v for k, v in data.items() if k != "messages"},
                            "serverMain": False,
                            "currentMessage": index,
                        },
                        host,
                    )
            greeting = history[0]["raw"] if opening and history else ""
            greeting = greeting.replace("{{user}}", persona).replace(
                "{{char}}", normalize(document).get("name", "")
            )
            await runtime.call("initMvu", greeting)
            return runtime
        except BaseException:
            await runtime.close()
            raise


def message_frames(history, prepare_markup):
    """Yield (index, html) for history messages that mount a server-side frame."""
    for index, message in enumerate(history):
        html = message.get("display", message.get("raw", ""))
        if prepare_markup and "<" in html:
            html = prepare_markup(html)
        if "<script" in html.lower():
            yield index, html


# generateSchema({}) in vendor/mvu/schema.ts when no previous schema exists.
EMPTY_SCHEMA = {"type": "object", "properties": {}, "extensible": False, "recursiveExtensible": False}
INITVAR_TAG = re.compile(r"<initvar\b[^>]*>[\s\S]*?</initvar\s*>", re.I)


def initialize_without_realm(data, greeting):
    """Apply initMvu in place when it provably changes nothing but defaults.

    Mirrors mvu.ts ``initialize`` for a card without InitVar entries or opening
    tags and with empty MVU data. Returns False when the realm must run it.
    """
    state = data["state"]
    if (
        any(is_initvar(entry) for entry in data["lore"])
        or INITVAR_TAG.search(greeting)
        or state.get("stat_data") not in (None, {})
        or not all(
            isinstance(state.get(key), (dict, type(None))) for key in ("initialized_lorebooks", "schema")
        )
    ):
        return False
    # JavaScript ||= assigns in place, so missing keys append in the same order.
    for key, default in (("stat_data", {}), ("initialized_lorebooks", {}), ("schema", EMPTY_SCHEMA)):
        if state.get(key) is None:
            state[key] = copy.deepcopy(default)
    if data["messages"]:
        data["messages"][-1]["variables"] = copy.deepcopy(state)
    return True


class DeferredSession:
    """Realm-equivalent session for a card whose JavaScript has nothing to do yet.

    snapshot/update and the identity cases of transform/process follow runtime.ts on a
    Python mirror. Any other call starts the realm and installs the current mirror:
    with no scripts, listeners or message frames, that is exactly the state an eager
    realm would hold at this point.
    """

    def __init__(self, owner, mirror, resource, prefix, history_length, host):
        self.owner, self.mirror, self.resource = owner, mirror, resource
        self.prefix, self.history_length, self.host = prefix, history_length, host
        self.realm = None

    async def _realm(self):
        if self.realm is None:
            context = await self.owner.backend.new_context(self.resource)
            try:
                self.realm = await self.owner._start(
                    context, self.prefix, self.history_length, self.host, self.mirror
                )
            except BaseException:
                await context.close()
                raise
        return self.realm

    async def call(self, name, *args):
        if self.realm is None:
            if name == "snapshot":
                return copy.deepcopy(self.mirror)
            if name == "update":
                self.mirror = copy.deepcopy(args[0])
                return None
            if name == "transform" and isinstance(args[1], list):
                if all(isinstance(rule, dict) and rule.get("disabled") for rule in args[1]):
                    return args[0]
            if name == "process" and len(args) >= 3 and args[1] == [] and args[2] == "":
                # No operations keep the state; MESSAGE_RECEIVED has no listener.
                if self.mirror["messages"]:
                    self.mirror["messages"][-1]["variables"] = copy.deepcopy(self.mirror["state"])
                return {"text": args[0], **copy.deepcopy(self.mirror)}
        return await (await self._realm()).call(name, *args)

    async def add_message_frame(self, html, data, host):
        await (await self._realm()).add_message_frame(html, data, host)

    async def close(self):
        if self.realm is not None:
            await self.realm.close()


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
                    self.owner.document_bytes -= len(old.encode("utf-8")) + sum(
                        len(d.encode("utf-8")) for d in docs
                    )
        elif self.owner:
            self.owner.documents.move_to_end(html)
        for document in documents:
            if "<script" in document.lower():
                capabilities = await self.call("frameCapabilities", document)
                if capabilities["displayOnly"]:
                    continue
                document = await self.call(
                    "snapshotMacros", document, data.get("state", {}), data.get("scopes", {})
                )
                document = document.replace("{{user}}", data.get("persona", "用户")).replace(
                    "{{char}}", data.get("character", "")
                )
                await self._add_message_frame(document, data, host)

    async def _add_message_frame(self, html, data, host):
        frame = await self.context.open_page(
            f"<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {self.prefix}; style-src 'unsafe-inline' data:; connect-src {self.prefix}\"></head><body></body></html>",
            host,
        )
        await frame.add_script(script_source(SDK))
        await frame.evaluate(
            """({data,prefix,length})=>{
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
        }""",
            {"data": data, "prefix": self.prefix, "length": self.history_length},
        )
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
        # install returns the live helper API (functions, schemas and BigInts).
        # It stays inside the realm; callers only need installation to finish,
        # and Android's JSON transport must not serialize that unused object.
        expression = (
            "data => { TavernRuntime.install(data, (m,a) => __tavernHost(m,a)); }"
            if install
            else "({name,args}) => TavernRuntime[name](...args)"
        )
        payload = args[0] if install else {"name": name, "args": args}
        # Full Agent patches may invoke MVU callbacks in message-local realms.
        # Restore every mirror on failure, not just the server's stat_data.
        patch_call = name == "process" and len(args) >= 3 and args[2] == "" and bool(args[1])
        checkpoints = (
            [
                (page, await page.evaluate("() => TavernRuntime.checkpoint()"))
                for page in [self.page, *self.frames]
            ]
            if patch_call
            else []
        )
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
