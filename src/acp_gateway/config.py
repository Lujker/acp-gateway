"""Gateway configuration.

Settings sources, highest priority first:
1. environment variables ``ACPGW_<SECTION>__<FIELD>`` (e.g. ``ACPGW_GATEWAY__PORT``);
2. ``config.yaml``;
3. defaults.

The ``.env`` file holds secrets only. Secrets never live in ``config.yaml``: a
profile names the variable that holds the secret (``secret_env``), and the
value is read from the process environment or the ``.env`` file.
"""

from __future__ import annotations

import ipaddress
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from acp_gateway import paths
from acp_gateway.log import register_secret

CONFIG_PATH_ENV = "ACPGW_CONFIG"
ENV_FILE_ENV = "ACPGW_ENV_FILE"

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_HEX_FINGERPRINT = re.compile(r"^[0-9A-F]{64}$")


def is_loopback_host(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewaySettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    api_token_env: str = "ACPGW_API_TOKEN"  # noqa: S105 (variable name, not a secret)
    mcp_token_env: str = "ACPGW_MCP_TOKEN"  # noqa: S105 (variable name, not a secret)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, host: str) -> str:
        # Invariant: the local API and MCP endpoint are never exposed beyond this host.
        if not is_loopback_host(host):
            raise ValueError(f"gateway.host must be a loopback address, got {host!r}")
        return host

    @field_validator("api_token_env", "mcp_token_env")
    @classmethod
    def _env_name(cls, name: str) -> str:
        return _validate_env_name(name)


class ConnectorSettings(_Section):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    connect_path: str = "/connect"
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @model_validator(mode="after")
    def _listener(self):
        from acp_gateway.connectors.control import validate_connect_path

        validate_connect_path(self.connect_path)
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("provide both connector.tls_cert and connector.tls_key")
        return self


class UpdateSettings(_Section):
    # Out-of-box: the independent updater is prepared on setup/start. An explicit
    # `updates.enabled: false` remains an opt-out and must be honoured everywhere.
    enabled: bool = True
    spool_dir: Path | None = None

    @field_validator("spool_dir")
    @classmethod
    def absolute_spool(cls, value):
        if value is not None and not value.is_absolute():
            raise ValueError("updates.spool_dir must be absolute")
        return value


class HermesAttachSettings(_Section):
    lock_dir: Path | None = None
    role: Literal["desktop-serve", "serve"] = "desktop-serve"
    profile: str = Field(default="default", min_length=1, max_length=128)

    @field_validator("lock_dir")
    @classmethod
    def _absolute_lock_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Hermes lock_dir must be absolute in the agent host OS")
        return value


def _wsl_path(value: str) -> str:
    """Validate a target-side Linux path without host filesystem lookups."""
    from pathlib import PurePosixPath

    if (
        not value
        or len(value) > 4096
        or not value.isprintable()
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or ":" in value
        or ".." in PurePosixPath(value).parts
    ):
        raise ValueError("WSL execution requires an absolute Linux path without traversal")
    return value


class WslSettings(_Section):
    """Explicit Linux execution identity, owned by the Windows connector."""

    distribution: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=32)
    python: str = Field(default="/usr/bin/python3", min_length=1, max_length=4096)
    home: str | None = Field(default=None, min_length=1, max_length=4096)
    agent_home: str | None = Field(default=None, min_length=1, max_length=4096)
    path: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("distribution")
    @classmethod
    def _distribution(cls, value: str) -> str:
        if value != value.strip() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", value):
            raise ValueError("WSL distribution must be an explicit safe name")
        return value

    @field_validator("user")
    @classmethod
    def _user(cls, value: str) -> str:
        if value == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]*", value):
            raise ValueError("WSL execution requires an explicit non-root Linux user")
        return value

    @field_validator("python", "home", "agent_home")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return None if value is None else _wsl_path(value)

    @field_validator("path")
    @classmethod
    def _search_paths(cls, values: list[str]) -> list[str]:
        return [_wsl_path(value) for value in values]


