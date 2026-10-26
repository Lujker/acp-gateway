"""Opt-in profile administration. Remote inputs never select executables or accounts."""

import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import yaml
from filelock import FileLock, Timeout

from acp_gateway.agents import AgentClient
from acp_gateway.agents.errors import AgentError
from acp_gateway.cli.wizard import _atomic
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.discovery import AgentDiscovery
from acp_gateway.connectors.launch import local_policies
from acp_gateway.connectors.policy import _cwd
from acp_gateway.connectors.protocol import ProfileSpec, ProfilesResult


def revision(content):
    return hashlib.sha256(content).hexdigest()


def check_directory(profile):
    """Check the target OS without starting the agent, hooks, shell or a model."""
    settings = getattr(profile, profile.kind)
    cwd = _cwd(profile.default_cwd)
    if settings.wsl is None:
        if not Path(cwd).is_dir():
            raise ValueError("Directory unavailable")
        return
    from acp_gateway.adapters.wsl import _linux_path

    if os.name != "nt" or not _linux_path(cwd):
        raise ValueError("WSL directory unavailable")
    target = settings.wsl
    result = subprocess.run(  # noqa: S603 -- fixed executable/argv, cwd is a separate argument
        [
            str(Path(os.environ["SystemRoot"]) / "System32" / "wsl.exe"),  # noqa: SIM112
            "--distribution",
            target.distribution,
            "--user",
            target.user,
            "--exec",
            "/usr/bin/env",
            "-i",
            target.python,
            "-I",
            "-c",
            "import os,sys;sys.exit(0 if os.path.isdir(sys.argv[1]) else 1)",
            cwd,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("Directory unavailable")


class LocalProfiles:
    def __init__(self, config, launch, maintenance, clients, policies, managers):
        self.config, self.launch, self.maintenance = config, launch, maintenance
        self.clients, self.policies, self.managers = clients, policies, managers
        self.lock = asyncio.Lock()
        self.discovery = AgentDiscovery()
        self.loaded_revision = (
            revision(config.config_path.read_bytes()) if config.config_path else None
        )

    def snapshot(self, content):
        return ProfilesResult(
            status="ok",
            revision=revision(content),
            profiles=[
                ProfileSpec(
                    alias=p.alias,
                    template=p.alias,
                    title=p.display_name,
                    cwd=p.default_cwd,
                    mode=getattr(p, p.kind).mode,
                )
                for p in self.config.settings.agents
                if p.backend in {"codex", "claude_code"}
            ],
            agents=self.launch.hello(self.config).agents,
        )

    async def request(self, request):
        if not self.config.settings.connector.remote_profiles or self.config.config_path is None:
            return ProfilesResult(status="disabled")
        if not time.time() <= request.expires <= time.time() + 120:
            return ProfilesResult(status="stale")
        if self.lock.locked():
            return ProfilesResult(status="busy")
        async with self.lock:
            try:
                return await self._request(request)
            except (OSError, ValueError, AgentError, Timeout, subprocess.TimeoutExpired):
                return ProfilesResult(status="invalid")

    async def _request(self, request):
        path = self.config.config_path
        # The wizard takes the same lock before checking its original snapshots.
        with FileLock(str(path) + ".profiles.lock", timeout=0, mode=0o600):
            original = path.read_bytes()
            raw = yaml.safe_load(original)
            if not raw.get("connector", {}).get("remote_profiles", False):
                return ProfilesResult(status="disabled")
            if revision(original) != self.loaded_revision:
                return ProfilesResult(status="stale")
            # Refuse externally edited settings until a local restart loads them.
            disk = [AgentProfile.model_validate(p) for p in raw.get("agents", [])]
            if disk != self.config.settings.agents:
                return ProfilesResult(status="stale")
            if request.action == "list":
                return self.snapshot(original)
            if request.action == "discover":
                if request.profile is not None or request.alias is not None:
                    return ProfilesResult(status="invalid")
                candidates, hints = await self.discovery.discover()
                if path.read_bytes() != original or time.time() > request.expires:
                    return ProfilesResult(status="stale")
                return self.snapshot(original).model_copy(
                    update={"candidates": candidates, "hints": hints}
                )
            identity = revision(
                json.dumps(
                    request.model_dump(mode="json", exclude={"epoch", "request"}),
                    sort_keys=True,
                ).encode()
            )
            backup_dir = path.parent / "setup-backups" / "profiles"
            receipt = backup_dir / (request.operation.hex + ".identity")
            backup = backup_dir / (request.operation.hex + ".yaml")
            if receipt.exists() and receipt.read_bytes() != identity.encode():
                return ProfilesResult(status="invalid")
            if raw.get("connector", {}).get("profile_operation") == identity:
                return self.snapshot(original)
            if request.revision != revision(original):
                return ProfilesResult(status="stale")
            profiles = {p.alias: p for p in disk}
            if request.action == "remove":
                if request.profile is not None or request.alias not in profiles or len(disk) <= 1:
                    return ProfilesResult(status="invalid")
                alias = request.alias
                if profiles[alias].backend not in {"codex", "claude_code"}:
                    return ProfilesResult(status="invalid")
                del profiles[alias]
            else:
                spec = request.profile
                if spec is None or request.alias is not None:
                    return ProfilesResult(status="invalid")
                if spec.candidate is not None:
                    if request.action != "add":
                        return ProfilesResult(status="invalid")
                    template = self.discovery.resolve(spec.candidate)
                elif spec.template in profiles:
                    template = profiles[spec.template]
                else:
                    return ProfilesResult(status="invalid")
                if template.backend not in {"codex", "claude_code"}:
                    return ProfilesResult(status="invalid")
                alias = spec.alias
                if (request.action == "add") == (alias in profiles):
                    return ProfilesResult(status="invalid")
                # An edit cannot replace an existing profile's execution identity.
                if request.action == "update" and spec.template != alias:
                    return ProfilesResult(status="invalid")
                data = template.model_dump(mode="json", exclude_none=True)
                data.update(alias=alias, title=spec.title, default_cwd=_cwd(spec.cwd))
                data[template.kind]["mode"] = spec.mode
                if spec.mode != getattr(template, template.kind).mode:
                    # Adopt the selected mode's defaults rather than safe limits in work.
                    settings_type = type(getattr(template, template.kind))
                    defaults = settings_type(mode=spec.mode)
                    old_defaults = settings_type(mode=getattr(template, template.kind).mode)
                    for key in ("sandbox", "turn_timeout", "max_turns"):
                        if key in data[template.kind] and data[template.kind][key] == getattr(
                            old_defaults, key
                        ):
                            data[template.kind][key] = getattr(defaults, key)
                profiles[alias] = AgentProfile.model_validate(data)
                await asyncio.to_thread(check_directory, profiles[alias])
            if len(profiles) > 100:
                return ProfilesResult(status="invalid")
            # Reserve synchronously before any close awaits; local ACP admission is fenced too.
            try:
                token = self.maintenance.apply("prepare")["token"]
            except ValueError:
                return ProfilesResult(status="busy")
            safe_to_resume = False
            try:
                manager = self.managers.get(alias)
                if manager is not None:
                    await manager.close()
                    if any(manager.maintenance_status().values()):
                        return ProfilesResult(status="unknown")
                old = self.clients.get(alias)
                if old is not None:
                    await old.close()
                # Recheck after awaits; never overwrite a concurrent local edit.
                if path.read_bytes() != original or time.time() > request.expires:
                    return ProfilesResult(status="stale")
                raw["agents"] = [
                    p.model_dump(mode="json", exclude_none=True) for p in profiles.values()
                ]
                raw.setdefault("connector", {})["profile_operation"] = identity
                content = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode()
                # Durable UUID tombstone precedes commit; never replace its original backup.
                if not receipt.exists():
                    _atomic(receipt, identity.encode())
                if backup.exists():
                    if backup.read_bytes() != original:
                        return ProfilesResult(status="invalid")
                else:
                    _atomic(backup, original)
                _atomic(path, content)
                self.loaded_revision = revision(content)
                self.config.settings.agents = list(profiles.values())
                self.config.settings.connector.profile_operation = identity
                self.clients.pop(alias, None)
                self.managers.pop(alias, None)
                if alias in profiles:
                    self.clients[alias] = AgentClient(
                        profiles[alias], pin_dir=self.config.settings.resolved_data_dir() / "pins"
                    )
                self.policies.clear()
                self.policies.update(local_policies(self.config))
                self.maintenance.profiles = self.config.settings.agents
                safe_to_resume = True
                result = self.snapshot(content)
                if request.profile is not None and request.profile.candidate is not None:
                    # Only initialize: no session/new, prompts, hooks or model requests.
                    client = self.clients[alias]
                    try:
                        await asyncio.wait_for(client.connect(), 8)
                        result.initialization = "ok"
                    except (OSError, ValueError, AgentError, TimeoutError):
                        result.initialization = "failed"
                    finally:
                        await client.close()
                return result
            finally:
                # Failed/unknown cleanup remains fenced until explicit local recovery.
                if safe_to_resume:
                    self.maintenance.apply("resume", token)
