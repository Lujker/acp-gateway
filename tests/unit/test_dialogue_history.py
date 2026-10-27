"""Prompts commit with jobs, survive reopening, and expire with their results."""

import secrets
import sqlite3
from datetime import timedelta

import pytest

from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import utcnow


def test_prompt_is_redacted_durable_and_expires_with_job(tmp_path):
    path = tmp_path / "gateway.db"
    secret = secrets.token_hex(24)
    register_secret(secret)
    store = Store.open(path)
    conv = Conversation("telegram", "123", "agent")
    record = store.add_session(conv, "session", "/work")
    store.add_job("new", record.id, prompt="Inspect " + secret)
    store.finish_job("new", JobStatus.COMPLETED, answer="done")
    store.close()
    store = Store.open(path)
    try:
        history = store.dialogue_turn(record.id)
        assert history["turn"]["answer"] == "done"
        assert history["turn"]["prompt"].startswith("Inspect ")
        assert secret not in history["turn"]["prompt"]
        store.add_job("running", record.id, prompt="keep")
        assert store.prune_jobs(utcnow() + timedelta(seconds=1)) == 1
        assert store.dialogue_turn(record.id, "new")["turn"] is None
        assert store._conn.execute("SELECT job_id FROM job_prompts").fetchall()[0][0] == "running"
        assert store.session(record.id) is not None
    finally:
        store.close()


def test_prompt_insert_failure_rolls_back_job_and_delivery():
    store = Store.open(":memory:")
    try:
        record = store.add_session(Conversation("telegram", "1", "a"), "s", "/work")
        store._conn.execute(
            "CREATE TRIGGER reject_prompt BEFORE INSERT ON job_prompts BEGIN "
            "SELECT RAISE(ABORT, 'test write failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="test write failure"):
            store.add_job("failed", record.id, prompt="do work", delivery_namespace="telegram:1")
        assert store.job("failed") is None
        assert store.deliveries.get("telegram:1", "failed") is None
    finally:
        store.close()


def test_history_cursor_stays_in_dialogue_and_old_prompt_is_unknown():
    store = Store.open(":memory:")
    try:
        conv = Conversation("telegram", "1", "agent")
        first = store.add_session(conv, "first", "/first")
        second = store.add_session(conv, "second", "/second")
        for identity in ("a", "b", "c"):
            store.add_job(identity, first.id, prompt=None if identity == "a" else identity)
        store.add_job("foreign", second.id, prompt="foreign")
        center = store.dialogue_turn(first.id, "b")
        assert (center["older"], center["newer"]) == ("a", "c")
        store.add_job("z", first.id, prompt="new arrival")
        assert store.dialogue_turn(first.id, "b") == center
        assert store.dialogue_turn(first.id, "foreign")["turn"] is None
        assert store.dialogue_turn(first.id, "a")["turn"]["prompt"] is None
    finally:
        store.close()
