"""Public conversation-oriented authoring API; all mutations check a revision."""

import copy
import json
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from tavern.character.editor_context import EditorContext
from tavern.character.editor_runtime_config import editor_runtime_config
from tavern.character.editor_sandbox import test_card
from tavern.character.editor_tools import EditorTools, tool_definitions
from tavern.character.editor_worldbook import reject_worldbook
from tavern.core.models import CardAuthorModelOptions, CardAuthorRequest


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(Body):
    title: str = Field(default="", max_length=100)


class Revision(Body):
    expected_revision: int = Field(ge=0)


class Title(Revision):
    title: str = Field(min_length=1, max_length=100)


class Reference(Revision):
    card_id: str
    document: dict[str, Any] | None = None
    expected_card_revision: int | None = Field(default=None, ge=0)


class Document(Revision):
    document: dict[str, Any]
    target_mode: Literal["half_agent", "full_agent"] | None = None


class PersonaReference(Revision):
    persona_id: str
    expected_persona_version: int = Field(ge=0)


class WorldbookReference(Revision):
    worldbook_id: str
    expected_worldbook_revision: int | None = Field(default=None, ge=0)


class Save(Revision):
    mode: Literal["overwrite", "copy", "create"]
    operation_id: str = Field(min_length=1, max_length=100)


class PortraitChoice(Revision):
    file_ref: str | None = Field(pattern=r"^F[1-9]\d*$")


class Probe(Revision):
    index: int = Field(default=0, ge=0)
    samples: list[str] = Field(default_factory=list, max_length=10)


class Preview(CardAuthorModelOptions, Body):
    text: str = ""


