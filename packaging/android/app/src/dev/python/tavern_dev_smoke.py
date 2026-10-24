"""Small on-device probes, bundled only in the separate development application."""

import asyncio
import importlib
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def native_dependencies():
    importlib.import_module("tiktoken_ext.openai_public")
    from tavern.core.tokens import count_text
    from tavern.search.retrieval import tokenize

    assert count_text("本地分词验证") > 0
    assert tokenize("灯塔 テスト")


async def sessions(root, checks):
    from httpx import ASGITransport, AsyncClient

    from tavern.application.api import create_app

    app = create_app(root)
    # Lifespan closes jobs, realm clients and storage before TemporaryDirectory removes the fixture.
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            document = {
                "name": "Android 自检",
                "first_mes": "欢迎 {{user}}。",
                "character_book": {
                    "entries": [{"id": 0, "comment": "[InitVar]", "content": "hp: 7", "enabled": False}]
                },
            }
            imported = await client.post(
                "/api/cards/import",
                files={"file": ("smoke.json", json.dumps(document).encode(), "application/json")},
            )
            imported.raise_for_status()
            card_id = imported.json()["id"]
            for mode in ("half_agent", "full_agent"):
                start = time.perf_counter()
                created = await client.post(
                    "/api/sessions", json={"card_id": card_id, "agent_mode": mode, "persona": "自检玩家"}
                )
                created.raise_for_status()
                session = created.json()
                assert session["snapshot"]["state"]["stat_data"]["hp"] == 7
                reopened = await client.get("/api/sessions/" + session["id"])
                reopened.raise_for_status()
                assert reopened.json()["snapshot"]["state"] == session["snapshot"]["state"]
                checks.append({"name": mode + "_session", "seconds": round(time.perf_counter() - start, 3)})
                print("PASS: " + mode + "_session", flush=True)


async def timed(checks, name, probe):
    start = time.perf_counter()
    await probe()
    checks.append({"name": name, "seconds": round(time.perf_counter() - start, 3)})
    print("PASS: " + name, flush=True)


async def message_frame():
    from tavern.scripting.runtime import ScriptRuntime

    # The frame receives its HTML and inline code as string arguments; quotes,
    # newlines and markup must arrive as data rather than as bridge source.
    html = (
        '<div id="status" title="它说：\'你好\'">等待</div>\n'
        '<script>\nconst label = "帧脚本\\"已运行\\"";\n'
        'document.getElementById("status").textContent = label;\n</script>'
    )
    snapshot = {"state": {"stat_data": {"hp": 7}}, "scopes": {}}
    history = [{"id": "greeting", "role": "assistant", "raw": html, "snapshot": snapshot}]

    async def host(method, args):
        return None

    runtime = ScriptRuntime()
    try:
        session = await runtime.open({"name": "帧脚本自检", "first_mes": html}, history, snapshot, True, host)
        try:
            assert len(session.frames) == 1
            assert await session.frames[0].texts("#status") == ['帧脚本"已运行"']
        finally:
            await session.close()
    finally:
        await runtime.close()


async def display_assertions(root):
    import re

    import tavern
    from tavern.character.editor_sandbox import test_card
    from tavern.storage.store import Store

    # The same published recipe and assertions as the desktop display regression.
    guide = Path(tavern.__file__).parent / "skills/adapt-card-to-tavern/references/regex-display.md"
    rule = json.loads(re.findall(r"```json\n([\s\S]*?)\n```", guide.read_text("utf-8"))[0])
    rule["replaceString"] = (
        rule["replaceString"]
        .replace("<script>", "<script>document.addEventListener('DOMContentLoaded', () => {")
        .replace("</script>", "});</script>")
    )
    document = {
        "name": "正则前端夹具",
        "first_mes": "客人来到柜台。",
        "alternate_greetings": [],
        "character_book": {"entries": [{"comment": "[InitVar]", "enabled": False, "content": "Money: '0'"}]},
        "extensions": {"regex_scripts": [rule]},
    }
    value = '</script><div id="injected">文本</div>'
    result = await test_card(
        Store(root),
        {"ref": "C1", "document": document},
        samples=["```\n普通叙事代码块\n```\n客人进门。\n"],
        display_assertions=[{"sample_index": 1, "selector": "#money", "text": "0", "after_text": value}],
        state_patch=[{"op": "replace", "path": "/Money", "value": value}],
    )
    assert result["status"] == "passed", result
    assert result["checks"][-1]["assertions"][0]["actual"] == value


