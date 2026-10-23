"""Android WebView realms in a disposable process, bridged to the Python event loop.

Java only transports JSON and renders pages. Resource/host capabilities and the
authoritative script lifecycle remain here. Cancellation kills the backend's realm
process, invalidating all of its mirrors, so a looping script cannot hold the host UI
or a close operation. Story scripts use :realm and tools use :realm_tools, so a tool
timeout never ends a story turn's pages.
"""

import asyncio
import base64
import json
import uuid


class AndroidRealmError(RuntimeError):
    pass


class AndroidPage:
    def __init__(self, context, host, on_error):
        self.context = context
        self.id = uuid.uuid4().hex
        self.host = host
        self.on_error = on_error
        self.closed = False

    async def evaluate(self, script, arg=None):
        if self.closed or self.context.closed:
            raise AndroidRealmError("脚本页面已关闭")
        return await self.context.backend.request("evaluate", page=self.id, script=script, arg=arg)

    async def add_script(self, source):
        if self.closed or self.context.closed:
            raise AndroidRealmError("脚本页面已关闭")
        await self.context.backend.request("script", page=self.id, source=source)

    async def texts(self, selector):
        return await self.evaluate(
            "selector => Array.from(document.querySelectorAll(selector), element => element.textContent)",
            selector,
        )

    async def close(self):
        if self.closed or self.context.closed:
            return
        self.closed = True
        self.context.pages.pop(self.id, None)
        self.context.backend.cancel_hosts(self.id)
        await self.context.backend.request("close_page", page=self.id, timeout=2)


class AndroidContext:
    def __init__(self, backend, resource):
        self.backend = backend
        self.resource = resource
        self.id = uuid.uuid4().hex
        self.pages = {}
        self.closed = False

    async def open_page(self, html, host, on_error=None):
        if self.closed:
            raise AndroidRealmError("脚本环境已关闭")
        page = AndroidPage(self, host, on_error)
        self.pages[page.id] = page
        try:
            await self.backend.request("open", context=self.id, page=page.id, html=html)
        except BaseException:
            await page.close()
            raise
        return page

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.backend.contexts.pop(self.id, None)
        for page in self.pages.values():
            self.backend.cancel_hosts(page.id)
        self.pages.clear()
        await self.backend.request("close_context", context=self.id, timeout=2)


class AndroidBackend:
    def __init__(self, *, tools=False):
        self.tools = tools
        self.client = None
        self.callback = None
        self.loop = None
        self.pending = {}
        self.contexts = {}
        self.host_tasks = {}
        self.sequence = 0

    def start(self):
        if self.client is not None:
            return
        # Chaquopy is not imported on desktop or until a realm is actually needed.
        from java import dynamic_proxy, jclass

        backend = self
        self.loop = asyncio.get_running_loop()

        class Callback(dynamic_proxy(jclass("com.terrarium.tavern.RealmCallback"))):
            def onMessage(self, message):
                if backend.callback is self and not backend.loop.is_closed():
                    backend.loop.call_soon_threadsafe(backend.received, str(message), self)

        self.callback = Callback()
        application = jclass("com.chaquo.python.Python").getPlatform().getApplication()
        self.client = jclass("com.terrarium.tavern.RealmClient")(application, self.callback, self.tools)

    async def new_context(self, resource):
        self.start()
        context = AndroidContext(self, resource)
        self.contexts[context.id] = context
        try:
            await self.request("context", context=context.id)
        except BaseException:
            self.contexts.pop(context.id, None)
            raise
        return context

    async def request(self, command, *, timeout=30, **payload):
        self.sequence += 1
        request = str(self.sequence)
        future = self.loop.create_future()
        self.pending[request] = future
        self.client.request(
            json.dumps({"command": command, "request": request, **payload}, ensure_ascii=False)
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            self.release_client(reset=True)
            self.invalidate("卡片脚本被取消或运行超时，隔离进程已重置")
            raise
        finally:
            self.pending.pop(request, None)

    def received(self, raw, callback):
        # A Java callback can already be queued on this loop when cancellation
        # resets its process. It cannot invalidate a replacement client's pages.
        if callback is not self.callback:
            return
        message = json.loads(raw)
        kind = message["type"]
        if kind == "disconnected":
            self.release_client()
            self.invalidate(message["error"])
        elif kind == "result":
            future = self.pending.get(message["request"])
            if future is not None and not future.done():
                if message.get("error") is not None:
                    future.set_exception(AndroidRealmError(message["error"]))
                else:
                    future.set_result(message.get("value"))
        elif kind == "pageerror":
            for context in self.contexts.values():
                page = context.pages.get(message["page"])
                if page and page.on_error:
                    page.on_error(message["error"])
                    break
        elif kind == "resource":
            try:
                context = self.contexts.get(message["context"])
                value = context.resource(message["url"], message["method"]) if context else None
                if value is not None:
                    body, mime = value
                    binary = isinstance(body, bytes)
                    value = {
                        "body": base64.b64encode(body).decode("ascii") if binary else body,
                        "binary": binary,
                        "mime": mime,
                    }
                self.reply(message, value)
            except Exception as error:
                self.reply(message, error=str(error))
        elif kind == "host":
            task = asyncio.create_task(self.host(message))
            self.host_tasks[task] = message["page"]
            task.add_done_callback(lambda done: self.host_tasks.pop(done, None))

    def reply(self, message, value=None, error=None):
        if self.client is not None and message["context"] in self.contexts:
            self.client.request(
                json.dumps(
                    {"command": "callback", "callback": message["callback"], "value": value, "error": error},
                    ensure_ascii=False,
                )
            )

    async def host(self, message):
        try:
            context = self.contexts.get(message["context"])
            page = context.pages.get(message["page"]) if context else None
            if page is None or page.closed:
                raise AndroidRealmError("脚本页面已关闭")
            self.reply(message, await page.host(message["method"], message.get("args")))
        except asyncio.CancelledError:
            self.reply(message, error="脚本页面已关闭")
            raise
        except Exception as error:
            self.reply(message, error=str(error))

    def cancel_hosts(self, page):
        for task, owner in self.host_tasks.items():
            if owner == page:
                task.cancel()

    def invalidate(self, reason):
        for context in self.contexts.values():
            context.closed = True
        self.contexts.clear()
        for task in self.host_tasks:
            task.cancel()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(AndroidRealmError(reason))

    def release_client(self, *, reset=False):
        if self.client is not None:
            if reset:
                self.client.reset()
            self.client.close()
            self.client = self.callback = None

    async def close(self):
        try:
            for context in list(self.contexts.values()):
                await context.close()
        finally:
            self.invalidate("脚本环境已关闭")
            if self.host_tasks:
                await asyncio.gather(*self.host_tasks, return_exceptions=True)
            self.release_client()
