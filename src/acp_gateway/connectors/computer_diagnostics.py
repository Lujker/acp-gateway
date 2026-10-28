"""Bounded connector diagnostics; probes never create sessions or send prompts."""

import asyncio
import tempfile
from pathlib import Path
from time import monotonic

from acp_gateway.adapters.runtime_inventory import wait_for_cleanup
from acp_gateway.agents import AgentClient
from acp_gateway.agents.errors import ManagedCapacity
from acp_gateway.connectors.maintenance import activity_reason
from acp_gateway.connectors.protocol import ComputerDiagnostic, RuntimeResult


class ComputerDiagnostics:
    def __init__(self, config, maintenance, updates, clients, local_factory):
        self.config, self.maintenance, self.updates = config, maintenance, updates
        self.clients, self.local_factory = clients, local_factory
        self.probing = 0
        self.uncertain = 0
        self._report_cache = None
        self._report_at = 0.0

    def maintenance_status(self):
        return {"opening": self.probing, "uncertain_sessions": self.uncertain}

    async def _probe(self, alias):
        profile = next((p for p in self.config.settings.agents if p.alias == alias), None)
        if profile is None or profile.backend == "connector":
            return "unavailable"
        if self.probing or self.maintenance.token is not None:
            return "busy"
        # Reserve before the first await; maintenance cannot hand off during a probe.
        self.probing += 1
        transport = None
        failed_cleanup = False

        def on_failed_cleanup(count):
            nonlocal failed_cleanup
            failed_cleanup = True

        async def factory():
            nonlocal transport
            if profile.backend == "claude_code":
                transport = await self.local_factory(alias, diagnostic_failure=on_failed_cleanup)
            else:
                transport = await self.local_factory(alias)
            return transport

        client = None
        temporary = None
        cancelled = False
        result = "failed"
        try:
            if profile.backend in {"codex", "claude_code", "hermes"}:
                client = AgentClient(
                    profile,
                    pin_dir=self.config.settings.resolved_data_dir() / "pins",
                    transport_factory=factory,
                    request_timeout=8,
                    reconnect_delays=(),
                )
            else:
                # A diagnostic must not silently establish a new TOFU pin.
                from acp_gateway.cli.wizard import _snapshot

                temporary = tempfile.TemporaryDirectory(prefix="acpgw-diagnostic-")
                pins = Path(temporary.name)
                existing = self.config.settings.resolved_data_dir() / "pins" / f"{alias}.sha256"
                if contents := _snapshot(existing):
                    (pins / existing.name).write_bytes(contents)
                secret = self.config.secrets.get(profile.secret_env) if profile.secret_env else None
                client = AgentClient(
                    profile, secret, pin_dir=pins, request_timeout=8, reconnect_delays=()
                )
            await asyncio.wait_for(client.connect(), 10)
            result = "ok"
        except asyncio.CancelledError:
            cancelled = True
        except ManagedCapacity:
            result = "busy"
        except Exception:
            result = "failed"
        finally:
            try:
                if client is not None:
                    cleanup = asyncio.create_task(client.close())
                    _, interrupted = await wait_for_cleanup(cleanup)
                    cancelled = cancelled or interrupted
                native = getattr(transport, "_transport", transport)
                if getattr(native, "cleanup_unconfirmed", False):
                    self.uncertain = max(self.uncertain, 1)
                    result = "cleanup_unknown"
                if failed_cleanup:
                    result = "cleanup_unknown"
            except Exception:
                self.uncertain = max(self.uncertain, 1)
                result = "cleanup_unknown"
            finally:
                try:
                    if temporary is not None:
                        temporary.cleanup()
                except OSError:
                    if result != "cleanup_unknown":
                        result = "failed"
                finally:
                    self.probing -= 1
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def request(self, request):
        report_task = asyncio.create_task(self._report())
        try:
            probe = (
                await self._probe(request.alias) if request.action == "initialize" else "not_run"
            )
            version, updater = await report_task
        finally:
            if not report_task.done():
                report_task.cancel()
                await asyncio.gather(report_task, return_exceptions=True)
        status = self.maintenance.status()
        bounded = lambda name: min(32768, max(0, status.get(name, 0)))  # noqa: E731
        return RuntimeResult(
            page=1,
            pages=1,
            profiles=[],
            diagnostics=ComputerDiagnostic(
                version=version,
                updater=updater,
                maintenance=activity_reason(status),
                running=bounded("running_sessions"),
                requests=bounded("pending_requests"),
                approvals=bounded("pending_approvals"),
                opening=bounded("opening"),
                probe=probe,
                probe_alias=request.alias,
            ),
        )

    def _read_report(self):
        report = self.updates.report()
        if report.can_update:
            updater = "ready"
        elif not self.config.settings.updates.enabled:
            updater = "disabled"
        elif report.platform not in {"linux", "win32"} or not (
            report.manager == "docker" or (report.manager == "uv" and report.source == "registry")
        ):
            updater = "unsupported"
        else:
            backend = "docker" if report.manager == "docker" else "uv"
            updater = self.updates.readiness(backend)
        return report.installed_version, updater

    async def _report(self):
        if self._report_cache is not None and monotonic() - self._report_at < 30:
            return self._report_cache
        try:
            value = await asyncio.wait_for(asyncio.to_thread(self._read_report), 12)
            self._report_cache, self._report_at = value, monotonic()
            return value
        except Exception:
            from acp_gateway import __version__

            return __version__, "unknown"
