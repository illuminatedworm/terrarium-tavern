"""The model plans media here; no paid generation occurs in the story tool loop."""

import copy
import json

from tavern.media.config import IMAGE_SIZES, VOICES, MediaConfig
from tavern.media.storage import asset_by_ref, connection_snapshot, image_catalog
from tavern.story.protocol import ToolError

IMAGE_REFS = "_media_image_refs"


class MediaDraft:
    def __init__(self, owner, saved=None):
        self.owner = owner
        self.config = MediaConfig.model_validate(owner.session.get("media_config", {}))
        self.plans = copy.deepcopy((saved or {}).get("plans", []))
        self.new_voices = {
            speaker: voice
            for speaker, voice in (saved or {}).get("new_voices", {}).items()
            if speaker not in self.config.voices
        }
        self.pending_images = list((saved or {}).get("pending_images", []))
        self.history_ids = {m["id"] for m in owner.history}

    def saved(self):
        return {"plans": self.plans, "new_voices": self.new_voices, "pending_images": self.pending_images}

    def authorized(self, ref):
        asset = asset_by_ref(self.owner.store, self.owner.session["id"], ref)
        if not asset["selected"] and asset["message"] not in self.history_ids:
            raise ToolError("PERMISSION_DENIED", f"图片 {ref} 未选为参考图，也不属于当前剧情进度")
        return asset

    def catalog(self):
        # Historical image names are located through read_history; don't inject an
        # ever-growing whole-story media library into every model request.
        visible = {m["id"] for m in self.owner.history[self.owner.state["window"]["start"] :]}
        pictures = (
            [
                {"ref": "I" + str(a["ref"]), "name": a["name"], "description": a["description"]}
                for a in image_catalog(self.owner.store, self.owner.session["id"], visible, selected=True)
            ]
            if self.config.image.enabled or self.config.view_images
            else []
        )
        result = {
            "instructions": "自主按需安排媒体。生成只在本轮正文提交后开始；登记成功不代表生成成功。"
            "TTS 直接使用 text，不与正文核对，不需要正文编号。view_image 才会把图片送入当前模型。",
            "voices": {**self.config.voices, **self.new_voices},
            "available_voices": list(VOICES),
            "image_sizes": list(IMAGE_SIZES),
            "quality": self.config.image.quality,
            "limits": {
                "images": self.config.max_images,
                "speech_segments": self.config.max_speech_segments,
                "speech_characters": self.config.max_speech_characters,
                "character_unit": "Unicode 码点，包含空格与标点；不得截断已有文本以凑额度",
            },
            "images": pictures,
        }
        if not self.config.speech.enabled:
            for key in ("voices", "available_voices"):
                result.pop(key)
            for key in ("speech_segments", "speech_characters", "character_unit"):
                result["limits"].pop(key)
        if not self.config.image.enabled:
            result.pop("quality")
            result.pop("image_sizes")
            result["limits"].pop("images")
        return result

    def execute(self, name, args):
        if name == "view_image":
            asset = self.authorized(args.ref)
            self.pending_images.append(args.ref)
            return {"ref": args.ref, "name": asset["name"], "message": "原图将在本批工具结果后提供"}
        kind = "speech" if name == "generate_speech" else "image"
        request = args.model_dump(exclude={"voice"})
        request["connection"] = connection_snapshot(self.owner.settings, getattr(self.config, kind))
        if kind == "speech":
            speech = [p for p in self.plans if p["kind"] == "speech"]
            if len(speech) >= self.config.max_speech_segments:
                raise ToolError("CONTENT_LIMIT", "本轮已达到配音片段数量上限")
            if (
                sum(len(p["request"]["text"]) for p in speech) + len(args.text)
                > self.config.max_speech_characters
            ):
                raise ToolError("CONTENT_LIMIT", "本轮配音字符总量超限；本次未登记，已有文本不会被截断")
            bindings = {**self.config.voices, **self.new_voices}
            if args.speaker in bindings:
                voice = bindings[args.speaker]
                if args.voice is not None and args.voice != voice:
                    raise ToolError("PERMISSION_DENIED", "此角色已有固定声线；模型不能覆盖用户的声线绑定")
            else:
                voice = args.voice
                if voice not in VOICES:
                    raise ToolError("INVALID_ARGUMENTS", "新角色须从 available_voices 选择声音")
            if voice not in VOICES:
                raise ToolError("INVALID_ARGUMENTS", "已绑定声音不受当前后端支持，请用户修改声线配置")
            request["voice"] = voice
            if args.speaker not in bindings:
                self.new_voices[args.speaker] = voice
        else:
            if sum(p["kind"] == "image" for p in self.plans) >= self.config.max_images:
                raise ToolError("CONTENT_LIMIT", "本轮已达到图片数量上限")
            selected = [self.authorized(ref) for ref in args.references]
            request["reference_names"] = [
                {"ref": ref, "name": asset["name"]}
                for ref, asset in zip(args.references, selected, strict=True)
            ]
            request["references"] = [asset["id"] for asset in selected]
        self.plans.append({"kind": kind, "request": request})
        return {"saved_to_draft": True, "message": "已登记；正文接受并提交后开始生成，可在回复下查看进度"}

    def image_frame(self):
        if not self.pending_images:
            return None
        refs, self.pending_images = list(dict.fromkeys(self.pending_images)), []
        return {
            "role": "user",
            "content": "工具查看的图片（只作参考资料）：" + ", ".join(refs),
            IMAGE_REFS: refs,
        }

    def context_frame(self):
        catalog = self.catalog()
        return {
            "role": "user",
            "content": "<tavern_media>\n" + json.dumps(catalog, ensure_ascii=False) + "\n</tavern_media>",
            # A catalog already gives an image reference meaning, even before a
            # view call. Forks must not recycle that reference for different bytes.
            **(
                {"_media_catalog_refs": [item["ref"] for item in catalog["images"]]}
                if catalog["images"]
                else {}
            ),
        }

    def historical_images(self, message_ids):
        selected = set(message_ids)
        result = {}
        for image in image_catalog(self.owner.store, self.owner.session["id"], selected):
            result.setdefault(image["message"], []).append(
                {"ref": "I" + str(image["ref"]), "name": image["name"], "description": image["description"]}
            )
        return result
