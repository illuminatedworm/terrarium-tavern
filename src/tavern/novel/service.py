"""Own independently scheduled investigators and wakeups, never story commits."""

import asyncio
from datetime import datetime, timezone

from tavern.core.channels import require_channel
from tavern.core.context_usage import bind_meter
from tavern.core.model_view import HANDLE
from tavern.core.provider import make_provider, require_model_connection
from tavern.novel.source import FILES, directory
from tavern.novel.storage import MAX_LIVING, MAX_STARTS, NovelStore
from tavern.novel.worker import NovelWorker
from tavern.scripting.request_context import ProviderRequestContext
from tavern.search.retrieval_config import ModelProfile
from tavern.storage.store import dump

START_NOTICE = "调查已在后台开始。本轮正文需要结论时，用 wait_novel_research 等待。"
DELIVERY_NOTICE = "报告、提问和失败通知会在本次工具结果之后作为单独消息出现；已发出的正文不会被改写。"


def exhausted(job):
    return (
        job["requests"] >= job["config"]["max_requests"] or job["elapsed"] >= job["config"]["timeout_seconds"]
    )


class NovelService:
    def __init__(self, store):
        self.store, self.data = store, NovelStore(store)
        self.tasks = {}
        self.restart_requested = set()
        self.provider_factory = None
        self.changed = asyncio.Event()
        self.generation = 0
        self.closed = False

    def notify(self):
        self.generation += 1
        self.changed.set()

    async def wait_change(self, generation, timeout=30):
        if generation != self.generation:
            return
        self.changed.clear()
        if generation != self.generation:
            return
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except TimeoutError:
            pass  # An SSE heartbeat / explicit bounded wait is not a failed investigation.

    def snapshot_config(self, sid, *, main_settings=None):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT id,agent_mode,channel_origin,context_strategy,compact_auto,model_selection,kemini "
                "FROM sessions WHERE id=?",
                (sid,),
            ).fetchone()
        if row is None:
            raise ValueError("对话不存在")
        session = dict(row)
        config = self.store.settings_document().full_agent.novel_research
        main = main_settings or self.store.session_settings(session)
        override = config.official_model if session["channel_origin"] == "chatgpt" else config.model
        profile = override or main.full_agent
        selected = profile.model_dump()
        if override is None:
            # Follow the actual delegator, including the user's per-turn reasoning
            # choice, rather than a global profile edited after that turn started.
            effective = main.model_dump()
            selected.update(
                {
                    key: effective[key]
                    for key in ModelProfile.model_fields
                    if key in effective and key not in {"provider_id", "provider_connections"}
                }
            )
        return {
            "profile": selected,
            "follows_main": override is None,
            "channel": session["channel_origin"],
            "max_requests": config.max_requests,
            "timeout_seconds": config.timeout_seconds,
        }

    def settings(self, job):
        frozen = job["config"]
        cfg = self.store.settings("full_agent", frozen["channel"])
        cfg.novel_research = ModelProfile.model_validate(frozen["profile"])
        cfg = cfg.for_role("novel_research")
        require_channel(cfg, frozen["channel"])
        cfg.channel_origin = frozen["channel"]
        cfg.context_strategy = "openai_compact" if frozen["channel"] == "chatgpt" else "new_context"
        cfg.compact_auto = True
        cfg.kemini.enabled = False
        cfg.cache_scope, cfg.cache_window = "novel:" + job["id"], "research"
        bind_meter(cfg, self.store, "novel", job["id"])
        return cfg

    def public(self, sid):
        with self.store.connection() as db:
            session = db.execute("SELECT agent_mode FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None or session[0] != "full_agent":
            raise ValueError("原著调查仅限 Full Agent 对话")
        binding = self.data.binding(sid)
        jobs = self.data.jobs(sid)
        with self.store.connection() as db:
            history = [
                dict(r)
                for r in db.execute(
                    "SELECT s.* FROM novel_sources s JOIN novel_source_history h "
                    "ON h.source=s.id WHERE h.session=? ORDER BY s.created DESC",
                    (sid,),
                )
            ]
            event_seq = db.execute(
                "SELECT coalesce(max(seq),0) FROM novel_events WHERE session=?", (sid,)
            ).fetchone()[0]
        for source in history:
            source["missing"] = not all(
                (directory(self.store.root, source["id"]) / f).is_file() for f in FILES
            )
            source["location"] = str(directory(self.store.root, source["id"]))
            source["current"] = source["id"] == binding["source"]
        return {
            "revision": binding["revision"],
            "event_seq": event_seq,
            "enabled": bool(binding["enabled"]),
            "source": next((s for s in history if s["current"]), None),
            "history": history,
            "jobs": jobs,
            "archive_before": self.data.archive_before(sid, jobs),
            "active": sum(j["status"] != "destroyed" for j in jobs),
            "effective": self.snapshot_config(sid),
        }

    def summaries(self, sid):
        return [
            {
                "ref": j["ref"],
                "status": j["status"],
                "question": j["task"],
                "origin": f"H{j['round_position']}",
                "message_pending": bool(j["pending_messages"]),
                "requests_remaining": max(0, j["config"]["max_requests"] - j["requests"]),
                "seconds_remaining": max(0, round(j["config"]["timeout_seconds"] - j["elapsed"], 1)),
                **({"error": j["error"]} if j["status"] in {"failed", "paused"} and j["error"] else {}),
            }
            for j in self.data.jobs(sid, living_only=True)
        ]

    def start(self, owner, args):
        sid = owner.session["id"]
        with self.store.connection() as db:
            previous = db.execute(
                "SELECT ref,status FROM novel_jobs WHERE session=? AND operation=?",
                (sid, owner.novel_operation),
            ).fetchone()
        if previous:
            return {
                "ref": f"R{previous['ref']}",
                "status": previous["status"],
                "created": False,
                "notice": "此委派已启动，不重复创建或刷新额度；已暂停或失败时用 resume_novel_research 恢复。",
            }
        config = self.snapshot_config(sid, main_settings=owner.settings)
        cfg = self.settings({"id": owner.tid, "config": config})
        if self.provider_factory is None:
            require_model_connection(cfg)
        binding = self.data.binding(sid)
        if not binding["source"]:
            raise ValueError("用户尚未指定原著文件")
        if not all((directory(self.store.root, binding["source"]) / name).is_file() for name in FILES):
            raise ValueError("原著参考文件缺失，请用户重新导入")
        job_id, created = self.data.create(
            sid,
            owner.novel_operation,
            len(owner.history) + 1,
            owner.tid,
            self.delegated_text(owner, args.question),
            self.delegated_text(owner, args.background),
            config,
        )
        if created:
            self.launch(job_id)
        job = self.data.job(job_id)
        return {
            "ref": f"R{job['ref']}",
            "status": job["status"],
            "created": created,
            "notice": START_NOTICE,
        }

    @staticmethod
    def delegated_text(owner, text):
        # Natural-language tasks can contain literal @D text. Only actual parent
        # handles are restored; the investigator projects its independent view.
        return HANDLE.sub(
            lambda match: (
                owner.model_view.reverse.get(match[0], match[0])
                if match[0] not in owner.model_view.reserved
                else match[0]
            ),
            text,
        )

    def launch(self, job_id):
        if self.closed:
            raise ValueError("应用正在关闭，调查已暂停")
        current = self.tasks.get(job_id)
        if current and not current.done():
            self.restart_requested.add(job_id)
            self.notify()
            return
        self.data.mark_running(job_id)
        self.tasks[job_id] = asyncio.create_task(self.drive(job_id))

        def finished(task):
            if self.tasks.get(job_id) is not task:
                return
            self.tasks.pop(job_id, None)
            requested = job_id in self.restart_requested
            self.restart_requested.discard(job_id)
            if self.closed:
                return
            state = self.data.dispatch_state(job_id)
            # A follow-up can race with client cleanup or an earlier pause. Honor
            # the later explicit wakeup, but never restart consumed mail or a destroyed task.
            if (
                state
                and state["status"] != "destroyed"
                and (
                    requested
                    and state["status"] in {"paused", "failed"}
                    or state["pending"]
                    and (requested or state["status"] in {"completed", "waiting"})
                )
            ):
                self.launch(job_id)

        self.tasks[job_id].add_done_callback(finished)
        self.notify()

    async def drive(self, job_id):
        job, worker, provider = self.data.job(job_id), None, None
        try:
            if job["status"] == "destroyed":
                return
            if exhausted(job):
                raise ValueError("调查者额度已耗尽；跨轮、追问和恢复不会重置额度")
            job["status"], job["error"] = "running", None
            self.data.save(job)
            cfg = self.settings(job)
            context = ProviderRequestContext(
                job_id, job_id, "research", self.store.device_id(), routing=job["progress"].get("routing", {})
            )
            worker = NovelWorker(self, job, cfg, context)
            async with asyncio.timeout(job["config"]["timeout_seconds"] - job["elapsed"]):
                provider = (
                    self.provider_factory(cfg)
                    if self.provider_factory
                    else await make_provider(cfg, request_context=context)
                )
                await worker.run(provider)
        except asyncio.CancelledError:
            job["status"], job["error"] = "paused", "调查已停止，可明确恢复；已用额度保留"
            if worker:
                worker.account_time()
            self.data.save(job)
        except Exception as error:
            # A background task has no awaiting caller: persist and surface its actual failure.
            if worker:
                worker.account_time()
            job["status"] = "failed"
            if isinstance(error, TimeoutError):
                # The deadline fired at the remaining budget; monotonic accounting can land a hair
                # short, which would otherwise offer a resume that times out immediately.
                job["elapsed"] = max(job["elapsed"], job["config"]["timeout_seconds"])
                job["error"] = "调查者累计运行时限已耗尽"
            else:
                job["error"] = str(error) or type(error).__name__
            advice = (
                "额度已耗尽，不能恢复；可销毁后按需新建，或不依赖原著继续。"
                if exhausted(job)
                else "仍有剩余额度，可用 resume_novel_research 恢复，或销毁后不依赖原著继续。"
            )
            self.data.save(
                job,
                mail={
                    "kind": "error",
                    "content": f"调查未完成：{job['error']}。已保留部分过程。{advice}未完成的调查不能当作已经查证。",
                    "operation": f"error-{job['requests']}",
                },
            )
        finally:
            if provider:
                try:
                    await asyncio.wait_for(provider.close(), 5)
                except TimeoutError:
                    # Closing a spent transport must not erase a saved report or the original failure.
                    job["error"] = (job["error"] or "") + "；模型连接关闭超时"
                    self.data.save(job)
            self.notify()

    async def pause(self, job_id):
        self.restart_requested.discard(job_id)
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if job_id not in self.tasks or self.tasks.get(job_id) is task:
                self.data.settle_unstarted_pause(job_id)
        else:
            job = self.data.job(job_id)
            if job["status"] != "destroyed":
                job["status"] = "paused"
                self.data.save(job)
        self.notify()

    async def destroy(self, job_id):
        self.restart_requested.discard(job_id)
        was_running = self.data.job(job_id)["status"] == "running"
        self.data.destroy(job_id)  # Revoke first so a racing response cannot be delivered.
        task = self.tasks.get(job_id)
        if task and not task.done():
            if was_running:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.notify()

    async def cancel_invalidated(self, sid):
        with self.store.connection() as db:
            revoked = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM novel_jobs WHERE session=? AND status='destroyed'", (sid,)
                )
            ]
        if not revoked:
            return
        for identity in revoked:
            task = self.tasks.get(identity)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.notify()

    async def close(self):
        self.closed = True
        self.restart_requested.clear()
        running = list(self.tasks)
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for job_id in running:
            self.data.settle_unstarted_pause(job_id)
        self.notify()

    async def execute(self, owner, name, args):
        sid = owner.session["id"]
        if name == "start_novel_research":
            return self.start(owner, args)
        if name == "list_novel_research":
            investigators = self.summaries(sid)
            return {
                "investigators": investigators,
                "slots_remaining": max(0, MAX_LIVING - len(investigators)),
                "starts_remaining_this_turn": max(
                    0, MAX_STARTS - self.data.starts(sid, len(owner.history) + 1)
                ),
            }
        job = self.data.resolve(sid, args.ref)
        if name == "destroy_novel_research":
            # Destroying revokes undelivered mail; the model must see a report before discarding its sender.
            if any(row["job"] == job["id"] for row in self.data.inbox(sid)):
                raise ValueError(
                    f"{args.ref} 还有尚未送达的报告或提问，销毁会丢弃它们；请在下一步收到后再销毁"
                )
            await self.destroy(job["id"])
            return {"ref": args.ref, "status": "destroyed"}
        if name in {"message_novel_research", "resume_novel_research"} and exhausted(job):
            raise ValueError(f"{args.ref} 额度已耗尽，不能继续；可销毁后按需新建")
        if name == "message_novel_research":
            if self.data.send(
                job["id"],
                self.delegated_text(owner, args.message),
                owner.novel_operation,
            ):
                self.launch(job["id"])
        elif name == "resume_novel_research":
            if job["status"] not in {"paused", "failed"}:
                raise ValueError("只能恢复已暂停或失败的调查者；已完成的调查请用 message_novel_research 追问")
            self.launch(job["id"])
        elif name == "wait_novel_research":
            deadline = asyncio.get_running_loop().time() + args.timeout_seconds
            while True:
                state = self.data.dispatch_state(job["id"])
                if not state or state["status"] == "destroyed":
                    break
                if not (
                    state["status"] == "running"
                    or job["id"] in self.restart_requested
                    or state["pending"]
                    and state["status"] in {"completed", "waiting"}
                ):
                    break
                generation = self.generation
                if any(row["job"] == job["id"] for row in self.data.inbox(sid)):
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await self.wait_change(generation, remaining)
        current = self.data.job(job["id"])
        return {"ref": args.ref, "status": current["status"], "notice": DELIVERY_NOTICE}

    async def deliver(self, owner):
        window = owner.state["window"]["id"]
        needs_status = not any(
            item["window_id"] == window and item["message"].get("_novel_status") for item in owner.new_trace
        )
        if not needs_status and not self.data.inbox(owner.session["id"]):
            return
        # Snapshot scripts before selecting mail. From the selection through the
        # SQLite progress/receipt commit there is no await where UI revocation could intervene.
        mirror = await owner.script.call("snapshot")
        if needs_status:
            binding = self.data.binding(owner.session["id"])
            source = self.data.source(binding["source"])
            status = {
                "file": source["name"],
                "imported_at": datetime.fromtimestamp(source["created"], timezone.utc).isoformat(),
                "investigators": [
                    {k: row[k] for k in ("ref", "status", "origin")}
                    for row in self.summaries(owner.session["id"])
                ],
                "notice": "只授权当前文件版本；其他版本文件的旧报告只作历史参考，不能当作当前文件的证据。",
            }
            owner.append(
                {
                    "role": "system",
                    "message_type": "novel_status",
                    "content": "原著参考运行状态：\n" + dump(owner.model_view.expose(status)),
                    "_novel_status": True,
                },
                window,
            )
        packets = self.data.inbox(owner.session["id"])
        for packet in packets:
            body = {
                "investigator": f"R{packet['ref']}",
                "created_from": f"H{packet['round_position']}",
                # The background was written by the main agent itself; repeating it on every
                # report, question and error would cost up to 16k characters each time.
                "initial_question": packet["task"],
                "kind": packet["kind"],
                "source": {
                    "name": packet["source_name"],
                    "imported_at": datetime.fromtimestamp(packet["source_created"], timezone.utc).isoformat(),
                },
                "content": packet["content"],
                "notice": "原著参考，不是本局已发生的事实；请结合当前进度判断是否仍适用。",
            }
            owner.append(
                {
                    "role": "system",
                    "message_type": "novel_research",
                    "content": "原著调查消息：\n" + dump(owner.model_view.expose(body)),
                    "_novel_mail": packet["seq"],
                },
                owner.state["window"]["id"],
            )
        if packets or needs_status:
            # Saving the appended messages and claiming mail share a SQLite transaction.
            await owner.save_progress(script_mirror=mirror)
            self.notify()