class CodexSettings(_Section):
    mode: Literal["safe", "work"] = "safe"
    wsl: WslSettings | None = None
    executable: str = Field(default="codex", min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    sandbox: Literal["read-only", "workspace-write"] = "read-only"

    @model_validator(mode="before")
    @classmethod
    def _work_defaults(cls, values):
        if isinstance(values, dict) and values.get("mode") == "work":
            values = {"turn_timeout": 3600, "sandbox": "workspace-write", **values}
        return values

    @model_validator(mode="after")
    def _wsl_executable(self):
        if self.wsl is not None:
            _wsl_path(self.executable)
        return self

    request_timeout: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    turn_timeout: float = Field(default=600, gt=0, le=3600, allow_inf_nan=False)

    @field_validator("executable")
    @classmethod
    def _executable(cls, value):
        from pathlib import PurePosixPath, PureWindowsPath

        if (
            not value.isprintable()
            or value.startswith("-")
            or "://" in value
            or value.startswith(("//", "\\"))
            or any(c.isspace() for c in PureWindowsPath(value).name)
            or any(c in value for c in "\"'`;|&<>")
        ):
            raise ValueError("Codex executable must be a local executable name or absolute path")
        if not (
            PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
        ) and not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
            raise ValueError("Codex executable must be a local executable name or absolute path")
        return value

    @field_validator("model")
    @classmethod
    def _model(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
            raise ValueError("invalid Codex model identifier")
        return value


class ClaudeCodeSettings(_Section):
    mode: Literal["safe", "work"] = "safe"
    wsl: WslSettings | None = None
    executable: str = Field(default="claude", min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    request_timeout: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    turn_timeout: float = Field(default=600, gt=0, le=3600, allow_inf_nan=False)

    max_turns: int = Field(default=16, ge=1, le=100)
    max_sessions: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="before")
    @classmethod
    def _work_defaults(cls, values):
        if isinstance(values, dict) and values.get("mode") == "work":
            values = {"turn_timeout": 3600, "max_turns": 100, **values}
        return values

    @model_validator(mode="after")
    def _wsl_executable(self):
        if self.wsl is not None:
            _wsl_path(self.executable)
        return self

    @field_validator("executable")
    @classmethod
    def _executable(cls, value):
        from pathlib import PurePosixPath, PureWindowsPath

        if (
            not value.isprintable()
            or value.startswith("-")
            or "://" in value
            or value.startswith(("//", "\\"))
            or any(c.isspace() for c in PureWindowsPath(value).name)
            or any(c in value for c in "\"'`;|&<>")
        ):
            raise ValueError(
                "Claude Code executable must be a local executable name or absolute path"
            )
        if not (
            PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
        ) and not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
            raise ValueError(
                "Claude Code executable must be a local executable name or absolute path"
            )
        return value

    @field_validator("model")
    @classmethod
    def _model(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
            raise ValueError("invalid Claude Code model identifier")
        return value


class AgentProfile(_Section):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    title: str | None = None
    kind: Literal["goose", "generic", "hermes", "codex", "claude_code"] = "generic"
    backend: Literal["remote", "connector", "hermes", "codex", "claude_code"] = "remote"
    claude_code: ClaudeCodeSettings | None = None
    codex: CodexSettings | None = None
    hermes: HermesAttachSettings | None = None
    computer_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    url: str = ""
    secret_env: str | None = None
    tls_fingerprint: str | None = None
    allow_insecure_transport: bool = False
    default_cwd: str
    # Session mode applied after session/new and session/load. Goose starts ACP
    # sessions in "auto" (no approvals), so goose profiles default to smart_approve.
    session_mode: str | None = None

    @property
    def display_name(self) -> str:
        return self.title or self.address

    @property
    def address(self) -> str:
        return f"{self.computer_id}/{self.alias}" if self.backend == "connector" else self.alias

    @property
    def uses_tls(self) -> bool:
        return urlsplit(self.url).scheme in ("wss", "https")

    @property
    def acp_endpoint(self) -> str:
        """WebSocket URL of the ACP endpoint.

        ``http(s)`` becomes ``ws(s)``. Goose Desktop takes a base URL, so for goose
        profiles an empty path means ``/acp``.
        """
        parts = urlsplit(self.url)
        scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
        path = parts.path
        if self.kind == "goose" and path in ("", "/"):
            path = "/acp"
        return urlunsplit((scheme, parts.netloc, path, parts.query, ""))

    @field_validator("secret_env")
    @classmethod
    def _env_name(cls, name: str | None) -> str | None:
        return None if name is None else _validate_env_name(name)

    @field_validator("tls_fingerprint")
    @classmethod
    def _normalize_fingerprint(cls, value: str | None) -> str | None:
        if not value:
            return None
        hex_digits = re.sub(r"[\s:]", "", value).upper()
        if not _HEX_FINGERPRINT.match(hex_digits):
            raise ValueError("tls_fingerprint must be a SHA-256 fingerprint (64 hex digits)")
        return ":".join(hex_digits[i : i + 2] for i in range(0, 64, 2))

    @model_validator(mode="after")
    def _default_session_mode(self) -> AgentProfile:
        if self.session_mode is None and self.kind == "goose":
            self.session_mode = "smart_approve"
        return self

    @model_validator(mode="after")
    def _check_transport(self) -> AgentProfile:
        if self.backend == "claude_code":
            from acp_gateway.connectors.policy import LocalPolicyError, _cwd

            if (
                self.kind != "claude_code"
                or self.hermes is not None
                or self.codex is not None
                or any(
                    (
                        self.computer_id,
                        self.url,
                        self.secret_env,
                        self.tls_fingerprint,
                        self.allow_insecure_transport,
                        self.session_mode,
                    )
                )
            ):
                raise ValueError(
                    "Claude Code requires kind: claude_code and local managed settings only"
                )
            try:
                _cwd(self.default_cwd)
            except LocalPolicyError:
                raise ValueError("Claude Code requires an absolute local cwd") from None
            if self.claude_code is None:
                self.claude_code = ClaudeCodeSettings()
            if self.claude_code.wsl is not None:
                _wsl_path(self.default_cwd)
            return self
        if self.claude_code is not None or (
            self.kind == "claude_code" and self.backend != "connector"
        ):
            raise ValueError(
                "Claude Code settings require backend: claude_code; relay profiles use kind only"
            )
        if self.kind == "claude_code" and (
            self.session_mode or self.hermes is not None or self.codex is not None
        ):
            raise ValueError("Claude Code relay profiles cannot configure other native transports")
        if self.backend == "codex":
            from acp_gateway.connectors.policy import LocalPolicyError, _cwd

            if (
                self.kind != "codex"
                or self.hermes is not None
                or any(
                    (
                        self.computer_id,
                        self.url,
                        self.secret_env,
                        self.tls_fingerprint,
                        self.allow_insecure_transport,
                        self.session_mode,
                    )
                )
            ):
                raise ValueError("Codex requires kind: codex and local managed settings only")
            try:
                _cwd(self.default_cwd)
            except LocalPolicyError:
                raise ValueError("Codex requires an absolute local cwd") from None
            if self.codex is None:
                self.codex = CodexSettings()
            if self.codex.wsl is not None:
                _wsl_path(self.default_cwd)
            return self
        if self.codex is not None or (self.kind == "codex" and self.backend != "connector"):
            raise ValueError("Codex settings require backend: codex; relay profiles use kind only")
        if self.kind == "codex" and (self.session_mode or self.hermes is not None):
            raise ValueError("Codex relay profiles cannot configure session modes or Hermes")
        if self.backend == "hermes":
            if self.kind != "hermes":
                raise ValueError("backend: hermes requires kind: hermes")
            if (
                self.computer_id
                or self.url
                or self.secret_env
                or self.tls_fingerprint
                or self.allow_insecure_transport
                or self.session_mode
            ):
                raise ValueError("Hermes attach uses local discovery and dedicated conversations")
            if self.hermes is None:
                self.hermes = HermesAttachSettings()
            return self
        if self.hermes is not None:
            raise ValueError("hermes settings require backend: hermes")
        if self.kind == "hermes" and self.backend == "remote":
            raise ValueError("Hermes JSON-RPC requires backend: hermes, not a remote ACP URL")
        if self.backend == "connector":
            if self.computer_id is None:
                raise ValueError("connector agent requires computer_id")
            if self.url or self.secret_env or self.tls_fingerprint or self.allow_insecure_transport:
                raise ValueError("connector agent credentials and URL belong on the computer")
            return self
        if self.computer_id is not None:
            raise ValueError("computer_id requires backend: connector")
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss", "http", "https"):
            raise ValueError(
                f"agent {self.alias!r}: url must be ws://, wss://, http:// or https://"
            )
        if not parts.hostname:
            raise ValueError(f"agent {self.alias!r}: url has no host")
        if parts.username is not None or parts.password is not None:
            # The URL is logged and shown by `config check`; credentials belong in .env.
            raise ValueError(
                f"agent {self.alias!r}: url must not contain credentials; "
                "put the secret in .env and reference it with secret_env"
            )
        if self.tls_fingerprint and not self.uses_tls:
            raise ValueError(f"agent {self.alias!r}: tls_fingerprint requires wss:// or https://")
        if (
            not self.uses_tls
            and not is_loopback_host(parts.hostname)
            and not self.allow_insecure_transport
        ):
            raise ValueError(
                f"agent {self.alias!r}: plaintext transport to a non-loopback host; "
                "use wss:// or set allow_insecure_transport: true"
            )
        return self


class PolicySettings(_Section):
    allow_new_sessions: bool = True
    allow_cancel: bool = True
    allow_approvals: bool = True
    allow_always_approval: bool = False
    allow_file_upload: bool = False
    allow_file_download: bool = False
    max_prompt_length: int = Field(default=50_000, gt=0)
    max_response_length: int = Field(default=100_000, gt=0)
    approval_timeout_seconds: int = Field(default=300, gt=0)
    approver_channels: list[str] = Field(default_factory=lambda: ["cli"])


class RateLimitSettings(_Section):
    # One aggregate budget per channel, shared by all its threads and agents.
    work_requests: int = Field(default=60, ge=1, le=10000)
    window_seconds: int = Field(default=60, ge=1, le=3600)


class RetentionSettings(_Section):
    jobs_days: int = Field(default=7, ge=1, le=36500)
    # Audit expiry is opt-in; existing installations retain all decisions.
    approval_audit_days: int | None = Field(default=None, ge=1, le=36500)
    sweep_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    batch_size: int = Field(default=1000, ge=1, le=10000)


class LoggingSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["auto", "console", "json"] = "auto"
    file: Path | None = None
    max_bytes: int = Field(default=5_000_000, ge=1024)
    backup_count: int = Field(default=3, ge=1, le=20)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class PairingSettings(_Section):
    enabled: bool = True
    request_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    max_requests: int = Field(default=256, ge=1, le=1024)


class TelegramSettings(_Section):
    enabled: bool = False
    status_enabled: bool = True
    status_interval_seconds: int = Field(default=2, ge=1, le=60)
    token_env: str = "TELEGRAM_BOT_TOKEN"  # noqa: S105
    allowed_user_ids: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("token_env")
    @classmethod
    def _env_name(cls, name: str) -> str:
        return _validate_env_name(name)

    @field_validator("allowed_user_ids")
    @classmethod
    def _ids(cls, values: list[int]) -> list[int]:
        if any(v <= 0 for v in values) or len(values) != len(set(values)):
            raise ValueError("allowed_user_ids must contain unique positive user IDs")
        return values

    @model_validator(mode="after")
    def _allowlist(self):
        if self.enabled and not self.allowed_user_ids:
            raise ValueError("enabled Telegram requires an explicit user allowlist")
        return self


_yaml_path: ContextVar[Path | None] = ContextVar("_yaml_path", default=None)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACPGW_",
        env_nested_delimiter="__",
        extra="forbid",
        # Validation errors are printed; never echo a rejected value (it may be a credential).
        hide_input_in_errors=True,
    )

    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    connector: ConnectorSettings = Field(default_factory=ConnectorSettings)
    pairing: PairingSettings = Field(default_factory=PairingSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)
    agents: list[AgentProfile] = Field(default_factory=list)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    rate_limits: RateLimitSettings = Field(default_factory=RateLimitSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    data_dir: Path | None = None

    @model_validator(mode="after")
    def _unique_aliases(self) -> Settings:
        aliases = [a.address for a in self.agents]
        duplicates = sorted({a for a in aliases if aliases.count(a) > 1})
        if duplicates:
            raise ValueError(f"duplicate agent aliases: {', '.join(duplicates)}")
        tool_prefixes = [alias.replace("/", "__") for alias in aliases]
        if len(tool_prefixes) != len(set(tool_prefixes)):
            raise ValueError("agent addresses have conflicting MCP tool names")
        if any(a.backend == "connector" for a in self.agents) and not self.connector.enabled:
            raise ValueError("connector agents require connector.enabled: true")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # dotenv is deliberately not a settings source: .env holds secrets only (SecretStore).
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        yaml_path = _yaml_path.get()
        if yaml_path is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        return tuple(sources)

    def agent(self, alias: str) -> AgentProfile:
        for profile in self.agents:
            if profile.address == alias:
                return profile
        raise KeyError(alias)

    def resolved_data_dir(self) -> Path:
        return self.data_dir or paths.data_dir()


class SecretStore:
    """Secrets referenced by name; the process environment wins over the ``.env`` file."""

    def __init__(self, env_file_values: dict[str, str | None] | None = None) -> None:
        self._file_values = {k: v for k, v in (env_file_values or {}).items() if v}

    def get(self, name: str) -> SecretStr | None:
        value = os.environ.get(name) or self._file_values.get(name)
        return SecretStr(value) if value else None

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    secrets: SecretStore
    config_path: Path | None
    env_file: Path | None

    def referenced_secret_names(self) -> list[str]:
        names = [self.settings.gateway.api_token_env, self.settings.gateway.mcp_token_env]
        names += [a.secret_env for a in self.settings.agents if a.secret_env]
        if self.settings.telegram.enabled:
            names.append(self.settings.telegram.token_env)
        return names

    def missing_agent_secrets(self) -> list[str]:
        return [
            f"{a.alias}: {a.secret_env}"
            for a in self.settings.agents
            if a.secret_env and a.secret_env not in self.secrets
        ]


def _validate_env_name(name: str) -> str:
    if not _ENV_NAME.match(name):
        raise ValueError(f"{name!r} is not a valid environment variable name")
    return name


def _first_existing(*candidates: Path) -> Path | None:
    return next((p for p in candidates if p.is_file()), None)


def find_config_file(explicit: Path | None = None) -> Path | None:
    """Project files require explicit opt-in; cwd must not redirect installed secrets."""
    if explicit is not None:
        return explicit
    if env := os.environ.get(CONFIG_PATH_ENV):
        return Path(env)
    return _first_existing(paths.config_dir() / "config.yaml")


def find_env_file(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    if env := os.environ.get(ENV_FILE_ENV):
        return Path(env)
    return _first_existing(paths.config_dir() / ".env")


def load_config(config_path: Path | None = None, env_file: Path | None = None) -> AppConfig:
    """Load settings and secrets; every referenced secret is registered for log redaction."""
    config_path = find_config_file(config_path)
    env_file = find_env_file(env_file)
    for path in (config_path, env_file):
        if path is not None and not path.is_file():
            raise FileNotFoundError(f"file not found: {path}")

    token = _yaml_path.set(config_path)
    try:
        settings = Settings()
    finally:
        _yaml_path.reset(token)

    secrets = SecretStore(dotenv_values(env_file, interpolate=False) if env_file else None)
    app_config = AppConfig(settings, secrets, config_path, env_file)
    for name in app_config.referenced_secret_names():
        if (secret := secrets.get(name)) is not None:
            register_secret(secret.get_secret_value())
    return app_config
