"""Startup consent and model jobs, with local mocked model responses (no downloads)."""
import asyncio
import json
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def main():
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        config = store.settings()
        config.retrieval.channels = ["keyword", "embedding"]
        config.local_models.reranker_enabled = True
        store.save_settings(config)
        card = store.import_card(json.dumps({"name": "测试", "first_mes": "欢迎"}).encode(), "test.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                status = {"startup_id": "boot-1", "models": {
                    purpose: {"ready": False, "downloaded": True, "model": purpose}
                    for purpose in ("embedding", "reranker")}, "jobs": []}
                calls = []

                async def model_status(route):
                    await route.fulfill(json=status)

                async def warmup(route):
                    purpose = route.request.url.split("/")[-2]
                    calls.append(purpose)
                    job = {"id": purpose, "kind": "warmup:" + purpose, "status": "complete"}
                    status["jobs"].append(job)
                    await route.fulfill(json=job)

                await page.route("**/api/local-models", model_status)
                await page.route("**/api/local-models/*/warmup", warmup)
                url = f"http://127.0.0.1:{port}"
                await page.goto(url)
                prompt = page.get_by_role("heading", name="预热本地检索模型？")
                await expect(prompt).to_be_visible()
                await page.get_by_role("button", name="暂不预热", exact=True).click()
                await page.reload()
                await expect(page.get_by_role("button", name="设置", exact=True)).to_be_visible()
                await expect(prompt).to_have_count(0)
                assert calls == []

                status["startup_id"] = "boot-2"
                await page.reload()
                await expect(prompt).to_be_visible()
                await page.get_by_role("button", name="现在预热", exact=True).click()
                await expect(page.get_by_text("Embedding：预热完成", exact=True)).to_be_visible()
                await expect(page.get_by_text("Reranker：预热完成", exact=True)).to_be_visible()
                assert sorted(calls) == ["embedding", "reranker"]
                await page.get_by_role("button", name="完成", exact=True).click()

                status["startup_id"] = "boot-3"
                status["models"]["embedding"]["downloaded"] = False
                status["models"]["reranker"]["ready"] = True
                await page.reload()
                await expect(prompt).to_be_visible()
                await expect(page.get_by_text("尚未下载", exact=False)).to_be_visible()
                await expect(page.get_by_role("button", name="现在预热", exact=True)).to_have_count(0)
                await page.get_by_role("button", name="暂不预热", exact=True).click()

                config.retrieval.channels = ["keyword"]
                config.local_models.reranker_enabled = False
                store.save_settings(config)
                status["startup_id"] = "boot-4"
                await page.evaluate("id => localStorage.setItem('tavern-session', id)", session["id"])
                await page.reload()
                await expect(page.locator('section[aria-label="MVU 变量"]')).to_be_visible()
                await expect(page.locator('section[aria-label="MVU 变量"] > details')).not_to_have_attribute("open", "")
                await expect(prompt).to_have_count(0)
                assert not errors, errors
                await browser.close()
                print("Startup warmup and collapsed MVU variables: passed")
        finally:
            server.should_exit = True
            thread.join(10)


if __name__ == "__main__":
    asyncio.run(main())
