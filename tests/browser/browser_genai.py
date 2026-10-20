"""Google GenAI parameter visibility, saved-value roundtrips and responsive layouts."""

import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tests.browser.select_menu import choose_model


async def main():
    output = Path(".cache/genai-browser")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tavern-genai-") as root:
        app = create_app(root)
        store = app.state.store
        doc = store.settings_document()
        profile = doc.full_agent.model
        profile.api_mode, profile.model = "gemini", "gemini-4-argon"
        profile.temperature, profile.top_p, profile.top_k = 0.42, 0.8, 32
        profile.cache.mode, profile.cache.preset, profile.cache.retention = "runtime", "custom", "24h"
        profile.cache.field = "legacy_marker"
        profile.cache.request = {"legacy_option": "preserve"}
        store.save_settings(doc)
        original = profile.model_dump()
        card = store.import_card('{"name":"GenAI 验证","first_mes":"开场。"}'.encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    for width, height, dpr, name in [
                        (2560, 1440, 1, "wide"),
                        (1920, 1080, 1, "desktop"),
                        (360, 792, 3, "mobile"),
                        (960, 540, 2, "zoom200-equivalent"),
                    ]:
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=dpr
                        )
                        page = await context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        await page.evaluate('sid=>localStorage.setItem("tavern-session",sid)', session["id"])
                        await page.reload()
                        if width <= 760:
                            await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                        await page.get_by_role("tab", name="缓存", exact=True).click()
                        await expect(page.get_by_label("缓存时效档位", exact=True)).to_have_count(0)
                        await expect(page.get_by_label("缓存跟进方式", exact=True)).to_have_count(0)
                        await expect(
                            page.get_by_text("当前 Gemini 模型仅支持隐式缓存", exact=False)
                        ).to_be_visible()
                        if width <= 760:
                            await page.get_by_role("button", name="折叠右侧栏", exact=True).click()
                            await page.get_by_role("button", name="切换左侧栏", exact=True).click()
                        await page.get_by_role("button", name="设置", exact=True).click()
                        dialog = page.get_by_role("dialog", name="设置", exact=True)
                        if width <= 760:
                            await (
                                page.locator(".settings-mode-buttons")
                                .get_by_role("button", name="full agent", exact=True)
                                .click()
                            )
                            await page.get_by_role("combobox", name="设置页面", exact=True).select_option(
                                "full_agent.model"
                            )
                        else:
                            await page.get_by_role("button", name="full agent 模型与生成", exact=True).click()
                        model = dialog.get_by_role("button", name="模型名称", exact=True)
                        protocol = dialog.get_by_label("接口方式", exact=True)
                        advanced = dialog.locator("details").filter(
                            has=page.get_by_text("更多参数：采样细项、分词器与提示缓存", exact=True)
                        )
                        if await advanced.get_attribute("open") is None:
                            await advanced.locator("summary").click()
                        initial_height = await dialog.evaluate("e=>e.getBoundingClientRect().height")

                        async def saved():
                            await expect(page.locator(".settings-save-status")).to_contain_text("已保存")

                        async def sampling(count):
                            for label in ("温度 temperature", "核采样 top_p", "候选数 top_k"):
                                await expect(dialog.get_by_label(label, exact=True)).to_have_count(count)

                        await sampling(0)
                        await expect(dialog.get_by_label("缓存控制", exact=True)).to_have_count(0)
                        await expect(dialog.get_by_label("参数格式", exact=True)).to_have_count(0)
                        note = dialog.get_by_text("当前 Gemini 模型仅支持云端隐式缓存", exact=False)
                        await note.scroll_into_view_if_needed()
                        await expect(note).to_be_visible()
                        await page.screenshot(path=str(output / f"{name}-gemini4.png"))

                        for model_name, visible in [
                            ("gemini-3.8-flash", 0),
                            ("gemini-3.5-flash-lite", 0),
                            ("gemini-2.5-flash", 1),
                        ]:
                            await choose_model(model, model_name)
                            await saved()
                            await sampling(visible)
                            await expect(dialog.get_by_label("缓存控制", exact=True)).to_have_value("runtime")
                            await expect(dialog.get_by_label("断点字段名", exact=True)).to_have_count(0)
                            assert (
                                abs(
                                    await dialog.evaluate("e=>e.getBoundingClientRect().height")
                                    - initial_height
                                )
                                < 1
                            )
                        await expect(dialog.get_by_label("温度 temperature", exact=True)).to_have_value(
                            "0.42"
                        )
                        # Switching back exposes the actual saved custom values, not a hidden replacement.
                        await protocol.select_option("chat_completions")
                        await saved()
                        await expect(dialog.get_by_label("断点字段名", exact=True)).to_have_value(
                            "legacy_marker"
                        )
                        assert (
                            "preserve"
                            in await dialog.get_by_label(
                                "附加请求参数（JSON，可留 {}）", exact=True
                            ).input_value()
                        )
                        await protocol.select_option("gemini")
                        await choose_model(model, "gemini-4-argon-" + "long-deployment-name-" * 10)
                        await saved()
                        await sampling(0)
                        await note.scroll_into_view_if_needed()
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        assert await page.evaluate("document.documentElement.scrollHeight <= innerHeight")
                        assert (
                            abs(await dialog.evaluate("e=>e.getBoundingClientRect().height") - initial_height)
                            < 1
                        )
                        await page.screenshot(path=str(output / f"{name}-long-model.png"))
                        await choose_model(model, "gemini-4-argon")
                        await saved()
                        restored = store.settings_document().full_agent.model.model_dump()
                        for key in ("temperature", "top_p", "top_k", "cache"):
                            assert restored[key] == original[key]
                        assert not errors, errors
                        await context.close()
                    print(
                        "GenAI settings, sidebar, saved values, long content and 4 DPR/layout cases passed."
                    )
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
