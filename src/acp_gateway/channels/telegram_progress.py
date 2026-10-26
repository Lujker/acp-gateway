"""Bounded, per-chat public work summaries, separate from the saved answer."""

# ruff: noqa: RUF001
from collections import OrderedDict

from acp_gateway.core import GatewayError
from acp_gateway.log import redact_text

WORK_LIMIT = 6000
WORK_HISTORY = 20
LIVE_WORK = 64


class ProgressCards:
    def _work_cache(self):
        if not hasattr(self, "_work_updates"):
            self._work_updates = OrderedDict()
        return self._work_updates

    def _record_work(self, event):
        chat = int(event.conversation.key)
        if chat not in self._chats or chat not in self.settings.allowed_user_ids:
            return
        cache = self._work_cache()
        key = (chat, event.job_id)
        previous = cache.get(key, {"_source": "", "truncated": False})
        if previous.get("_finished"):
            return
        # Preserve credential context across deltas. Only this bounded in-memory
        # source is unredacted; views and durable history use a redacted snapshot.
        source = (
            previous["_source"] + event.event.text
            if not previous["truncated"]
            else previous["_source"]
        )
        cache[key] = {
            "_source": source[:WORK_LIMIT],
            "truncated": previous["truncated"] or len(source) > WORK_LIMIT,
        }
        cache.move_to_end(key)
        while len(cache) > LIVE_WORK:
            cache.popitem(last=False)

    def _finish_work(self, job):
        chat = int(job.conversation.key)
        if (chat, job.id) not in self._work_cache():
            return
        item = self._work_snapshot(job)
        if not item or chat not in self.settings.allowed_user_ids:
            return
        key = f"work_history:{chat}"
        history = self.core.store.channel_state(self.namespace, key, {})
        history.pop(job.id, None)
        history[job.id] = item
        history = dict(list(history.items())[-WORK_HISTORY:])
        self.core.store.set_channel_state(self.namespace, key, history)
        self._work_cache()[(chat, job.id)] = {**item, "_finished": True}

    def _work_snapshot(self, job):
        chat = int(job.conversation.key)
        item = self._work_cache().get((chat, job.id))
        if item is not None and "_source" in item:
            source = item["_source"]
            if not job.status.finished or item["truncated"]:
                # Withhold the unfinished trailing token, including one cut by
                # the memory bound. It may be a credential not yet recognizable.
                boundary = max(
                    (index for index, char in enumerate(source) if char.isspace()), default=-1
                )
                source = source[: boundary + 1]
            return {"text": redact_text(source), "truncated": item["truncated"]}
        if item is None:
            history = self.core.store.channel_state(self.namespace, f"work_history:{chat}", {})
            item = history.get(job.id)
        return item or {"text": "", "truncated": False}

    async def _work_panel(self, chat, argument):
        parts = argument.split()
        if not 1 <= len(parts) <= 2:
            raise GatewayError("Используйте /work JOB_ID [PAGE].")
        job = self.core.owned_job(parts[0], channel=self.name, key=str(chat))
        item = self._work_snapshot(job)
        text = redact_text(item["text"])
        chunks = [text[i : i + 1200] for i in range(0, len(text), 1200)] or [
            "Промежуточных сообщений пока нет или они не сохранились."
        ]
        try:
            page = int(parts[1]) if len(parts) == 2 else 1
            if not 1 <= page <= len(chunks):
                raise ValueError
        except ValueError:
            raise GatewayError("Недоступная страница хода работы.") from None
        body = f"🧭 Ход работы\n\nЗадача: {job.id}\nСтраница: {page}/{len(chunks)}\n\n"
        body += chunks[page - 1]
        if item["truncated"]:
            body += "\n\nℹ️ Показано начало; продолжение сокращено."
        rows = []
        navigation = []
        if page > 1:
            navigation.append(("← Назад", f"/work {job.id} {page - 1}"))
        if page < len(chunks):
            navigation.append(("Далее →", f"/work {job.id} {page + 1}"))
        if navigation:
            rows.append(navigation)
        rows += [
            [("↻ Обновить", f"/work {job.id} {page}"), ("📄 Результат", f"/result {job.id}")],
            [("← Задача", f"/job {job.id}")],
            *self._back_rows(),
        ]
        await self._panel(chat, body, rows)
