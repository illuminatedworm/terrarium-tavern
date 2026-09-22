"""Manual archive controls and edit notices against an isolated local app."""

import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest


async def main():
    screenshots = Path(".cache/scene-ui")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        settings = store.settings()
        settings.foreground.model = settings.background.model = "fixture"
        settings.providers[0].api_key = "local"
        store.save_settings(settings)
        card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        calls = []

        class ArchiveProvider:
            async def chat_complete(self, messages, **kwargs):
                calls.append(messages)
                await asyncio.sleep(1.2)
                source = json.loads(messages[-1]["content"])["messages"][0]["id"]
                if "剧情档案整理员" in messages[0]["content"]:
                    result = {"narrative": "旅人将钥匙交给乙", "facts": [{"text": "乙持有钥匙", "source_ids": [source]}]}
                else:
                    result = {"memories": [{"content": "乙持有钥匙", "source": source, "kind": "fact"}]}
                return SimpleNamespace(content=json.dumps(result, ensure_ascii=False), usage={})

            async def close(self):
                pass

        app.state.service.memory_provider_factory = ArchiveProvider
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
                await asyncio.sleep(.1)
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.add_init_script(f"if(!localStorage.getItem('tavern-session'))localStorage.setItem('tavern-session',{json.dumps(session['id'])});")
                    await page.goto(address)
                    await page.locator('.story').wait_for()
                    await page.get_by_role("button", name="场景", exact=True).click()
                    summary = page.get_by_role("button", name="重新生成剧情摘要", exact=True)
                    memory = page.get_by_role("button", name="重新生成记忆", exact=True)
                    await expect(summary).to_be_visible()

                    async def delayed_request(route):
                        await asyncio.sleep(.7)
                        await route.continue_()

                    await page.route("**/scene/*/regenerate", delayed_request)
                    for button in (summary, memory):
                        await button.click()
                        await expect(summary).to_be_disabled()
                        await expect(memory).to_be_disabled()
                        await expect(button).to_have_attribute("aria-busy", "true")
                        await expect(button).to_have_text("更新中…")
                        await expect(button.locator('.scene-update-spinner')).to_be_visible()
                        await button.evaluate("button => { button.click(); button.click(); }")
                        await page.screenshot(path=str(screenshots / "updating.png"))
                        if button is memory:
                            await page.get_by_role('button',name='后台工作记录',exact=True).click()
                            await expect(page.locator('.work-turn').first.locator(':scope > summary strong')).to_have_text('重新生成记忆')
                            await expect(page.locator('.work-status').first).to_have_text('运行中')
                        await expect(button).to_be_enabled(timeout=10000)
                        if button is memory:
                            await expect(page.locator('.work-status').first).to_have_text('已完成')
                            await page.get_by_role('button',name='返回对话',exact=True).click()
                    assert len(calls) == 2
                    latest = store.session(session["id"])
                    assert len(latest["messages"]) == 1
                    assert latest["snapshot"]["memory"][0]["source"] == session["head"]
                    await page.screenshot(path=str(screenshots / "desktop.png"))

                    async def edit(text):
                        await page.locator('.message-actions').get_by_role("button", name="编辑", exact=True).click()
                        await page.locator('.inline-message-editor textarea').fill(text)
                        await page.get_by_role("button", name="保存修改", exact=True).click()
                        await expect(page.locator('.edit-notice')).to_be_visible()

                    await edit("旅人将钥匙交给乙。")
                    await page.screenshot(path=str(screenshots / "notice.png"))
                    await page.wait_for_timeout(3000)
                    await edit("旅人明确将钥匙交给乙。")
                    await page.wait_for_timeout(3000)
                    await expect(page.locator('.edit-notice')).to_be_visible()
                    await expect(page.locator('.edit-notice')).to_be_hidden(timeout=3000)
                    assert store.session(session["id"])["snapshot"]["memory"][0]["source"] == session["head"]
                    payload_requests=[]
                    page.on('request',lambda request: payload_requests.append(request.url) if '/work-records/' in request.url and request.url.rsplit('/',1)[-1].isdigit() else None)
                    for i in range(23):
                        tid,_=store.create_turn(session["id"],TurnRequest(operation_id=f"history-{i}",expected_revision=latest['revision']+2,allow_content_edit=True,text=f"整理设定 {i}"))
                        store.event(tid,'maintenance_started',{})
                        store.event(tid,'provider_request',{'purpose':'maintenance','model':'fixture','messages':[{'role':'user','content':'LARGE_RECORD_CONTENT_'*300}],'tools':['read_state']})
                        store.event(tid,'maintenance_completed',{'record':'设定已整理'})
                        store.event(tid,'committed',{})
                        store.set_turn(tid,status='committed')
                    await page.get_by_role('button',name='后台工作记录',exact=True).click()
                    await expect(page.locator('.work-turn')).to_have_count(20)
                    assert not payload_requests
                    assert await page.locator('.work-events').count()==0
                    first=page.locator('.work-turn').first
                    await first.locator(':scope > summary').click()
                    await expect(first.locator('.work-event')).to_have_count(4)
                    assert not payload_requests
                    request=first.locator('.work-event').filter(has_text='模型请求')
                    await request.locator(':scope > summary').click()
                    await expect(request.locator('.work-data-root')).to_be_visible()
                    assert len(payload_requests)==1
                    assert await request.locator('pre').count()==0
                    inputs=request.locator('.work-data-branch').filter(has_text='输入消息').first
                    await inputs.locator(':scope > summary').click()
                    item=inputs.locator('.work-data-branch').first
                    await item.locator(':scope > summary').click()
                    content=item.locator('.work-data-branch').filter(has_text='内容').first
                    await content.locator(':scope > summary').click()
                    await expect(content.locator('pre')).to_contain_text('LARGE_RECORD_CONTENT_')
                    await page.screenshot(path=str(screenshots / 'work-records.png'))
                    await page.get_by_role('button',name='加载更早回合',exact=True).click()
                    await expect(page.locator('.work-turn')).to_have_count(25)
                    await page.get_by_role('button',name='展开回合',exact=True).click()
                    await page.get_by_role('button',name='收起回合',exact=True).click()
                    await expect(page.locator('.work-events')).to_have_count(0)
                    await page.get_by_role('button',name='返回对话',exact=True).click()
                    await expect(page.locator('.messages')).to_be_visible()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                    await expect(summary).to_be_visible()
                    await page.screenshot(path=str(screenshots / "narrow.png"))
                    await page.get_by_role('button',name='折叠右侧栏',exact=True).click()
                    await page.get_by_role('button',name='后台工作记录',exact=True).click()
                    await expect(page.locator('.work-turn')).to_have_count(20)
                    await page.screenshot(path=str(screenshots / 'work-records-narrow.png'))
                    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    await page.set_viewport_size({'width':1440,'height':1000})
                    await page.get_by_role('button',name='展开左侧栏',exact=True).click()
                    await page.get_by_role('tab',name='角色卡',exact=True).click()
                    await page.locator('.card-side-item').first.click()
                    await expect(page.locator('.editor-page')).to_be_visible()
                    assert '版本' not in await page.locator('.editor-page').inner_text()
                    assert '版本' not in await page.locator('.card-side-list').inner_text()
                    await page.get_by_label('角色描述',exact=True).fill('角色卡覆盖保存检查。')
                    await page.get_by_role('button',name='保存角色',exact=True).click()
                    await expect(page.get_by_text('角色卡已保存。',exact=True)).to_be_visible()
                    assert 'version' not in store.card(card['id'])
                    assert store.card(card['id'])['document']['data']['description']=='角色卡覆盖保存检查。'
                    await page.screenshot(path=str(screenshots / 'card-current.png'))
                    await page.locator('.sidebar-bottom').get_by_role('button',name='设置',exact=False).click()
                    await page.get_by_role('button',name='上下文结构编排',exact=True).click()
                    global_editor=page.get_by_role('region',name='全局上下文结构编排',exact=True)
                    await global_editor.get_by_label('添加内容块来源',exact=True).select_option('custom')
                    await global_editor.get_by_role('button',name='＋ 添加',exact=True).click()
                    await global_editor.get_by_label('块标识',exact=True).fill('global_ui_note')
                    await global_editor.get_by_label('自定义注入文本',exact=True).fill('全局上下文编排检查')
                    await page.get_by_role('button',name='系统提示词',exact=True).click()
                    await page.get_by_role('button',name='上下文结构编排',exact=True).click()
                    await expect(global_editor.get_by_label('自定义注入文本',exact=True)).to_have_value('全局上下文编排检查')
                    await page.screenshot(path=str(screenshots / 'global-recipe.png'))
                    await page.set_viewport_size({'width':390,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    await page.screenshot(path=str(screenshots / 'global-recipe-narrow.png'))
                    await page.set_viewport_size({'width':1440,'height':1000})
                    await global_editor.get_by_role('button',name='保存全局编排',exact=True).click()
                    await expect(page.get_by_role('dialog')).to_be_hidden()
                    global_recipe=store.settings().context_recipe.model_dump()
                    created=store.new_session(SessionCreate(card_id=card['id']))
                    assert created['recipe']==global_recipe
                    await page.evaluate('(id)=>localStorage.setItem("tavern-session",id)',created['id'])
                    await page.reload()
                    await page.locator('.story').wait_for()
                    await page.get_by_role('button',name='展开右侧栏',exact=True).click()
                    await page.get_by_role('button',name='会话设置',exact=True).click()
                    await page.get_by_role('button',name='编辑上下文配方',exact=True).click()
                    dialog=page.get_by_role('dialog')
                    await dialog.locator('.recipe-select').filter(has_text='自定义文本').click()
                    await dialog.get_by_label('自定义注入文本',exact=True).fill('仅当前对话的上下文编排')
                    await dialog.get_by_role('button',name='保存配方',exact=True).click()
                    await expect(dialog).to_be_hidden()
                    customized=store.session(created['id'])
                    assert customized['recipe']!=global_recipe
                    assert store.settings().context_recipe.model_dump()==global_recipe
                    inherited=store.fork_session(customized['id'],customized['head'],customized['revision'])
                    assert inherited['recipe']==customized['recipe']
                    await page.get_by_role('button',name='恢复当前全局编排',exact=True).click()
                    await expect(page.get_by_role('status').filter(has_text='已采用当前全局编排')).to_be_visible()
                    assert store.session(created['id'])['recipe']==global_recipe
                    assert store.session(inherited['id'])['recipe']==customized['recipe']
                    await page.get_by_role('tab',name='角色卡',exact=True).click()
                    await page.locator('.card-side-row').first.hover()
                    await page.locator('.card-delete-action').first.click()
                    await page.get_by_role('button',name='确认删除角色卡',exact=True).click()
                    await expect(page.locator('.card-side-item')).to_have_count(0)
                    assert store.cards()==[]
                    with store.connection() as db:
                        assert db.execute('SELECT count(*) FROM cards').fetchone()[0]==0
                    await page.evaluate('(id)=>localStorage.setItem("tavern-session",id)',created['id'])
                    await page.reload()
                    await page.locator('.story').wait_for()
                    await expect(page.locator('.story-heading h1')).to_have_text(card['name'])
                    assert store.session_card(created['id'])['document']['data']['description']=='角色卡覆盖保存检查。'
                    await page.screenshot(path=str(screenshots / 'deleted-card-story.png'))
                    assert not errors, errors
                    print("Passed: scene controls, work records, physical card deletion, surviving stories, global/default/custom/fork recipes and responsive layouts.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)
            sock.close()


if __name__ == "__main__":
    asyncio.run(main())