async def dependency_bundle(root):
    from tavern.scripting.bundler import DependencyBundler
    from tavern.scripting.realm import default_backend

    module = root / "entry.js"
    module.write_text("export const answer = 42;", "utf-8")
    source = (
        "import {answer} from 'https://cards.example/entry.js'; import _ from 'lodash';"
        "globalThis.moduleResult = [answer, _.tag];"
    )
    async with DependencyBundler() as bundler:
        output = await bundler.bundle(source, {"https://cards.example/entry.js": module})
        try:
            await bundler.bundle("import 'https://cards.example/missing.js';", {})
        except ValueError as error:
            assert "Unresolved dependency" in str(error), error
        else:
            raise AssertionError("缺失模块必须报错")
    backend = default_backend(tools=True)
    try:
        context = await backend.new_context(lambda *_: None)

        async def host(*_):
            raise AssertionError("打包结果不应调用宿主能力")

        page = await context.open_page("<html><head></head><body></body></html>", host)
        await page.add_script("globalThis._ = {tag: 'lodash'};")
        await page.add_script(output)
        assert await page.evaluate("moduleResult") == [42, "lodash"]
    finally:
        await backend.close()


async def scripts(root, checks):
    await timed(checks, "message_frame", message_frame)
    await timed(checks, "display_assertions", lambda: display_assertions(root))
    await timed(checks, "dependency_bundle", lambda: dependency_bundle(root))


