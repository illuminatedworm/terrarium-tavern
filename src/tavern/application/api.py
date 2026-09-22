import asyncio
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tavern.application.service import RoleplayRuntime
from tavern.character.cards import export_card, normalize
from tavern.conversation.context import compile_context
from tavern.core.models import (
    ContentEdit,
    ContextRecipe,
    LoreInjectionRemoval,
    MessageEdit,
    PersonaEdit,
    RevisionRequest,
    SceneRefreshRequest,
    SessionCreate,
    SessionFork,
    SessionOptions,
    Settings,
    TurnRequest,
)
from tavern.search.retrieval_config import Provider
from tavern.storage.store import Conflict, Store, dump


def create_app(data_dir=None):
    store = Store(Path(data_dir or os.environ.get("TAVERN_DATA", "data")))
    service = RoleplayRuntime(store)
    from tavern.character.card_editor import CardEditorService
    editor = CardEditorService(store)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        async def maintain():
            from tavern.storage.maintenance import cleanup
            while True:
                if not any(not t.done() for t in service.tasks.values()) and not any(not t.done() for t in service.local_models.tasks.values()):
                    try:
                        cleanup(store)
                        service.retrieval.cache.clear()
                        service.retrieval.dense_cache.clear()
                    except (OSError, Conflict):
                        pass  # In-use files are retried at the next idle pass.
                await asyncio.sleep(3600)
        maintenance = asyncio.create_task(maintain())
        try:
            yield
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            await editor.close()
            await service.close()

    app = FastAPI(title="Terrarium Tavern", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.service = store, service
    app.state.shutdown_callback = None
    app.state.editor = editor
    from tavern.character.card_editor_api import register_editor_routes
    register_editor_routes(app, editor)

    @app.post("/api/shutdown")
    async def shutdown(payload: dict):
        if payload.get("confirm") is not True:
            raise ValueError("需要明确确认关停服务")
        if app.state.shutdown_callback is None:
            raise ValueError("当前启动方式未注册关停接口，请使用 start.ps1 启动")
        asyncio.get_running_loop().call_later(0.2, app.state.shutdown_callback)
        return {"status": "stopping"}

    @app.middleware("http")
    async def local_boundary(request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅允许本地访问"}, status_code=403)
        origin = request.headers.get("origin")
        if (
            origin
            and origin != f"{request.url.scheme}://{request.headers.get('host')}"
            and origin not in {"http://127.0.0.1:5173", "http://localhost:5173"}
        ):
            return JSONResponse({"detail": "跨来源请求被拒绝"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409 if isinstance(error, Conflict) else 400)

    @app.exception_handler(KeyError)
    async def not_found(request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "runtime": "KohakuTerrarium", "version": "0.1.0"}

    @app.get("/api/storage")
    async def storage():
        from tavern.storage.maintenance import storage_report
        return storage_report(store)

    @app.post("/api/storage/cleanup")
    async def storage_cleanup(payload: dict):
        from tavern.storage.maintenance import cleanup
        if any(not t.done() for t in service.local_models.tasks.values()):
            raise Conflict("请先结束本地模型或索引任务")
        cleanup(store, vacuum=payload.get("vacuum") is True)
        service.retrieval.cache.clear()
        service.retrieval.dense_cache.clear()
        return await storage()

    @app.get("/api/settings")
    async def settings():
        result = store.settings().for_role("foreground").model_dump()
        from tavern.core.prompts import PROMPT_DEFINITIONS
        result["prompt_definitions"] = PROMPT_DEFINITIONS
        result["has_api_key"] = bool(store.settings().for_role("foreground").api_key)
        for provider in result["providers"]:
            provider["has_api_key"] = bool(provider.pop("api_key"))
        return result

    @app.put("/api/settings")
    async def save_settings(payload: dict):
        old = store.settings().model_dump()
        # Capability can only be changed by the actual probe route.
        payload.pop("capability", None)
        payload.pop("capability_key", None)
        payload.pop("has_api_key", None)
        if payload.get("api_key") == "":
            payload.pop("api_key")
        if "providers" in payload:
            existing = {p["id"]: p for p in old["providers"]}
            for provider in payload["providers"]:
                provider.pop("has_api_key", None)
                if not provider.get("api_key"):
                    provider["api_key"] = existing.get(provider["id"], {}).get("api_key", "")
        for role in ("foreground", "background", "card_editor"):
            if role in payload and payload[role] is not None:
                payload[role]["capability"] = (old[role] or {}).get("capability", "unknown")
                payload[role]["capability_key"] = (old[role] or {}).get("capability_key", "")
        new = Settings.model_validate({**old, **payload})
        for role in ("foreground", "background", "card_editor"):
            profile = getattr(new, role)
            if profile is None:
                continue
            old_profile = old[role] or old["background"]
            provider = next(p for p in new.providers if p.id == profile.provider_id)
            old_provider = next((p for p in old["providers"] if p["id"] == old_profile["provider_id"]), {})
            if (profile.model, profile.provider_id, provider.base_url) != (old_profile["model"], old_profile["provider_id"], old_provider.get("base_url")):
                profile.capability, profile.capability_key = "unknown", ""

        resolved = new.for_role("foreground")
        for field in ("base_url", "api_key", "model", "tokenizer", "context_tokens", "reply_tokens", "temperature",
                      "reasoning_effort", "reasoning_format", "proxy", "use_proxy"):
            setattr(new, field, getattr(resolved, field))
        if urlparse(new.base_url).scheme not in ("http", "https"):
            raise ValueError("模型地址必须为 HTTP(S)")
        if (old["base_url"], old["model"]) != (new.base_url, new.model):
            new.capability, new.capability_key = "unknown", ""
        new = Settings.model_validate(new.model_dump())
        store.save_settings(new)
        return await settings()

    @app.post("/api/settings/models")
    async def provider_models(provider: Provider):
        import httpx

        config = store.settings()
        saved = next((p for p in config.providers if p.id == provider.id), None)
        key = provider.api_key or (saved.api_key if saved and saved.base_url == provider.base_url else "")
        proxy = (config.network.proxy or None) if config.network.api_proxy and provider.use_proxy else None
        try:
            async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=30) as client:
                response = await client.get(provider.base_url.rstrip("/") + "/models",
                                            headers={"Authorization": f"Bearer {key}"} if key else {})
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    raise ValueError("模型接口未返回 data 列表")
                return {"models": sorted({m["id"] for m in data
                                          if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]})}
        except httpx.HTTPStatusError as error:
            raise ValueError(f"获取模型列表失败：HTTP {error.response.status_code}") from None
        except httpx.RequestError:
            raise ValueError("无法连接模型接口，请检查地址、代理和网络") from None

    @app.get("/api/local-models")
    async def local_model_status():
        return service.local_models.status(store.settings())

    @app.post("/api/local-models/{purpose}/{action}")
    async def local_model_action(purpose: Literal["embedding", "reranker"], action: Literal["download", "warmup"]):
        config = store.settings()
        if action == "download":
            return service.local_models.download(purpose, config)
        async def warm(record):
            await service.local_models.warmup(purpose, config)
            record["phase"] = "preparing"
            service.retrieval.cache.clear()
        return service.local_models.job("warmup:" + purpose, warm)

    @app.post("/api/local-models/jobs/{jid}/cancel")
    async def cancel_model_job(jid: str):
        task = service.local_models.tasks.get(jid)
        if task is None:
            raise KeyError("模型任务不存在")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return service.local_models.jobs[jid]

    @app.get("/api/retrieval/indexes")
    async def retrieval_indexes():
        with store.connection() as db:
            return {"cached_entries": db.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0],
                    "jobs": [job for job in service.local_models.jobs.values() if job["kind"] == "index"]}

    @app.post("/api/sessions/{sid}/retrieval/rebuild")
    async def rebuild_retrieval(sid: str):
        session = store.session(sid)
        document = (session["snapshot"].get("content") or {}).get("document", store.session_card(sid)["document"])
        async def rebuild(record):
            config = store.settings()
            if "embedding" in config.retrieval.channels and not service.local_models.status(config)["models"]["embedding"]["ready"]:
                raise ValueError("请先下载并预热 Embedding 模型，再准备向量索引")
            record["phase"] = "preparing"
            service.retrieval.cache.clear()
            await service.retrieval.prepare(document, session["messages"], "", session["snapshot"],
                                            config, session, "rebuild", index_only=True)
            jobs = [task for jid, task in service.local_models.tasks.items()
                    if service.local_models.jobs[jid]["kind"] == "index" and not task.done()]
            if jobs:
                await asyncio.gather(*jobs)
                failed = [service.local_models.jobs[jid] for jid, task in service.local_models.tasks.items()
                          if task in jobs and service.local_models.jobs[jid]["status"] != "complete"]
                if failed:
                    raise ValueError(failed[0].get("error", "索引任务未完成"))
            record["message"] = "世界书索引准备完成" if "embedding" in config.retrieval.channels else "关键词 / BM25 索引已准备；未启用向量通道，不生成向量条目"
        return service.local_models.job("rebuild", rebuild)

    @app.post("/api/settings/probe")
    async def probe(role: Literal["foreground", "background", "card_editor"] = "foreground"):
        from tavern.scripting.agent import make_provider

        saved_config = store.settings()
        config = saved_config.for_role(role)
        captured = []
        provider = await make_provider(config, captured.append)
        try:
            result = await provider.chat_complete(
                [
                    {"role": "system", "content": config.probe_system_prompt},
                    {"role": "user", "content": "Scene begins."},
                    {"role": "assistant", "content": "Ready."},
                    {
                        "role": "system",
                        "content": config.probe_marker_prompt,
                    },
                    {"role": "user", "content": "State the current scene marker."},
                ]
            )
            preserved = bool(captured and captured[-1]["messages"][3]["role"] == "system")
            config.capability = (
                "accepted" if preserved and "TAVERN_MID_42" in result.content else "unsupported"
            )
            config.capability_key = f"{config.base_url.rstrip('/')}|{config.model}|chat-completions"
            effective_role = "background" if role == "card_editor" and saved_config.card_editor is None else role
            getattr(saved_config, effective_role).capability = config.capability
            getattr(saved_config, effective_role).capability_key = config.capability_key
            saved_config.capability, saved_config.capability_key = config.capability, config.capability_key
            store.save_settings(saved_config)
            return {
                "capability": config.capability,
                "request": captured,
                "response": result.content,
                "note": "此检查验证请求位置与一次响应，不证明服务商内部实现或所有 RP 场景效果。",
            }
        finally:
            await provider.close()

    @app.get("/api/cards")
    async def cards():
        return store.cards()

    @app.post("/api/cards/import")
    async def import_card(file: UploadFile = File(...)):
        return store.import_card(await file.read(32 * 1024 * 1024 + 1), file.filename or "card.json")

    @app.get("/api/cards/{cid}")
    async def card(cid: str):
        return store.card(cid)

    @app.put("/api/cards/{cid}")
    async def edit_card(cid: str, edit: ContentEdit):
        if edit.expected_revision is None:
            raise ValueError("缺少角色卡版本，请刷新后保存")
        return store.edit_card(cid, edit.document, edit.expected_revision)

    @app.post("/api/cards/{cid}/copy")
    async def copy_card(cid: str):
        return store.copy_card(cid)

    @app.put("/api/cards/{cid}/permission")
    async def permission(cid: str, payload: dict):
        store.card(cid)
        with store.connection() as db:
            db.execute("UPDATE cards SET permission=?,revision=revision+1 WHERE id=?", (bool(payload.get("enabled")), cid))
        return store.card(cid)

    @app.post("/api/cards/{cid}/dependencies")
    async def resolve_dependencies(cid: str):
        return await service.dependencies.resolve(store.card(cid))

    @app.get("/api/cards/{cid}/dependencies")
    async def dependency_manifest(cid: str):
        return service.dependencies.read(store.card(cid))

    @app.post("/api/cards/{cid}/lorebook")
    async def import_lore(cid: str, file: UploadFile = File(...)):
        card = store.card(cid)
        book = json.loads(await file.read(4 * 1024 * 1024))
        entries = book.get("entries", [])
        if isinstance(entries, dict):
            entries = [{**entry, "uid": key} for key, entry in entries.items()]
        if not isinstance(entries, list):
            raise ValueError("世界书 entries 无效")
        data = card["document"].get("data", card["document"])
        data["character_book"] = {**book, "entries": entries}
        return store.edit_card(cid, card["document"], card["revision"])

    @app.get("/api/cards/{cid}/export")
    async def export(cid: str):
        card = store.card(cid)
        original = (store.root / "blobs" / card["original"]).read_bytes()
        resources = {
            name: (store.root / "blobs" / digest).read_bytes() for name, digest in card["resources"].items()
        }
        content, mime, ext = export_card(card["document"], card["container"], original, resources)
        return Response(
            content,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="character.{ext}"'},
        )

    @app.get("/api/cards/{cid}/portrait")
    async def portrait(cid: str):
        card = store.card(cid)
        if card["container"] == "png":
            return FileResponse(store.root / "blobs" / card["original"], media_type="image/png")
        assets = normalize(card["document"]).get("assets", [])
        for asset in assets:
            path = asset.get("uri", "").removeprefix("embeded://")
            if asset.get("type") == "icon" and path in card["resources"]:
                mime = mimetypes.guess_type(path)[0]
                if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"):
                    continue
                return FileResponse(store.root / "blobs" / card["resources"][path], media_type=mime)
        raise HTTPException(404, "没有头像")

    @app.get("/api/sessions")
    async def sessions():
        return store.sessions()

    @app.get("/api/personas")
    async def personas():
        return store.personas()

    @app.post("/api/personas")
    async def new_persona(edit: PersonaEdit):
        return store.save_persona(None, edit)

    @app.put("/api/personas/{pid}")
    async def edit_persona(pid: str, edit: PersonaEdit):
        return store.save_persona(pid, edit)

    @app.delete("/api/personas/{pid}")
    async def delete_persona(pid: str):
        store.delete_persona(pid)
        return {"deleted": True}

    @app.post("/api/sessions")
    async def new_session(options: SessionCreate):
        session = store.new_session(options)
        try:
            result = await service.initialize_session(session["id"])
            service.prepare_indexes(session["id"])
            return result
        except BaseException:
            with store.connection() as db:
                db.execute("DELETE FROM messages WHERE session=?", (session["id"],))
                db.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
            raise

    @app.delete("/api/cards/{cid}")
    async def delete_card(cid: str):
        await service.delete_card(cid)
        return {"deleted": cid}

    @app.get("/api/sessions/{sid}")
    async def session(sid: str):
        result = store.session(sid)
        card = store.session_card(sid)
        if card["permission"] and any("<" in message.get("display", "") for message in result["messages"]):
            await service.dependencies.resolve(card)
            for message in result["messages"]:
                if "<" in message.get("display", ""):
                    message["display"] = service.dependencies.markup(card, message["display"])
        service.prepare_indexes(sid)
        return result

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        await service.delete_session(sid)
        return {"deleted": sid}

    @app.get("/api/sessions/{sid}/branches")
    async def branches(sid: str):
        return store.branches(sid)

    @app.get("/api/sessions/{sid}/card")
    async def session_card(sid: str):
        return store.session_card(sid)

    @app.post("/api/sessions/{sid}/fork", status_code=201)
    async def fork_session(sid: str, payload: SessionFork):
        return store.fork_session(sid, payload.message_id, payload.expected_revision)

    @app.put("/api/turns/{tid}/input")
    async def edit_pending_input(tid: str, edit: MessageEdit):
        return store.edit_pending_input(tid, edit.text, edit.expected_revision)

    @app.put("/api/sessions/{sid}/messages/{mid}")
    async def edit_message(sid: str, mid: str, edit: MessageEdit):
        return await service.edit_message(sid, mid, edit.text, edit.expected_revision)

    @app.put("/api/sessions/{sid}/messages/{mid}/version")
    async def switch_message_version(sid: str, mid: str, change: RevisionRequest):
        return store.switch_message_version(sid, mid, change.head, change.expected_revision)

    @app.delete("/api/sessions/{sid}/lore-injections/{anchor_id}/{entry_id}")
    async def remove_lore_injection(sid: str, anchor_id: str, entry_id: str, change: LoreInjectionRemoval):
        return store.remove_lore_injection(sid, anchor_id, entry_id, change.expected_revision)

    @app.put("/api/sessions/{sid}/lorebook")
    async def edit_session_lore(sid: str, change: ContentEdit):
        from tavern.character.card_workflow import validate_document
        from tavern.storage.store import uid

        if change.expected_revision is None:
            raise ValueError("缺少对话版本")
        session = store.session(sid)
        import copy
        document = copy.deepcopy((session["snapshot"].get("content") or {}).get(
            "document", store.session_card(sid)["document"]))
        document.get("data", document)["character_book"] = normalize(change.document).get("character_book", {})
        validate_document(document)
        req = TurnRequest(text="编辑对话世界书", expected_revision=change.expected_revision,
                          operation_id=change.operation_id or uid(), allow_content_edit=True)
        tid, created = store.create_turn(sid, req)
        if not created:
            return store.session(sid)
        try:
            store.set_turn(tid, status="running")
            snapshot = copy.deepcopy(session["snapshot"])
            snapshot["content"] = {"document": document, "reason": "手工编辑对话世界书", "turn": tid}
            message = {**session["messages"][-1], "id": uid(), "snapshot": snapshot}
            pending = {"messages": [message],
                       "before": session["snapshot"], "reason": "手工编辑对话世界书"}
            if session["policy_effective"] == "preview":
                store.set_turn(tid, status="awaiting_review", pending=dump(pending))
                store.event(tid, "awaiting_review", {"before": session["snapshot"], "after": snapshot})
            else:
                store.commit_turn(tid, pending)
        except Exception as error:
            store.set_turn(tid, status="error", error=str(error))
            raise
        return store.session(sid)

    @app.put("/api/sessions/{sid}/head")
    async def switch_branch(sid: str, change: RevisionRequest):
        return store.update_session(sid, change.expected_revision, head=change.head)

    @app.put("/api/sessions/{sid}/options")
    async def session_options(sid: str, options: SessionOptions):
        return store.update_session(
            sid,
            options.expected_revision,
            persona=options.persona,
            policy=options.policy,
            persona_data=dump(options.persona_data),
            **({"turn_preference": options.turn_preference} if "turn_preference" in options.model_fields_set else {}),
        )

    @app.put("/api/sessions/{sid}/recipe")
    async def recipe(sid: str, payload: dict):
        validated = (store.settings().context_recipe.model_copy(deep=True) if payload.get("use_global")
                     else ContextRecipe.model_validate(payload["recipe"]))
        return store.update_session(sid, payload["expected_revision"], recipe=validated.model_dump_json())

    @app.post("/api/sessions/{sid}/preview")
    def preview(sid: str, payload: dict):
        # FastAPI runs sync endpoints in its worker pool: token counting and
        # history compilation must not block story reads or turn event streams.
        s = store.session(sid)
        c = store.session_card(sid)
        document = (s["snapshot"].get("content") or {}).get("document", c["document"])
        result = compile_context(
            document, s, s["messages"], s["snapshot"], payload.get("text", ""), store.settings().for_role("foreground"), "preview",
            read_only=True,
        )
        result["usage"] = store.latest_usage(sid)
        result["memory_warning"] = s["snapshot"].get("memory_warning")
        return result

    @app.post("/api/sessions/{sid}/turns", status_code=202)
    async def submit(sid: str, req: TurnRequest):
        return service.submit(sid, req)

    @app.post("/api/sessions/{sid}/scene/{kind}/regenerate", status_code=202)
    async def regenerate_scene(sid: str, kind: Literal["memory"], req: SceneRefreshRequest):
        return service.submit(sid, TurnRequest(**req.model_dump(), scene_refresh=kind))


    from tavern.memories.memory_records import RecordEdit

    @app.put("/api/sessions/{sid}/memory/records")
    async def edit_memory_record(sid: str, edit: RecordEdit):
        return store.edit_memory_record(sid, edit)

    @app.get("/api/sessions/{sid}/memory/rebuild-estimate")
    async def memory_rebuild_estimate(sid: str):
        from tavern.memories.memory_engine import estimate_batches
        from tavern.memories.memory_records import read_memory
        session = store.session(sid)
        cfg = store.settings().for_role("background")
        document = (session["snapshot"].get("content") or {}).get("document", store.session_card(sid)["document"])
        base = {"version": 1, "records": [r for r in read_memory(session["snapshot"])["records"] if r.get("manual")]}
        import asyncio
        count = await asyncio.to_thread(estimate_batches, session["messages"], base, document, session["persona"], cfg)
        return {"estimated_batches": count, "review_max_calls": 1 if cfg.memory_review_enabled else 0,
                "message": "按现有手工记忆估算；新增记忆可能增加批次数，完成前可取消，全部成功后替换。"}

    @app.post("/api/sessions/{sid}/interaction")
    async def interaction(sid: str, payload: dict):
        return await service.interaction(sid, payload)

    @app.get("/api/turns/{tid}")
    async def turn(tid: str):
        return store.turn(tid)

    @app.get("/api/sessions/{sid}/work-records")
    async def work_records(sid: str, before: str | None = None, limit: int = Query(20, ge=1, le=50)):
        return store.work_turns(sid, before, limit)

    @app.get("/api/sessions/{sid}/work-records/{tid}/events")
    async def work_events(sid: str, tid: str, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return store.work_events(sid, tid, after, limit)

    @app.get("/api/sessions/{sid}/work-records/{tid}/events/{seq}")
    async def work_event_payload(sid: str, tid: str, seq: int):
        return store.work_event_payload(sid, tid, seq)

    @app.post("/api/turns/{tid}/cancel")
    async def cancel(tid: str):
        await service.cancel(tid)
        return store.turn(tid)

    @app.post("/api/turns/{tid}/review")
    async def review(tid: str, payload: dict):
        return service.review(tid, bool(payload.get("accept")))

    @app.get("/api/turns/{tid}/events")
    async def event_stream(tid: str, request: Request, after: int = 0):
        store.turn(tid)

        async def stream():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while True:
                for event in store.events(tid, cursor):
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {dump(event)}\n\n"
                if store.turn(tid)["status"] not in ("queued", "running"):
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return  # Disconnecting the observer never cancels the producer.
                await asyncio.sleep(0.15)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    static = Path(__file__).parents[1] / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")
    dist = Path(__file__).resolve().parents[3] / "dist"
    if not dist.exists():
        dist = static / "web"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app