def register_conversation_routes(app, editor):
    base = "/api/card-author/conversations"

    @app.get(base)
    async def list_conversations():
        return editor.edits.conversations()

    @app.post(base)
    async def create_conversation(payload: Create):
        return editor.edits.create_conversation(payload.title)

    @app.get(base + "/{wid}")
    async def conversation(wid: str):
        return editor.edits.conversation(wid)

    @app.put(base + "/{wid}")
    async def rename(wid: str, payload: Title):
        return editor.edits.title(wid, payload.title, payload.expected_revision)

    @app.delete(base + "/{wid}")
    async def delete(wid: str, payload: Revision):
        editor.edits.delete(wid, payload.expected_revision)
        return {"deleted": True}

    @app.post(base + "/{wid}/references")
    async def attach(wid: str, payload: Reference):
        return editor.edits.attach(
            wid, payload.card_id, payload.expected_revision, payload.document, payload.expected_card_revision
        )

    @app.post(base + "/{wid}/files")
    async def upload_file(wid: str, expected_revision: int = Form(...), file: UploadFile = File(...)):
        from tavern.character.editor_attachments import attach_file
        from tavern.character.editor_files import MAX_FILE_BYTES

        try:
            with editor.store.connection() as db:
                editor.edits.check(db, wid, expected_revision)
            data = bytearray()
            while chunk := await file.read(1024 * 1024):
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("文件超过 32 MiB，请先拆分。")
            return attach_file(editor.edits, wid, expected_revision, file.filename, data)
        finally:
            await file.close()

    @app.delete(base + "/{wid}/files/{ref}")
    async def remove_file(wid: str, ref: str, payload: Revision):
        from tavern.character.editor_attachments import detach_file

        return detach_file(editor.edits, wid, payload.expected_revision, ref)

    @app.delete(base + "/{wid}/references/{ref}")
    async def detach(wid: str, ref: str, payload: Revision):
        return editor.edits.remove_card(wid, ref, payload.expected_revision)

    @app.post(base + "/{wid}/persona-references")
    async def attach_persona(wid: str, payload: PersonaReference):
        return editor.edits.attach_persona(
            wid, payload.persona_id, payload.expected_revision, payload.expected_persona_version
        )

    @app.post(base + "/{wid}/worldbook-references")
    async def attach_worldbook(wid: str, payload: WorldbookReference):
        return editor.edits.attach_worldbook(
            wid, payload.worldbook_id, payload.expected_revision, payload.expected_worldbook_revision
        )

    @app.delete(base + "/{wid}/persona-references/{ref}")
    async def detach_persona(wid: str, ref: str, payload: Revision):
        return editor.edits.detach_persona(wid, ref, payload.expected_revision)

    @app.put(base + "/{wid}/drafts/{ref}")
    async def edit(wid: str, ref: str, payload: Document):
        options = {"target_mode": payload.target_mode} if "target_mode" in payload.model_fields_set else {}
        return editor.edits.update_card(wid, ref, payload.document, payload.expected_revision, **options)

    @app.post(base + "/{wid}/drafts/{ref}/save")
    async def save(wid: str, ref: str, payload: Save):
        return editor.edits.save_card(wid, ref, payload.expected_revision, payload.mode, payload.operation_id)

    @app.put(base + "/{wid}/drafts/{ref}/portrait")
    async def set_portrait(wid: str, ref: str, payload: PortraitChoice):
        return editor.edits.set_portrait(wid, ref, payload.file_ref, payload.expected_revision)

    @app.get(base + "/{wid}/portraits/{digest}")
    async def portrait(wid: str, digest: str):
        path = editor.edits.portrait(wid, digest)
        if not path.is_file():
            raise HTTPException(404, "头像文件不存在")
        return FileResponse(path, media_type="image/png")

    @app.post(base + "/{wid}/drafts/{ref}/undo")
    async def undo(wid: str, ref: str, payload: Revision):
        return editor.edits.undo_card(wid, ref, payload.expected_revision)

    @app.post(base + "/{wid}/drafts/{ref}/reload")
    async def reload(wid: str, ref: str, payload: Revision):
        return editor.edits.reload_card(wid, ref, payload.expected_revision)

    @app.post(base + "/{wid}/drafts/{ref}/test")
    async def test(wid: str, ref: str, payload: Probe):
        with editor.store.connection() as db:
            workspace = editor.edits.check(db, wid, payload.expected_revision)
            item = editor.edits.item(workspace, ref)
        reject_worldbook(item, "开场，无法做本地开场验证")
        return await test_card(editor.store, item, payload.index, payload.samples)

    @app.post(base + "/{wid}/runs")
    async def run(wid: str, payload: CardAuthorRequest):
        return editor.submit(wid, payload, legacy=False)

    @app.delete(base + "/{wid}/progress")
    async def discard(wid: str, payload: Revision):
        return editor.edits.discard(wid, payload.expected_revision)

    @app.get(base + "/{wid}/tools")
    async def tools(wid: str):
        editor.edits.conversation(wid)
        from tavern.web.cloud import WebSession

        cfg = editor_runtime_config(editor.store, wid, {})
        progress = editor.edits.progress(wid)
        return WebSession(
            cfg.card_author_config.web.mode,
            cfg,
            "card_author",
            failed=bool(progress and progress.get("web_fallback")),
        ).definitions(tool_definitions())

    @app.get(base + "/{wid}/context")
    async def context(wid: str):
        workspace = editor.edits.conversation(wid)
        return {
            "revision": workspace["revision"],
            "state": workspace["state"],
            "pending": workspace["pending"] is not None,
            "confirmed_briefs": [
                {"card_ref": c["ref"], "text": c["brief"]} for c in workspace["cards"] if c.get("brief")
            ],
        }

    def preview_owner(wid, payload):
        workspace = editor.edits.conversation(wid)
        with editor.store.connection() as db:
            environment = editor.edits.environment_snapshot(db, workspace)
        progress = editor.edits.progress(wid)
        working = (
            progress["working"] if progress else {"cards": workspace["cards"], "state": workspace["state"]}
        )
        owner = SimpleNamespace(
            store=editor.store,
            edits=editor.edits,
            wid=wid,
            rid="preview",
            legacy=False,
            environment=environment,
            files=copy.deepcopy(workspace.get("files", [])),
            working=copy.deepcopy(working),
            cfg=editor_runtime_config(editor.store, wid, payload.model_dump()),
            definitions=tool_definitions(),
            current_refs=progress.get("current_refs", []) if progress else [],
            pending_calls=copy.deepcopy(progress.get("pending_calls")) if progress else None,
            trace=copy.deepcopy(progress.get("trace", [])) if progress else [],
            persona_changes=copy.deepcopy(progress.get("persona_changes", {})) if progress else {},
            request={},
            reads={},
        )
        from tavern.web.cloud import WebSession

        owner.definitions = WebSession(
            owner.cfg.card_author_config.web.mode,
            owner.cfg,
            "card_author",
            failed=bool(progress and progress.get("web_fallback")),
        ).definitions(tool_definitions())
        owner.context = EditorContext(
            copy.deepcopy(progress["context"] if progress else editor.edits.context(wid)), owner
        )
        owner.tools = EditorTools(owner)
        if payload.text:
            owner.context.add_message(
                "user", payload.text, [f for f in workspace.get("files", []) if not f.get("message_ref")]
            )
        return owner

    @app.post(base + "/{wid}/stats")
    async def stats(wid: str, payload: Preview):
        owner = preview_owner(wid, payload)
        owner.context.prepare()
        with editor.store.connection() as db:
            # The newest usage-bearing call wins, including failed/resumable
            # turns; an omitted cache counter must never reuse an older value.
            row = db.execute(
                """SELECT e.payload FROM card_edit_events e JOIN card_edit_runs r ON r.id=e.run
                WHERE r.workspace=? AND e.kind IN ('context_stats','completed')
                AND json_type(e.payload,'$.usage')='object' ORDER BY e.seq DESC LIMIT 1""",
                (wid,),
            ).fetchone()
        return {
            "context_usage": owner.context.usage(),
            "context_limit": owner.cfg.context_tokens,
            "model": owner.cfg.model,
            **owner.context.image_stats(),
            "usage": json.loads(row[0]).get("usage") if row else None,
            "cache_usage": editor.edits.conversation_cache_usage(wid),
        }

    @app.post(base + "/{wid}/preview")
    async def preview(wid: str, payload: Preview):
        owner = preview_owner(wid, payload)
        messages = owner.context.prepare()
        from tavern.character.editor_attachments import file_metadata
        from tavern.character.editor_images import IMAGE_REFS
        from tavern.core.message_adapter import WIRE_FIELDS, adapt_messages, adapter_snapshot

        messages = [
            {
                **{k: v for k, v in message.items() if k in WIRE_FIELDS},
                **(
                    {
                        "attachments": file_metadata(
                            [f for f in owner.files if f["ref"] in message[IMAGE_REFS]]
                        )
                    }
                    if message.get(IMAGE_REFS)
                    else {}
                ),
            }
            for message in adapt_messages(messages, owner.cfg)[0]
        ]
        tools = (
            [d for d in owner.definitions if d.get("function", {}).get("name") == "new_context"]
            if owner.context.must_rollover
            else owner.definitions
        )
        return {
            "messages": messages,
            "adapter_revision": adapter_snapshot(owner.cfg)["revision"],
            "tools": tools,
            "context_usage": owner.context.usage(),
            "context_limit": owner.cfg.context_tokens,
            "preview_only": True,
            **owner.context.image_stats(),
            "note": "只读预览；图片仅展示附件引用、格式与尺寸，实际请求发送原图。尚未执行模型和工具，不包含后续工具读取结果。未保存的环境快照仅供预览，首次发送时才固定。",
        }
