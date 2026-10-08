"""CLI operations against the owner API, including streaming and human decisions."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager

import httpx

from acp_gateway.config import AppConfig
from acp_gateway.daemon import owner_token
from acp_gateway.log import redact_text, redact_value


def api_url(config: AppConfig) -> str:
    host = config.settings.gateway.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.settings.gateway.port}"


def check_response(response: httpx.Response) -> None:
    if response.is_error or response.is_redirect:
        response.read()
        try:
            detail = response.json().get("detail", "request failed")
        except (ValueError, AttributeError):
            detail = "request failed"
        raise ValueError(f"HTTP {response.status_code}: {detail}")


def events(response: httpx.Response):
    check_response(response)
    kind, data = "message", []
    for line in response.iter_lines():
        if not line:
            if data:
                yield kind, json.loads("\n".join(data))
            kind, data = "message", []
        elif line.startswith("event:"):
            kind = line[6:].lstrip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    check_response(response)
    return response.json()


def show_json(data):
    print(json.dumps(redact_value(data), ensure_ascii=False, indent=2))


def show_job(job, *, as_json=False) -> int:
    if as_json:
        show_json(job)
    else:
        print(redact_text(job["answer"]))
        print(f"job {job['id']}: {job['status']}", file=sys.stderr)
        if job.get("error"):
            print(redact_text(job["error"]), file=sys.stderr)
    return int(job["status"] in {"failed", "interrupted", "cancelled"} or bool(job.get("error")))


def stream_job(client, job, *, as_json=False):
    if job["status"] != "running":
        return show_job(job, as_json=as_json)
    printed = ""
    final = None
    try:
        with client.stream("GET", f"/jobs/{job['id']}/events") as response:
            for kind, data in events(response):
                if kind in {"snapshot", "resync"}:
                    current = data
                elif kind == "JobFinished":
                    current = data["job"]
                elif kind == "JobProgress" and data.get("event_type") == "MessageChunk":
                    if not as_json:
                        text = redact_text(data["event"]["text"])
                        print(text, end="", flush=True)
                        printed += text
                    continue
                else:
                    continue
                if not as_json:
                    answer = redact_text(current["answer"])
                    if answer.startswith(printed):
                        print(answer[len(printed) :], end="", flush=True)
                    elif answer != printed:
                        print("\n" + answer, end="", flush=True)
                    printed = answer
                if current["status"] != "running":
                    final = current
                    break
        if final is None:
            raise ValueError(f"event stream ended; use acpgw result {job['id']} to recover the job")
    except KeyboardInterrupt:
        request(client, "POST", f"/jobs/{job['id']}/cancel")
        raise
    if as_json:
        return show_job(final, as_json=True)
    print()
    print(f"job {final['id']}: {final['status']}", file=sys.stderr)
    if final.get("error"):
        print(redact_text(final["error"]), file=sys.stderr)
    return int(final["status"] != "completed" or bool(final.get("error")))


@contextmanager
def approval_connection(client):
    with client.stream("GET", "/approvals/events") as response:
        iterator = events(response)
        try:
            kind, connected = next(iterator)
        except StopIteration as exc:
            raise ValueError("approval connection ended before initialization") from exc
        if kind != "connected":
            raise ValueError("approval connection did not initialize")
        yield connected, iterator


def decide(client, approval, lease, kind):
    option = next((o for o in approval["request"]["options"] if o["kind"] == kind), None)
    if option is None:
        raise ValueError("the requested approval option is not available")
    request(
        client,
        "POST",
        f"/approvals/{approval['id']}",
        json={"option_id": option["option_id"], "lease_id": lease},
    )


def watch_approvals(client):
    def handle(approval, lease):
        print(f"\nApproval {approval['id']} · {approval['conversation']['agent']}")
        print(redact_text(approval["request"]["title"] or "Agent action"))
        show_json(approval["request"]["raw_input"])
        choice = input("Allow once [a], reject [r/Enter], skip [s]: ").strip().lower()
        if choice == "s":
            return
        if choice not in {"a", "r", ""}:
            print("Unknown choice; request left pending.")
            return
        try:
            decide(client, approval, lease, "allow_once" if choice == "a" else "reject_once")
            print("Decision sent.", flush=True)
        except ValueError as exc:
            print(redact_text(str(exc)), file=sys.stderr)

    with approval_connection(client) as (connected, iterator):
        print("Watching approvals. Ctrl+C disconnects this human channel.", flush=True)
        lease = connected["lease_id"]
        for approval in connected["approvals"]:
            handle(approval, lease)
        for kind, data in iterator:
            if kind == "ApprovalRequested":
                handle(data["approval"], lease)
            elif kind == "resync":
                for approval in request(client, "GET", "/approvals")["approvals"]:
                    handle(approval, lease)
    raise ValueError("approval stream ended; reconnect with acpgw approvals watch")


def run(config: AppConfig, args) -> int:
    token = owner_token(config)
    with httpx.Client(
        base_url=api_url(config),
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, read=None),
    ) as client:
        command = args.command
        conv = {"agent": getattr(args, "agent", None), "thread": getattr(args, "thread", "default")}
        if command == "status":
            show_json(request(client, "GET", "/health"))
        elif command == "computers":
            show_json(request(client, "GET", "/computers"))
        elif command == "sessions":
            show_json(
                request(
                    client,
                    "GET",
                    "/sessions",
                    params={k: v for k, v in conv.items() if v is not None},
                )
            )
        elif command == "new":
            show_json(request(client, "POST", "/sessions", json=conv | {"cwd": args.cwd}))
        elif command == "switch":
            show_json(request(client, "POST", f"/sessions/{args.session}/activate", json=conv))
        elif command == "stop":
            show_json(request(client, "POST", "/stop", json=conv))
        elif command == "result":
            job = request(client, "GET", f"/jobs/{args.job_id}", params={"wait": args.wait})
            return show_job(job, as_json=args.json)
        elif command == "ask":
            text = sys.stdin.read() if args.text == "-" else args.text
            path = f"/sessions/{args.session}/messages" if args.session else "/messages"
            job = request(client, "POST", path, json=conv | {"text": text})
            if args.no_stream:
                show_json(job)
            else:
                return stream_job(client, job, as_json=args.json)
        elif command == "approvals":
            action = args.approval_command or "list"
            if action == "list":
                show_json(request(client, "GET", "/approvals"))
            elif action == "watch":
                watch_approvals(client)
            else:
                with approval_connection(client) as (connected, _):
                    approval = next(
                        (a for a in connected["approvals"] if a["id"] == args.approval_id), None
                    )
                    if approval is None:
                        raise ValueError("approval not found or already settled")
                    decide(
                        client,
                        approval,
                        connected["lease_id"],
                        "allow_once" if action == "approve" else "reject_once",
                    )
                    print("Decision sent.")
        return 0
