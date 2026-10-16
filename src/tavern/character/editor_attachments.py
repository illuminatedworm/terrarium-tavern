"""Browser-selected reference files, owned by one Card Editor conversation."""
import hashlib
import json
import re
import uuid
from pathlib import Path

from tavern.character.editor_files import MAX_FILE_BYTES
from tavern.character.editor_images import IMAGE_FORMATS, image_metadata

EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".json5", ".yaml", ".yml",
              ".xml", ".html", ".log", ".ini", ".toml", ".rst", ".docx", ".xlsx", *IMAGE_FORMATS}


def attachment_path(store, wid, item):
    if not re.fullmatch(r"F[1-9]\d*", item["ref"]) or item["extension"] not in EXTENSIONS:
        raise ValueError("文件引用记录无效")
    root = (store.root / "blobs").resolve()
    directory = root / "editor-files" / hashlib.sha256(wid.encode()).hexdigest()
    path = directory / (item["ref"] + item["extension"])
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("文件引用路径无效")
    return path


def attach_file(edits, wid, revision, filename, data):
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(name).suffix.lower()
    if not name or len(name) > 255 or any(ord(c) < 32 for c in name) or extension not in EXTENSIONS:
        raise ValueError("请选择文本、DOCX、XLSX 文件，或 PNG、JPEG、静态 WebP 图片。")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("文件超过 32 MiB，请先拆分。")
    metadata = image_metadata(extension, data) if extension in IMAGE_FORMATS else {}
    path = None
    try:
        with edits.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = edits.check(db, wid, revision)
            files = workspace.setdefault("files", [])
            if len(files) >= 20 or sum(f["size_bytes"] for f in files) + len(data) > 128 * 1024**2:
                raise ValueError("本对话附件最多 20 个、合计 128 MiB；请移除待发送附件或新建对话。")
            item = {"ref": f"F{workspace['next_ref']}", "name": name, "extension": extension, "size_bytes": len(data), "message_ref": None, **metadata}
            target = attachment_path(edits.store, wid, item)
            target.parent.mkdir(parents=True, exist_ok=True)
            # A process crash may leave the next (never committed) file behind.
            # References are monotonic; never replace a file in the saved list.
            if target.exists() and not any(f["ref"] == item["ref"] for f in files):
                target.unlink()
            with target.open("xb") as stream:
                path = target
                stream.write(data)
            files.append(item)
            db.execute("UPDATE card_editor_conversations SET next_ref=next_ref+1 WHERE id=?", (wid,))
            edits.write(db, workspace)
    except BaseException:
        if path:
            path.unlink(missing_ok=True)
        raise
    return edits.conversation(wid)


def remove_files(store, wid, files, *, conversation_deleted=False):
    if conversation_deleted:
        directory = attachment_path(store, wid, {"ref": "F1", "extension": ".txt"}).parent
        files = [{"ref": p.stem, "extension": p.suffix} for p in directory.iterdir()
                 if re.fullmatch(r"F[1-9]\d*", p.stem) and p.suffix in EXTENSIONS] if directory.is_dir() else []
    for item in files:
        path = attachment_path(store, wid, item)
        path.unlink(missing_ok=True)
        if path.parent.is_dir() and not any(path.parent.iterdir()):
            path.parent.rmdir()


def detach_file(edits, wid, revision, ref):
    with edits.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        workspace = edits.check(db, wid, revision)
        files = workspace.get("files", [])
        item = next((f for f in files if f["ref"] == ref), None)
        if item is None:
            raise KeyError("文件引用不存在")
        if item.get("message_ref"):
            raise ValueError("已发送附件随历史消息保留，不能从输入区移除。")
        workspace["files"] = [f for f in files if f["ref"] != ref]
        edits.write(db, workspace)
    remove_files(edits.store, wid, [item])
    return edits.conversation(wid)


def file_metadata(files):
    return [{key: f[key] for key in ("ref", "name", "extension", "size_bytes", "mime_type", "width", "height") if key in f} for f in files]


def message_content(text, files):
    if not files:
        return text
    instructions = ("图片按附件列表中的图片顺序随本条消息直接提供；"
                    if any(f["extension"] in IMAGE_FORMATS for f in files) else "")
    return (text + "\n\n本条用户消息的附件（" + instructions + "文本或文档使用 ref 作为 read_text_file/read_document 的 path；"
            "按需读取，后续消息不代表重新提交或要求重复读取）：\n" + json.dumps(file_metadata(files), ensure_ascii=False))


def migrate_message_attachments(store):
    """Recover old used files from recorded tool calls; never guess an owner."""
    changes = []
    with store.connection() as db:
        for row in db.execute("SELECT id,payload FROM card_editor_conversations"):
            payload = json.loads(row["payload"])
            legacy = [f for f in payload.get("files", []) if "message_ref" not in f]
            if not legacy:
                continue
            owners = {}
            for event in db.execute("""SELECT e.payload,r.request FROM card_edit_events e
                JOIN card_edit_runs r ON r.id=e.run WHERE r.workspace=? AND e.kind='tool_started'
                ORDER BY e.seq""", (row["id"],)):
                value = json.loads(event["payload"])
                if value.get("tool") not in {"read_text_file", "read_document"}:
                    continue
                try:
                    ref = json.loads(value.get("arguments", "{}" )).get("path")
                except (ValueError, AttributeError, TypeError):
                    continue
                source = json.loads(event["request"]).get("_input_ref")
                if source and isinstance(ref, str):
                    owners.setdefault(ref, source)
            for file in legacy:
                file["message_ref"] = owners.get(file["ref"])
            changes.append((json.dumps(payload, ensure_ascii=False), row["id"], row["payload"]))
    if changes:
        store.backup(store.root / f"before-file-messages-{uuid.uuid4().hex[:12]}.sqlite3")
        with store.connection() as db:
            db.executemany("UPDATE card_editor_conversations SET payload=? WHERE id=? AND payload=?", changes)
