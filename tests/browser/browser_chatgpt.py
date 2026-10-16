"""Browser acceptance against real local OAuth callbacks and synthetic signed OpenAI responses."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tests.backend.chatgpt_fixtures import ACCESS, CLIENT, REFRESH, IdentityFixture
from tests.browser.select_menu import choose_menu, choose_model, menu_option

OUTPUT = Path(".agents/.local/chatgpt-validation")


async def viewport_ok(page, label):
    result = await page.evaluate("""() => ({width:innerWidth,height:innerHeight,
        pageWidth:document.documentElement.scrollWidth,pageHeight:document.documentElement.scrollHeight,
        bodyWidth:document.body.scrollWidth,bodyHeight:document.body.scrollHeight})""")
    assert result["pageWidth"] <= result["width"] and result["bodyWidth"] <= result["width"], (label, result)
    assert result["pageHeight"] <= result["height"] and result["bodyHeight"] <= result["height"], (label, result)


async def settings_page(page, page_id):
    group, key = page_id.split(".")
    groups = {"shared": "通用", "full_agent": "full agent", "half_agent": "half agent"}
    labels = {"providers": "提供商", "model": "模型与生成", "editor": "角色卡编辑 agent"}
    dialog = page.get_by_role("dialog", name="设置", exact=True)
    if not await dialog.is_visible():
        settings = page.get_by_role("button", name="设置", exact=True)
        if not await settings.is_visible():
            for name in ("切换左侧栏", "展开左侧栏"):
                toggle = page.get_by_role("button", name=name, exact=True)
                if await toggle.is_visible():
                    await toggle.click()
                    break
        await settings.click()
    await expect(dialog.locator(".settings-layout")).to_be_visible()
    selector = dialog.get_by_role("combobox", name="设置页面", exact=True)
    if await selector.is_visible():
        await dialog.locator(".settings-mode-buttons").get_by_role("button", name=groups[group], exact=True).click()
        await selector.select_option(page_id)
    else:
        await dialog.locator(".settings-pages").get_by_role("button", name=groups[group] + " " + labels[key], exact=True).click()
    return dialog


async def finish_login(page, identity, *, client_id=None):
    params = identity.params
    issued = client_id or (CLIENT if params["client_id"] == "dynamic_agent_client" else params["client_id"])
    response = await page.request.get(params["redirect_uri"], params={"code": "browser-fixture-code",
                                       "state": params["state"], "client_id": issued})
    return response


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with tempfile.TemporaryDirectory(dir=OUTPUT, prefix="isolated-") as directory:
        app = create_app(Path(directory))
        identity = IdentityFixture()
        identity.catalog[0].update(default_reasoning_level="low", supported_reasoning_levels=[
            {"effort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")])
        accounts = app.state.store.chatgpt
        accounts.http_transport, accounts.browser_open = httpx.MockTransport(identity), identity.browser
        doc = app.state.store.settings_document()
        doc.shared.network.api_proxy = False
        app.state.store.save_settings(doc)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(viewport={"width": 1920, "height": 1080})
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base)
                dialog = await settings_page(page, "shared.providers")
                height = (await dialog.bounding_box())["height"]
                await dialog.get_by_role("tab", name="ChatGPT 官方", exact=True).click()
                await dialog.get_by_role("button", name="连接 ChatGPT 账号", exact=True).click()
                provider = dialog.locator(".provider-editor").last
                await provider.get_by_label("名称", exact=True).fill("ChatGPT 订阅测试")
                before = app.state.store.settings_document()
                await provider.get_by_role("button", name="Continue with ChatGPT", exact=True).click()
                await expect(provider.get_by_text("请在系统浏览器中完成授权。十分钟内有效。", exact=False)).to_be_visible()
                assert (await finish_login(page, identity)).status == 200
                welcome = page.get_by_role("dialog", name="正在使用 ChatGPT 订阅", exact=True)
                await expect(welcome).to_be_visible()
                assert await dialog.evaluate("element => element.inert || element.closest('[inert]') !== null")
                await welcome.get_by_role("button", name="知道了", exact=True).click()
                await provider.locator("summary").click()
                await expect(provider.get_by_text("已连接 · 已启用 ChatGPT 订阅使用", exact=True)).to_be_visible()
                assert abs((await dialog.bounding_box())["height"] - height) < 1
                current = app.state.store.settings_document()
                assert current.full_agent.model == before.full_agent.model
                assert current.half_agent == before.half_agent
                assert current.shared.card_editor == before.shared.card_editor
                checks.append("new registration, protected callback, welcome, unchanged Agent choices")

                await provider.get_by_role("button", name="重新登录此账号", exact=True).click()
                await expect(provider.get_by_role("button", name="取消登录", exact=True)).to_be_visible()
                identity.claim_overrides["sub"] = "wrong-person"
                assert (await finish_login(page, identity)).status == 400
                await expect(provider.get_by_text("ChatGPT 身份校验失败", exact=False)).to_be_visible()
                identity.claim_overrides.clear()
                assert accounts.public()["accounts"][0]["plan_enabled"]
                checks.append("failed reauthorization preserves existing connection")

                await provider.get_by_role("button", name="Continue with ChatGPT", exact=True).click()
                await expect(provider.get_by_role("button", name="取消登录", exact=True)).to_be_visible()
                await provider.get_by_role("button", name="取消登录", exact=True).click()
                await expect(provider.get_by_text("登录已取消。", exact=True)).to_be_visible()
                checks.append("login cancellation")

                identity.token_overrides["scope"] = "openid profile email offline_access"
                await provider.get_by_role("button", name="Continue with ChatGPT", exact=True).click()
                await expect(provider.get_by_role("button", name="取消登录", exact=True)).to_be_visible()
                assert (await finish_login(page, identity, client_id="oaiapp_browser_second")).status == 200
                await expect(provider.get_by_text("已登录 · 未启用订阅使用权限", exact=True)).to_be_visible()
                await provider.get_by_role("button", name="启用订阅使用", exact=True).click()
                await expect(provider.get_by_role("button", name="取消登录", exact=True)).to_be_visible()
                assert identity.params["prompt"] == "consent"
                identity.token_overrides.clear()
                assert (await finish_login(page, identity)).status == 200
                await expect(provider.get_by_text("已连接 · 已启用 ChatGPT 订阅使用", exact=True)).to_be_visible()
                assert not await welcome.is_visible()
                checks.append("multiple registrations, explicit plan consent, welcome shown once")

                identity.revocation_status = 503
                await provider.get_by_role("button", name="退出账号", exact=True).click()
                await expect(provider.get_by_text("已在本地退出，但未确认远程撤销", exact=False)).to_be_visible()
                await expect(provider.get_by_text("已退出，请重新登录", exact=True)).to_be_visible()
                identity.revocation_status = 200
                await provider.get_by_role("button", name="重新登录此账号", exact=True).click()
                await expect(provider.get_by_role("button", name="取消登录", exact=True)).to_be_visible()
                assert "id_token_hint" not in identity.params
                assert (await finish_login(page, identity)).status == 200
                await expect(provider.get_by_text("已连接 · 已启用 ChatGPT 订阅使用", exact=True)).to_be_visible()
                checks.append("logout and unconfirmed revocation feedback, returning login")
                markup = await page.content()
                assert all(secret not in markup for secret in (ACCESS, REFRESH, "fixture-id-token-only"))

                dialog = await settings_page(page, "full_agent.model")
                await dialog.get_by_role("tab", name="ChatGPT 官方", exact=True).click()
                await dialog.get_by_role("button", name="配置模型连接", exact=True).click()
                await choose_menu(dialog.get_by_role("button", name="提供商", exact=True), label="ChatGPT 订阅测试")
                await expect(dialog.get_by_label("输出预留预算（tokens）", exact=True)).to_be_visible()
                assert not await dialog.get_by_label("接口方式", exact=True).count()
                assert not await dialog.get_by_label("温度 temperature", exact=True).count()
                await dialog.get_by_role("button", name="更新模型列表", exact=True).click()
                await expect(dialog.get_by_text("已更新 1 个模型，列表已缓存。", exact=True)).to_be_visible()
                await choose_model(dialog.get_by_role("button", name="模型名称", exact=True), "fixture")
                for _ in range(50):
                    if app.state.store.settings_document().full_agent.official_model.model == "fixture":
                        break
                    await asyncio.sleep(0.1)
                assert app.state.store.settings_document().full_agent.official_model.model == "fixture"
                effort = dialog.get_by_role("button", name="思考强度", exact=True)
                await choose_menu(effort, "xhigh")
                await expect(effort).to_have_attribute("data-value", "xhigh")
                await expect(await menu_option(effort, "xhigh")).to_have_text("超高（xhigh）")
                await expect(await menu_option(effort, "ultra")).to_have_attribute("aria-disabled", "true")
                assert await (await menu_option(effort, "none")).count() == 0
                assert app.state.store.settings_document().full_agent.model == before.full_agent.model
                checks.append("official reasoning levels and selectable xhigh, unchanged ordinary API model")
                await expect(dialog.get_by_text("使用 ChatGPT 订阅", exact=True)).to_be_visible()
                checks.append("account catalogue, fixed protocol, unsupported controls, output budget label")
                await context.close()

                for width, height, scale, label in [(2560, 1440, 1, "desktop-2560"),
                    (1920, 1080, 1, "desktop-1920"), (360, 792, 3, "mobile-1080x2376"),
                    (1280, 720, 2, "desktop-2560-200pct"), (960, 540, 2, "desktop-1920-200pct")]:
                    context = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale)
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base)
                    dialog = await settings_page(page, "shared.providers")
                    await dialog.get_by_role("tab", name="ChatGPT 官方", exact=True).click()
                    provider = dialog.locator(".provider-editor").last
                    await provider.locator("summary").click()
                    await provider.get_by_role("button", name="Continue with ChatGPT", exact=True).scroll_into_view_if_needed()
                    await viewport_ok(page, label + ":providers")
                    await page.screenshot(path=str(OUTPUT / f"{label}-providers.png"))
                    initial = (await dialog.bounding_box())["height"]
                    await settings_page(page, "full_agent.model")
                    await expect(dialog.get_by_label("输出预留预算（tokens）", exact=True)).to_be_visible()
                    await viewport_ok(page, label + ":model")
                    assert abs((await dialog.bounding_box())["height"] - initial) < 1
                    await page.screenshot(path=str(OUTPUT / f"{label}-model.png"))
                    checks.append(label + ": no root overflow, stable settings height")
                    await context.close()
                await browser.close()
            assert not errors, errors
            (OUTPUT / "report.json").write_text(json.dumps({"checks": checks, "page_errors": errors,
                "boundary": "Synthetic signed OAuth/OpenAI HTTP responses; real browser, callback listener, secure storage and application UI."},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"passed": len(checks), "output": str(OUTPUT)}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive(), "Fixture server did not stop"


if __name__ == "__main__":
    asyncio.run(main())
