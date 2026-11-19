"""Directory, complete reads and literal search over Card Author's current drafts."""

import re

import jsonpointer

from tavern.character.cards import card_view
from tavern.character.editor_documents import child, preview
from tavern.character.editor_reads import remember
from tavern.core.model_view import IDENTITY_FIELDS, hidden_field


def string_fields(value, path, nodes, owner=None):
    owner = nodes.get(path, owner)
    if isinstance(value, str):
        if owner is not None:
            yield owner, path, value
    elif isinstance(value, (dict, list)):
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in children:
            if not hidden_field(str(key)) and key not in IDENTITY_FIELDS:
                yield from string_fields(item, child(path, key), nodes, owner)


class DocumentTools:
    def public_ref(self, node):
        return self.view.pointer(node.ref) if node.ref.startswith("/") else node.ref

    async def transfer_document(self, args):
        from tavern.character.editor_transfer import transfer

        return transfer(self, args)

    def document_node(self, ref, handle):
        name = self.view.pointer(handle, restore=True) if handle.startswith("/") else handle
        return self.directory.resolve(self.card(ref), name)

    def summary(self, item):
        cached = self.summaries.get(item["ref"])
        if cached and cached[0] is item["document"]:
            return cached[1]
        index = self.directory.index(item)
        data = card_view(item["document"])
        groups = {}
        constants = []
        for node in index.values():
            groups[node.branch] = groups.get(node.branch, 0) + 1
            if node.matches("constants"):
                constants.append(node)
        summary = {
            "navigation_only": True,
            "previews": {
                k: preview(self.view.text(data[k]), 140)
                for k in ("description", "personality", "scenario")
                if data.get(k)
            },
            "tags_preview": [
                preview(self.view.text(t), 40) for t in (data.get("tags") or [])[:8] if isinstance(t, str)
            ]
            if isinstance(data.get("tags"), list)
            else [],
            "branches": groups,
            "constants": {
                "count": len(constants),
                "body_chars": sum(len(n.text or "") for n in constants),
                "sample": [n.listing(self.view.text) for n in constants[:5]],
            },
        }
        self.summaries[item["ref"]] = item["document"], summary
        return summary

    async def list_documents(self, args):
        nodes = self.directory.index(self.card(args.card_ref)).values()
        rows, total = [], 0
        for n in nodes:
            if not n.matches(args.branch):
                continue
            if total >= args.offset and (args.limit is None or len(rows) < args.limit):
                row = n.listing(self.view.text, args.limit is None)
                row["ref"] = self.public_ref(n)
                if args.branch == "all":
                    row["branch"] = n.branch
                rows.append(row)
            total += 1
        end = args.offset + len(rows)
        return {
            "card_ref": args.card_ref,
            "branch": args.branch,
            "navigation_only": True,
            "documents": rows,
            "total": total,
            "next_offset": end if end < total else None,
        }

    async def search_documents(self, args):
        refs = (
            args.card_refs
            or self.owner.request.get("target_refs")
            or [c["ref"] for c in self.owner.working["cards"]]
        )
        pattern = re.compile(f"(?=({re.escape(args.query)}))", re.IGNORECASE)
        total, hits = 0, []
        for ref in dict.fromkeys(refs):
            card = self.card(ref)
            nodes = {n.path: n for n in self.directory.index(card).values()}
            for node, path, value in string_fields(card["document"], "", nodes):
                if not node.matches(args.branch):
                    continue
                visible = self.view.text(value)
                for match in pattern.finditer(visible):
                    if args.offset <= total < args.offset + args.limit:
                        start, end = match.span(1)
                        hits.append(
                            {
                                "card_ref": ref,
                                "ref": self.public_ref(node),
                                "title": node.title,
                                "branch": node.branch,
                                "path": self.view.pointer(path),
                                "match_start": start,
                                "preview": visible[max(0, start - 48) : end + 48],
                            }
                        )
                    total += 1
        end = args.offset + len(hits)
        return {
            "matches": hits,
            "total": total,
            "navigation_only": True,
            "next_offset": end if end < total else None,
        }

    async def read_documents(self, args):
        card = self.card(args.card_ref)
        nodes = [self.document_node(args.card_ref, ref) for ref in dict.fromkeys(args.refs)]
        if args.all_constants:
            selected = {n.ref for n in nodes}
            nodes.extend(
                n
                for n in self.directory.index(card).values()
                if n.matches("constants") and n.ref not in selected
            )
        elif not nodes:
            raise ValueError("请选择文档引用，或指定 all_constants 读取全部常驻叙事条目")

        def read(path):
            remember(self.owner.reads, args.card_ref, card["document"], path)
            return jsonpointer.resolve_pointer(card["document"], path)

        result = []
        for n in nodes:
            row = {
                "ref": self.public_ref(n),
                "title": n.title,
                "path": self.view.pointer(n.path),
                "branch": n.branch,
            }
            if args.part == "raw":
                row["raw"] = read(n.path)
            else:
                if args.part in {"document", "body"}:
                    if n.body is not None:
                        row.update(body=read(n.path + n.body), body_path=self.view.pointer(n.path + n.body))
                    elif n.branch in {"fields", "extensions"}:
                        row["value"] = read(n.path)
                    else:
                        row["body"] = None
                if args.part in {"document", "properties"}:
                    row["properties"] = dict(n.labels)
                    if isinstance(n.value, dict) and n.branch in {"entries", "regex", "scripts"}:
                        # Attribute paths reflect the real storage location, including
                        # ST extension aliases. Never grant a read of the entire parent.
                        row["attributes"] = [
                            {"path": self.view.pointer(sub), "value": read(n.path + sub)}
                            for sub in n.attribute_paths()
                        ]
            result.append(row)
        return {"card_ref": args.card_ref, "documents": result}

    async def edit_document_text(self, args):
        node = self.document_node(args.card_ref, args.ref)
        suffix = node.body if args.path is None else self.view.pointer(args.path, restore=True)
        if suffix is None:
            raise ValueError("该文档没有正文，请指定实际文本字段")
        path = node.path + suffix
        self.require_read(args.card_ref, path)
        source = jsonpointer.resolve_pointer(self.card(args.card_ref)["document"], path)
        if not isinstance(source, str):
            raise ValueError("所选字段不是文本")
        replacement = self.view.restore(args.text)
        if args.old_text is not None:
            old = self.view.restore(args.old_text)
            first = source.find(old)
            if first < 0 or source.find(old, first + 1) != -1:
                raise ValueError("原文片段没有唯一命中，请重读当前正文并用前后文字明确位置")
            replacement = source[:first] + replacement + source[first + len(old) :]
        # Only the model-provided fragments were projected. Round-tripping the
        # untouched source through handle substitution can alter literal text.
        return self.apply_operations(
            args.card_ref, [{"op": "replace", "path": path, "value": replacement}], args.comment
        )
