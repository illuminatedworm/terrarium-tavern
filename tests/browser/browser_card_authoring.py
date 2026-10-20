"""Standalone multi-card authoring acceptance, entirely local and isolated."""

import asyncio
import copy
import json
import re
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from threading import Thread

import uvicorn
from PIL import Image
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.character.editor_attachments import attach_file
from tavern.storage.store import dump
from tests.backend.test_card_author import ToolLLM, fixture


async def main():
    output = Path(".cache/card-authoring-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="card-authoring-ui-") as root:
        app = create_app(root)
        store, editor = app.state.store, app.state.editor
        card = store.import_card(dump(fixture()).encode(), "fixture.json")
        second = fixture()
        second["name"] = "参考卡乙"
        store.import_card(dump(second).encode(), "second.json")
        editor.provider_factory = lambda: ToolLLM([])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role("tab", name="制卡", exact=True).click()
                    await expect(page.get_by_role("region", name="制卡对话入口")).to_be_visible()
                    assert editor.edits.conversations() == []
                    await page.reload()
                    await expect(page.get_by_role("region", name="制卡对话入口")).to_be_visible()
                    assert editor.edits.conversations() == []
                    await page.get_by_role("button", name="新建制卡对话", exact=True).click()
                    composer = page.get_by_role("textbox", name="制卡要求", exact=True)
                    await expect(composer).to_be_visible()
                    await expect(page.get_by_role("button", name="内置技能", exact=True)).to_have_count(1)
                    await composer.fill("先给一个方案，不制作")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await expect(page.locator(".cw-message")).to_have_count(2)
                    wid = editor.edits.conversations()[0]["id"]
                    assert editor.edits.conversation(wid)["cards"] == []
                    editor.provider_factory = lambda: ToolLLM(
                        [
                            (
                                "create_draft",
                                {"name": "新书店店主", "proposal_ref": "H2", "confirmation_ref": "H3"},
                            ),
                            (
                                "patch_draft",
                                {
                                    "card_ref": "C1",
                                    "operations": [
                                        {
                                            "op": "replace",
                                            "path": "/data/description",
                                            "value": "神秘的书店店主",
                                        },
                                        {"op": "replace", "path": "/data/first_mes", "value": "铜铃响了。"},
                                        {
                                            "op": "replace",
                                            "path": "/data/mes_example",
                                            "value": "{{char}}：请进。",
                                        },
                                    ],
                                    "comment": "制作",
                                },
                            ),
                        ]
                    )
                    await composer.fill("确认方案，制作")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await expect(page.locator(".cw-message")).to_have_count(4)
                    await expect(
                        page.locator(".cw-inspector-footer").get_by_role(
                            "button", name="保存新卡", exact=True
                        )
                    ).to_be_visible()
                    await expect(page.get_by_role("button", name="审阅修改", exact=True)).to_have_count(0)
                    await (
                        page.get_by_role("navigation", name="卡片信息")
                        .get_by_role("button", name=re.compile("修改"))
                        .click()
                    )
                    await expect(page.get_by_role("button", name="保存新卡", exact=True)).to_be_enabled()
                    await composer.fill("保存时保留我的未发送输入")
                    await page.get_by_role("button", name="保存新卡", exact=True).click()
                    await expect(page.get_by_role("button", name="保存角色卡", exact=True)).to_be_enabled()
                    await expect(composer).to_have_value("保存时保留我的未发送输入")
                    assert len(store.cards()) == 3
                    await composer.fill("@")
                    picker = page.get_by_role("listbox", name="引用角色", exact=True)
                    await expect(picker).to_be_visible()
                    await composer.fill("@测试卡")
                    await composer.press("Tab")
                    await expect(composer).to_have_value("")
                    editor.provider_factory = lambda: ToolLLM(
                        [
                            ("read_field", {"card_ref": "C2", "path": "/description"}),
                            (
                                "patch_draft",
                                {
                                    "card_ref": "C2",
                                    "operations": [
                                        {"op": "replace", "path": "/description", "value": "UI 修改结果"}
                                    ],
                                    "comment": "修改",
                                },
                            ),
                        ]
                    )
                    await composer.fill("修改 C2 的描述")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await expect(page.locator(".cw-message")).to_have_count(6)
                    assert await page.locator(".cw-message.assistant").evaluate_all("""messages => messages.every(message =>
                        message.previousElementSibling?.matches('.cw-run-sequence') &&
                        message.previousElementSibling.dataset.runId === message.dataset.messageRun)""")
                    await (
                        page.get_by_role("navigation", name="卡片信息")
                        .get_by_role("button", name="预览", exact=True)
                        .click()
                    )
                    await expect(page.get_by_role("region", name="当前草稿阅读预览")).to_contain_text(
                        "UI 修改结果"
                    )
                    await expect(
                        page.locator(".cw-inspector-footer").get_by_role(
                            "button", name="保存角色卡", exact=True
                        )
                    ).to_be_visible()
                    await expect(page.get_by_role("button", name="保存角色卡", exact=True)).to_have_count(1)
                    await expect(page.get_by_role("button", name="另存副本", exact=True)).to_have_count(1)
                    await composer.fill("保留我的额外要求")
                    await page.locator(".cw-skills-button").click()
                    skills = page.get_by_role("dialog", name="内置技能", exact=True)
                    await expect(skills).to_contain_text("适配项目工作流")
                    await (
                        skills.locator(".skill-choice")
                        .filter(has_text="迁移正文状态栏")
                        .get_by_role("button", name="填入请求")
                        .click()
                    )
                    await expect(composer).to_have_value(
                        re.compile("保留我的额外要求.*关于角色卡「测试卡」.*状态栏", re.S)
                    )
                    await expect(page.locator(".cw-message")).to_have_count(6)
                    await composer.fill("")
                    await (
                        page.get_by_role("navigation", name="卡片信息")
                        .get_by_role("button", name=re.compile("修改"))
                        .click()
                    )
                    assert (
                        editor.edits.conversation(wid)["cards"][1]["document"]["description"] == "UI 修改结果"
                    )
                    assert store.card(card["id"])["document"] == card["document"]
                    newer = copy.deepcopy(card["document"])
                    newer["description"] = "外部新版"
                    store.edit_card(card["id"], newer, 0)
                    await page.get_by_role("button", name="保存角色卡", exact=True).click()
                    await expect(page.get_by_role("alert").first).to_contain_text("原卡已改变")
                    await page.get_by_role("button", name="另存副本", exact=True).click()
                    await expect(page.locator(".cw-notice", has_text="角色卡已保存")).to_be_visible()
                    await page.reload()
                    await expect(composer).to_be_visible()
                    await expect(
                        page.get_by_role("combobox", name="选择卡片").locator("option")
                    ).to_have_count(2)
                    assert editor.edits.conversations()[0]["id"] == wid

                    class Slow(ToolLLM):
                        async def chat(self, messages, **kwargs):
                            await asyncio.sleep(30)
                            async for chunk in super().chat(messages, **kwargs):
                                yield chunk

                    class SlowAfterWrite(ToolLLM):
                        async def chat(self, messages, **kwargs):
                            if self.call_count == 2:
                                await asyncio.sleep(30)
                            async for chunk in super().chat(messages, **kwargs):
                                yield chunk

                    editor.provider_factory = lambda: SlowAfterWrite(
                        [
                            ("read_field", {"card_ref": "C2", "path": "/description"}),
                            (
                                "patch_draft",
                                {
                                    "card_ref": "C2",
                                    "operations": [
                                        {
                                            "op": "replace",
                                            "path": "/description",
                                            "value": "尚未完成的实时预览",
                                        }
                                    ],
                                    "comment": "验证预览同步",
                                },
                            ),
                        ]
                    )
                    await composer.fill("检查未完成草稿同步")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await (
                        page.get_by_role("navigation", name="卡片信息")
                        .get_by_role("button", name="预览", exact=True)
                        .click()
                    )
                    await page.get_by_role("checkbox", name="查看本轮未完成版本", exact=True).check()
                    await expect(page.get_by_role("region", name="当前草稿阅读预览")).to_contain_text(
                        "尚未完成的实时预览"
                    )
                    await expect(page.get_by_role("button", name="打开表单编辑", exact=True)).to_be_disabled()
                    await expect(page.get_by_role("button", name="保存角色卡", exact=True)).to_be_disabled()
                    await expect(page.get_by_role("button", name="另存副本", exact=True)).to_be_disabled()
                    await page.get_by_role("button", name="停止", exact=True).click()
                    await expect(page.get_by_role("button", name="继续本轮", exact=True)).to_be_visible()
                    await page.get_by_role("button", name="丢弃本轮", exact=True).click()
                    await page.get_by_role("dialog").get_by_role("button", name="确认", exact=True).click()
                    await expect(page.get_by_role("region", name="当前草稿阅读预览")).not_to_contain_text(
                        "尚未完成的实时预览"
                    )

                    editor.provider_factory = lambda: Slow([])
                    await composer.fill("检查卡片")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await expect(page.get_by_role("button", name="停止", exact=True)).to_be_visible()
                    await page.reload()
                    await expect(page.get_by_role("button", name="停止", exact=True)).to_be_visible()
                    await page.get_by_role("button", name="停止", exact=True).click()
                    await expect(page.get_by_role("button", name="继续本轮", exact=True)).to_be_visible()
                    await page.get_by_role("button", name="丢弃本轮", exact=True).click()
                    await page.get_by_role("dialog").get_by_role("button", name="确认", exact=True).click()
                    await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()

                    for width, height, name in (
                        (2560, 1440, "desktop-2560"),
                        (1920, 1080, "desktop-1920"),
                        (360, 792, "mobile-css-dpr3"),
                        (960, 540, "zoom-200-equivalent"),
                    ):
                        await page.set_viewport_size({"width": width, "height": height})
                        if (
                            width < 760
                            and await page.get_by_role("button", name="折叠左侧栏", exact=True).is_visible()
                        ):
                            await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                        await page.screenshot(path=str(output / f"{name}.png"))
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                        ), name
                        await expect(composer).to_be_visible()
                        if width <= 1100:
                            await page.get_by_role("button", name="卡片与修改", exact=True).click()
                            await expect(
                                page.get_by_role("button", name="返回对话", exact=True)
                            ).to_be_visible()
                            await page.get_by_role("button", name="返回对话", exact=True).click()
                    await page.set_viewport_size({"width": 1920, "height": 1080})
                    await (
                        page.get_by_role("navigation", name="卡片信息")
                        .get_by_role("button", name="数据", exact=True)
                        .click()
                    )
                    await page.get_by_role("button", name="打开表单编辑", exact=True).click()
                    form = page.get_by_role("dialog", name="编辑角色卡草稿", exact=True)
                    await expect(form).to_be_visible()
                    before = await form.bounding_box()
                    await form.get_by_role("button", name="原始 JSON", exact=True).click()
                    after = await form.bounding_box()
                    assert abs(before["height"] - after["height"]) < 2
                    raw = form.get_by_role("textbox", name="原始 JSON", exact=True)
                    baseline = json.loads(await raw.input_value())
                    baseline_cards = copy.deepcopy(editor.edits.conversation(wid)["cards"])
                    await raw.fill("{invalid form draft")
                    await form.get_by_role("button", name="恢复原稿状态", exact=True).click()
                    await form.get_by_role("button", name="确认放弃修改", exact=True).click()
                    await expect(form.get_by_role("button", name="确认放弃修改", exact=True)).to_have_count(0)
                    await expect(form.get_by_role("button", name="恢复原稿状态", exact=True)).to_be_disabled()
                    assert json.loads(await raw.input_value()) == baseline
                    assert editor.edits.conversation(wid)["cards"] == baseline_cards
                    assert (
                        await page.evaluate(
                            "prefix=>Object.keys(sessionStorage).filter(key=>key.startsWith(prefix))",
                            f"tavern-card-draft-{wid}-",
                        )
                        == []
                    )
                    updated = copy.deepcopy(baseline)
                    updated.get("data", updated)["creator_notes"] = "表单保存检查"
                    await raw.fill(json.dumps(updated, ensure_ascii=False))
                    selected_ref = await page.get_by_role(
                        "combobox", name="选择卡片", exact=True
                    ).input_value()
                    endpoint = f"**/api/card-author/conversations/{wid}/drafts/{selected_ref}"
                    await page.route(
                        endpoint,
                        lambda route: route.fulfill(
                            status=409,
                            content_type="application/json",
                            body='{"detail":"测试保存冲突"}',
                        ),
                    )
                    await form.get_by_role("button", name="保存草稿", exact=True).click()
                    await expect(form).to_be_visible()
                    await expect(form.get_by_role("alert").filter(has_text="测试保存冲突")).to_be_visible()
                    assert await page.evaluate(
                        "key=>sessionStorage.getItem(key)!==null",
                        f"tavern-card-draft-{wid}-{selected_ref}",
                    )
                    assert editor.edits.conversation(wid)["cards"] == baseline_cards
                    await page.unroute(endpoint)
                    await form.get_by_role("button", name="保存草稿", exact=True).click()
                    await expect(form).to_have_count(0)
                    assert (
                        next(
                            item
                            for item in editor.edits.conversation(wid)["cards"]
                            if item["ref"] == selected_ref
                        )["document"]
                        == updated
                    )
                    assert (
                        await page.evaluate(
                            "key=>sessionStorage.getItem(key)",
                            f"tavern-card-draft-{wid}-{selected_ref}",
                        )
                        is None
                    )
                    await page.get_by_role("button", name="打开表单编辑", exact=True).click()
                    await expect(form).to_be_visible()
                    await expect(form.get_by_role("button", name="恢复原稿状态", exact=True)).to_be_disabled()
                    await form.get_by_role("button", name="关闭", exact=True).click()
                    await expect(page.get_by_role("button", name="工具与上下文", exact=True)).to_have_count(0)
                    await expect(page.get_by_label("制卡上下文用量")).to_contain_text("tokens")
                    await expect(page.get_by_label("制卡上下文用量")).to_contain_text("缓存命中率")
                    await expect(page.get_by_role("button", name="对话模型", exact=True)).to_be_visible()
                    await expect(page.get_by_role("button", name="本次思考强度", exact=True)).to_be_visible()
                    cover = BytesIO()
                    Image.new("RGB", (24, 32), (120, 80, 40)).save(cover, "PNG")
                    attach_file(
                        editor.edits,
                        wid,
                        editor.edits.conversation(wid)["revision"],
                        "封面.png",
                        cover.getvalue(),
                    )
                    await page.reload()
                    inspector = page.get_by_role("navigation", name="卡片信息")
                    await inspector.get_by_role("button", name="数据", exact=True).click()
                    await page.get_by_role("combobox", name="头像").select_option(label="封面.png")
                    await expect(page.get_by_role("button", name="移除头像", exact=True)).to_be_visible()
                    await inspector.get_by_role("button", name="预览", exact=True).click()
                    await expect(page.locator(".draft-preview-heading img")).to_have_attribute(
                        "src", re.compile(r"/portraits/[0-9a-f]{64}$")
                    )
                    await page.wait_for_function(
                        "() => { const image = document.querySelector('.draft-preview-heading img');"
                        " return image && image.complete && image.naturalWidth === 24; }"
                    )
                    await inspector.get_by_role("button", name="数据", exact=True).click()
                    await page.get_by_role("button", name="移除头像", exact=True).click()
                    await expect(page.get_by_role("button", name="移除头像", exact=True)).to_have_count(0)
                    mobile = await browser.new_page(
                        viewport={"width": 360, "height": 792}, device_scale_factor=3
                    )
                    await mobile.add_init_script(
                        "localStorage.setItem('tavern-sidebar-section','editor');"
                        + f"localStorage.setItem('tavern-active-card-conversation','{wid}');"
                    )
                    await mobile.goto(address)
                    await expect(mobile.get_by_role("textbox", name="制卡要求", exact=True)).to_be_visible()
                    assert await mobile.evaluate("devicePixelRatio") == 3
                    await mobile.screenshot(path=str(output / "mobile-1080x2376.png"))
                    assert await mobile.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                    )
                    await mobile.close()
                    assert not errors, errors
                    print(
                        "Card authoring browser acceptance passed: create, references, save/conflict, recovery, forms, portraits, responsive layouts."
                    )
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 5)


if __name__ == "__main__":
    asyncio.run(main())
