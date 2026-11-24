"""The model plans media here; no paid generation occurs in the story tool loop."""

import copy
import json

from tavern.media.config import (
    LegacySpeechArguments,
    MediaConfig,
    available_voices,
    canonical_voice,
    image_reference_limit,
    image_sizes,
    validate_voice,
)
from tavern.media.storage import asset_by_ref, connection_snapshot, image_catalog
from tavern.story.protocol import ToolError

IMAGE_REFS = "_media_image_refs"


class MediaDraft:
    def __init__(self, owner, saved=None):
        self.owner = owner
        self.config = MediaConfig.model_validate(owner.session.get("media_config", {}))
        self.plans = copy.deepcopy((saved or {}).get("plans", []))
        # A resumed turn can already contain the first staged clip for a role.
        # Its frozen choice applies to later clips for that same pair until commit.
        for plan in self.plans:
            if plan["kind"] == "speech":
                clip = plan["request"]
                conn = clip["connection"]
                story = owner.session["media_story_model"]
                if (
                    clip.get("story_model", story) == story
                    and conn["provider_id"] == self.config.speech.provider_id
                    and conn["model"] == self.config.speech.model
                    and clip["speaker"] not in self.config.voices
                ):
                    voice = canonical_voice(conn["adapter"], clip["voice"])
                    try:
                        validate_voice(self.config.speech, voice)
                    except ValueError:
                        # Keep the old clip for review. A repaired default can serve
                        # later clips for this speaker without inheriting the invalid voice.
                        continue
                    self.config.voices[clip["speaker"]] = voice
        self.pending_images = list((saved or {}).get("pending_images", []))
        self.history_ids = {m["id"] for m in owner.history}

    def saved(self):
        return {"plans": self.plans, "pending_images": self.pending_images}

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
            "image_sizes": list(image_sizes(self.config.image)),
            "limits": {
                "images": self.config.max_images,
                "speech_segments": self.config.max_speech_segments,
                "speech_characters": self.config.max_speech_characters,
                "character_unit": "Unicode 码点，包含空格与标点；不得截断已有文本以凑额度",
            },
            "images": pictures,
        }
        if not self.config.speech.enabled:
            for key in ("speech_segments", "speech_characters", "character_unit"):
                result["limits"].pop(key)
        if not self.config.image.enabled:
            result.pop("image_sizes")
            result["limits"].pop("images")
        elif self.config.image.adapter == "openai_images":
            result["quality"] = self.config.image.quality
        return result

    def execute(self, name, args):
        if name == "view_image":
            asset = self.authorized(args.ref)
            self.pending_images.append(args.ref)
            return {"ref": args.ref, "name": asset["name"], "message": "原图将在本批工具结果后提供"}
        kind = "speech" if name == "generate_speech" else "image"
        request = args.model_dump()
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
            voice = self.config.voices.get(args.speaker, self.config.speech.default_voice)
            if isinstance(args, LegacySpeechArguments):
                if args.voice is not None:
                    chosen = canonical_voice(self.config.speech.adapter, args.voice)
                    if args.speaker in self.config.voices and chosen != voice:
                        raise ToolError("PERMISSION_DENIED", "旧配音调用不能覆盖此角色已有的声线")
                    # A pending model-authored choice is limited to the catalog it saw.
                    if self.config.speech.adapter in (
                        "openai_realtime",
                        "gemini_tts",
                    ) and chosen not in available_voices(self.config.speech, legacy_catalog=True):
                        raise ToolError("INVALID_ARGUMENTS", "旧配音调用只能选择当时提供的预设音色")
                    voice = chosen
                elif args.speaker not in self.config.voices:
                    raise ToolError("INVALID_ARGUMENTS", "旧配音调用没有为新角色选择声线")
            try:
                validate_voice(self.config.speech, voice)
            except ValueError as exc:
                raise ToolError("INVALID_ARGUMENTS", "此角色的配音设置需要用户调整") from exc
            request["voice"] = voice
            request["story_model"] = self.owner.session["media_story_model"]
            self.config.voices[args.speaker] = voice
        else:
            if sum(p["kind"] == "image" for p in self.plans) >= self.config.max_images:
                raise ToolError("CONTENT_LIMIT", "本轮已达到图片数量上限")
            sizes = image_sizes(self.config.image)
            if args.size not in sizes:
                raise ToolError("INVALID_ARGUMENTS", "请选择本次工具提供的尺寸或 auto")
            if len(args.references) > image_reference_limit(self.config.image):
                raise ToolError("INVALID_ARGUMENTS", "参考图数量超过当前生图模型上限")
            request["size"] = sizes[args.size]
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
