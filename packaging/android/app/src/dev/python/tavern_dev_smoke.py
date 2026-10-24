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
            # Android's initial empty child may precede document-start injection.
            # The response sandbox must already make that Window inaccessible.
            for page in (session.page, session.frames[0]):
                assert await page.evaluate("""() => {
                    history.replaceState(null, '', location.href.split('#')[0] + '#probe');
                    const child = document.createElement('iframe');
                    document.body.append(child);
                    try { void child.contentWindow.RTCPeerConnection; return false; }
                    catch (error) { return error.name === 'SecurityError'; }
                    finally { child.remove(); }
                }""")
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


async def local_models(root, cache_dir, checks, selected="models"):
    import math
    import re

    from tavern.android.local_models import SOURCES, mobile_config
    from tavern.core.models import Settings
    from tavern.search.local_models import create_local_model_manager
    from tavern.search.retrieval_config import LocalModel

    manager = create_local_model_manager(SimpleNamespace(root=root))
    settings = Settings()
    for purpose in ("embedding", "reranker"):
        setattr(settings.local_models, purpose, LocalModel(**mobile_config(purpose)))
    documents = [
        "北岸港口的守卫阿舟住在灯塔二层，每晚巡视码头。",
        "南山茶园的采茶季从四月开始，雨后需要及时晾晒茶叶。",
        "北岸港口的铁匠铺靠近东门，铁匠白天修理船锚。",
    ]
    query = "北岸港口的守卫住在哪里？"
    reference_path = Path(cache_dir) / "model-fixtures/cpu-reference.json"
    report_path = Path(cache_dir) / "model-fixtures/acceleration-report.json"
    reference = None
    if selected != "models":
        reference = json.loads(reference_path.read_text("utf-8"))
        assert reference["engine"] == SOURCES["engine"]["commit"]
    devices = (
        ("cpu", "opencl:0", "hexagon:0")
        if selected == "models"
        else ("opencl:0" if selected == "models-opencl" else "hexagon:0",)
    )

    def memory():
        result = {}
        for purpose, server in manager.servers.items():
            status = Path(f"/proc/{server.process.getPid()}/status").read_text()
            result[purpose] = {
                key: int(re.search(r"^" + key + r":\s+(\d+)", status, re.MULTILINE)[1])
                for key in ("VmRSS", "VmHWM")
            }
        return result

    async def measure(row, name, operation):
        start = time.perf_counter()
        value = await operation
        row[name + "_seconds"] = round(time.perf_counter() - start, 3)
        print(f"MODEL: {row['device']} {name} {row[name + '_seconds']}s", flush=True)
        return value

    try:
        await manager.initialize()
        checks.append({"name": "model_devices", "devices": manager.devices})
        for purpose in ("embedding", "reranker"):
            path = Path(cache_dir) / "model-fixtures" / SOURCES[purpose]["filename"]
            with path.open("rb") as source:

                async def chunks():
                    while block := source.read(1024 * 1024):
                        yield block

                await manager.import_model(purpose, chunks(), settings, size=path.stat().st_size)
        for device in devices:
            row = {"name": "local_models", "device": device, "ok": False}
            checks.append(row)
            try:
                for purpose in ("embedding", "reranker"):
                    getattr(settings.local_models, purpose).device = device
                    await measure(row, purpose + "_load", manager.warmup(purpose, settings))
                row["execution"] = dict(manager.metrics)
                row["loaded_memory_kib"] = memory()
                assert all(item["ready"] for item in manager.status(settings)["models"].values())
                vectors = await measure(
                    row, "embedding", manager.infer("embedding", "embed", documents, settings, timeout=120)
                )
                query_vector = await manager.infer(
                    "embedding", "embed", [query], settings, timeout=120, query=True
                )
                long_text = documents[0] * 35
                assert len(await manager.servers["embedding"].tokenize(long_text)) > 512
                long_vector = await measure(
                    row,
                    "long_embedding",
                    manager.infer("embedding", "embed", [long_text], settings, timeout=120),
                )
                scores = await measure(
                    row,
                    "rerank",
                    manager.infer(
                        "reranker", "rerank", [[query, [text]] for text in documents], settings, timeout=120
                    ),
                )
                row["rerank_scores"] = scores
                similarities = [
                    sum(a * b for a, b in zip(query_vector[0], vector, strict=True)) for vector in vectors
                ]
                row["embedding_similarities"] = similarities
                assert similarities.index(max(similarities)) == 0, "向量检索未选中已知相关文档"
                if device != "cpu":
                    prefix_vectors = [*vectors, *query_vector, *long_vector]
                    row["short_cosines"] = [
                        sum(a * b for a, b in zip(left, right, strict=True))
                        / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
                        for left, right in zip(prefix_vectors, reference["vectors"][:5], strict=True)
                    ]
                    (Path(cache_dir) / "model-fixtures" / (device.split(":")[0] + "-short.json")).write_text(
                        json.dumps({"vectors": prefix_vectors, "scores": scores}), "utf-8"
                    )
                assert all(0 <= score <= 1 for score in scores) and scores[0] > max(scores[1:]), (
                    "重排顺序不符"
                )
                entry = {"id": "fixture", "keys": ["码头"], "content": documents[0] * 1000}
                indexed = await measure(
                    row, "long_index", manager.infer("embedding", "index", [entry], settings, timeout=360)
                )
                row["indexed_characters"] = len(entry["content"])
                row["indexed_fragments"] = len(indexed[0]["fragments"])
                assert len(indexed[0]["vectors"]) == row["indexed_fragments"] > 2
                row["inferred_memory_kib"] = memory()
                seen, repeated = {}, []
                for fragment, vector in zip(indexed[0]["fragments"], indexed[0]["vectors"], strict=True):
                    if fragment in seen:
                        previous = seen[fragment]
                        repeated.append(
                            sum(a * b for a, b in zip(previous, vector, strict=True))
                            / math.sqrt(sum(a * a for a in previous) * sum(b * b for b in vector))
                        )
                    else:
                        seen[fragment] = vector
                row["repeat_min_cosine"] = min(repeated)
                assert min(repeated) >= 0.999999, "同一设备重复输入产生不稳定向量"
                data = {
                    "engine": SOURCES["engine"]["commit"],
                    "vectors": [*vectors, *query_vector, *long_vector, *indexed[0]["vectors"]],
                    "scores": scores,
                    "fragments": indexed[0]["fragments"],
                }
                if device == "cpu":
                    reference = data
                    reference_path.write_text(json.dumps(data), "utf-8")
                else:
                    assert data["fragments"] == reference["fragments"]
                    assert len(data["vectors"]) == len(reference["vectors"])
                    cosines = [
                        sum(a * b for a, b in zip(left, right, strict=True))
                        / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
                        for left, right in zip(data["vectors"], reference["vectors"], strict=True)
                    ]
                    cosine = min(cosines)
                    error = max(abs(a - b) for a, b in zip(scores, reference["scores"], strict=True))
                    row.update(min_cosine=cosine, rerank_max_abs=error, cosines=cosines)
                    (
                        Path(cache_dir) / "model-fixtures" / (device.split(":")[0] + "-results.json")
                    ).write_text(json.dumps(data), "utf-8")
                # Cancel after httpcore reports the native request body sent. No
                # timing sleeps; the other loaded model must remain usable.
                sent = asyncio.Event()

                async def trace(name, info):
                    if name == "http11.send_request_body.complete":
                        sent.set()

                async def hook(request):
                    request.extensions["trace"] = trace

                server = manager.servers["embedding"]
                server.client.event_hooks["request"].append(hook)
                process = server.process
                request = asyncio.create_task(
                    manager.infer("embedding", "embed", [long_text] * 32, settings, timeout=120)
                )
                try:
                    await asyncio.wait_for(sent.wait(), 10)
                finally:
                    request.cancel()
                    try:
                        await request
                    except asyncio.CancelledError:
                        pass
                    else:
                        raise AssertionError("推理未被取消")
                assert not process.isAlive() and "embedding" not in manager.ready
                assert manager.servers["reranker"].alive
                after = await manager.infer(
                    "reranker", "rerank", [[query, [documents[0]]]], settings, timeout=120
                )
                assert abs(after[0] - scores[0]) <= 0.001
                row["cancel_released_owned_process"] = True
                if device != "cpu":
                    # Cross-hardware cosine is an observation, not a pass gate.
                    assert error <= 0.001, "重排分数差异超过阈值"
                row["ok"] = True
            except Exception as error:
                # Keep independent backend results for diagnosis; fail the matrix
                # after all selected backends, never treat a skipped device as passed.
                row["error"] = str(error)
                print(f"FAIL: {device}: {error}", flush=True)
            finally:
                await manager.unload("embedding")
                await manager.unload("reranker")
                report_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), "utf-8")
        assert all(row.get("ok", True) for row in checks), checks
    finally:
        await manager.close()
    assert not manager.servers and not manager.ready
    print("PASS: local_models", flush=True)


def run(cache_dir, suite):
    # Native UI/restart probes run in DevSmokeInstrumentation.
    if suite not in {
        "all",
        "native",
        "sessions",
        "scripts",
        "documents",
        "restart",
        "models",
        "ui",
        "devices",
        "models-opencl",
        "models-hexagon",
    }:
        raise ValueError("未知自检范围：" + suite)
    checks = []
    started = time.perf_counter()
    if suite == "devices":
        from tavern.android.local_models import AndroidLocalModelManager

        with tempfile.TemporaryDirectory(prefix="tavern-devices-", dir=cache_dir) as temporary:
            manager = AndroidLocalModelManager(SimpleNamespace(root=Path(temporary)))
            asyncio.run(manager.initialize())
            checks.append({"name": "model_devices", "devices": manager.devices})
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
    if suite in {"models", "models-opencl", "models-hexagon"}:
        # Deliberately opt-in: normal daily checks do not require or load model weights.
        with tempfile.TemporaryDirectory(prefix="tavern-model-smoke-", dir=cache_dir) as temporary:
            asyncio.run(local_models(Path(temporary), cache_dir, checks, suite))
    return json.dumps({"ok": True, "checks": checks, "seconds": round(time.perf_counter() - started, 3)})