def office_fixtures(root):
    import zipfile

    from openpyxl import Workbook

    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    parts = {
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Default Extension="xml" ContentType="application/xml"/>
          <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
        </Types>""",
        "_rels/.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
        </Relationships>""",
        "word/_rels/document.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
        </Relationships>""",
        "word/styles.xml": f'<w:styles xmlns:w="{namespace}"><w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/></w:style></w:styles>',
        "word/document.xml": f'<w:document xmlns:w="{namespace}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>世界设定</w:t></w:r></w:p>'
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>姓名</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>职业</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>林澈</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>航海士</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>文档完整尾部</w:t></w:r></w:p></w:body></w:document>",
    }
    docx = root / "资料.docx"
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value.encode("utf-8"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "人物"
    sheet.append(["姓名", "职业"])
    sheet.append(["林澈", "航海士"])
    xlsx = root / "资料.xlsx"
    workbook.save(xlsx)
    return docx, xlsx


async def office_documents(root, checks):
    from tavern.character.editor_files import read_document

    docx, xlsx = office_fixtures(root)

    async def word():
        # The Card Author tool path: the disposable :documents process does the parsing.
        result = await read_document(str(docx))
        assert result["converter"] == "mammoth + markdownify", result
        assert "# 世界设定" in result["markdown"] and "林澈" in result["markdown"]
        assert "文档完整尾部" in result["markdown"]

    async def sheet():
        result = await read_document(str(xlsx))
        assert result["converter"] == "openpyxl", result
        assert "## 人物" in result["markdown"] and "| 林澈 | 航海士 |" in result["markdown"]

    await timed(checks, "read_document_docx", word)
    await timed(checks, "read_document_xlsx", sheet)


async def local_models(root, cache_dir, checks):
    from tavern.android.local_models import SOURCES, mobile_config
    from tavern.core.models import Settings
    from tavern.search.local_models import create_local_model_manager
    from tavern.search.retrieval_config import LocalModel

    started = time.perf_counter()
    manager = create_local_model_manager(SimpleNamespace(root=root))
    settings = Settings()
    for purpose in ("embedding", "reranker"):
        setattr(settings.local_models, purpose, LocalModel(**mobile_config(purpose)))
    settings.local_models.embedding.batch_size = 2
    settings.local_models.reranker.batch_size = 2
    documents = ["北岸港口的守卫阿舟住在灯塔二层，每晚巡视码头。", "南山茶园的采茶季从四月开始。"]
    query = "北岸港口的守卫住在哪里？"
    try:
        for purpose in ("embedding", "reranker"):
            print("IMPORT: " + purpose, flush=True)
            path = Path(cache_dir) / "model-fixtures" / SOURCES[purpose]["filename"]
            with path.open("rb") as source:

                async def chunks():
                    while block := source.read(1024 * 1024):
                        yield block

                await manager.import_model(purpose, chunks(), settings, size=path.stat().st_size)
            print("WARMUP: " + purpose, flush=True)
            await manager.warmup(purpose, settings)
        assert all(item["ready"] for item in manager.status(settings)["models"].values())
        vectors = await manager.infer("embedding", "embed", documents, settings, timeout=45)
        assert len(vectors) == 2 and all(len(row) == 1024 for row in vectors)
        # Exercise native last-token pooling across the 512-token microbatch boundary.
        long_text = documents[0] * 35
        assert len(await manager.servers["embedding"].tokenize(long_text)) > 512
        long_vector = await manager.infer("embedding", "embed", [long_text], settings, timeout=45)
        assert len(long_vector[0]) == 1024
        settings.retrieval.chunk_tokens = 128
        settings.retrieval.chunk_overlap = 16
        indexed = await manager.infer(
            "embedding",
            "index",
            [{"id": "fixture", "keys": ["码头"], "content": documents[0] * 10}],
            settings,
            timeout=45,
        )
        assert len(indexed[0]["fragments"]) >= 2
        assert len(indexed[0]["vectors"]) == len(indexed[0]["fragments"])
        scores = await manager.infer(
            "reranker", "rerank", [[query, [text]] for text in documents], settings, timeout=45
        )
        assert len(scores) == 2 and 0 <= scores[1] < scores[0] <= 1
    finally:
        await manager.close()
    assert not manager.servers and not manager.ready
    checks.append(
        {
            "name": "local_models",
            "seconds": round(time.perf_counter() - started, 3),
            "indexed_fragments": len(indexed[0]["fragments"]),
            "rerank_scores": scores,
        }
    )
    print("PASS: local_models", flush=True)


def run(cache_dir, suite):
    # "restart" runs in DevSmokeInstrumentation; Python only validates the name here.
    if suite not in {"all", "native", "sessions", "scripts", "documents", "restart", "models"}:
        raise ValueError("未知自检范围：" + suite)
    checks = []
    started = time.perf_counter()
    if suite in {"all", "native"}:
        start = time.perf_counter()
        native_dependencies()
        checks.append({"name": "native_dependencies", "seconds": round(time.perf_counter() - start, 3)})
        print("PASS: native_dependencies", flush=True)
    if suite in {"all", "sessions"}:
        # No reference to the persistent story directory or any user model credentials.
        with tempfile.TemporaryDirectory(prefix="tavern-smoke-", dir=cache_dir) as temporary:
            asyncio.run(sessions(Path(temporary), checks))
    if suite in {"all", "scripts"}:
        with tempfile.TemporaryDirectory(prefix="tavern-script-smoke-", dir=cache_dir) as temporary:
            asyncio.run(scripts(Path(temporary), checks))
    if suite in {"all", "documents"}:
        with tempfile.TemporaryDirectory(prefix="tavern-document-smoke-", dir=cache_dir) as temporary:
            asyncio.run(office_documents(Path(temporary), checks))
    if suite == "models":
        # Deliberately opt-in: normal daily checks do not require or load model weights.
        with tempfile.TemporaryDirectory(prefix="tavern-model-smoke-", dir=cache_dir) as temporary:
            asyncio.run(local_models(Path(temporary), cache_dir, checks))
    return json.dumps({"ok": True, "checks": checks, "seconds": round(time.perf_counter() - started, 3)})
