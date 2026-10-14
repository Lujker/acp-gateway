"""Validated local connector launch options shared by foreground and service modes."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from acp_gateway.connectors.control import normalize_pin, validate_url
from acp_gateway.connectors.policy import LocalAgentPolicy
from acp_gateway.connectors.protocol import AgentManifest, Hello


def local_policies(config):
    profiles = config.settings.agents
    if not profiles:
        raise ValueError("configure at least one local agent before registering a computer")
    if any(p.backend not in {"remote", "hermes"} for p in profiles):
        raise ValueError("a computer connector requires direct local agent profiles")
    missing = config.missing_agent_secrets()
    if missing:
        raise ValueError("missing agent secrets: " + ", ".join(missing))
    return {
        p.alias: LocalAgentPolicy.from_profile(
            p, max_prompt_length=config.settings.policy.max_prompt_length
        )
        for p in profiles
    }


@dataclass(frozen=True)
class ConnectorLaunch:
    dispatcher_url: str
    computer_id: str
    token_file: Path
    tls_fingerprint: str | None = None

    @classmethod
    def from_saved(cls, config):
        from acp_gateway.setup_state import read

        state = read(config.config_path)
        if state.get("role") != "connector" or not state.get("connection"):
            raise ValueError(
                "No saved connector connection; run acpgw setup --role connector "
                "or provide all connector launch options explicitly"
            )
        connection = state["connection"]
        token = Path(connection["token_file"])
        if not token.is_absolute():
            raise ValueError("Saved connector key path must be absolute; rerun acpgw connect")
        return cls.from_args(
            SimpleNamespace(
                dispatcher_url=connection["dispatcher_url"],
                computer_id=connection["computer_id"],
                token_file=token,
                tls_fingerprint=connection.get("tls_fingerprint"),
            ),
            config,
        )

    @classmethod
    def from_args(cls, args, config):
        if not args.dispatcher_url or not args.computer_id or args.token_file is None:
            raise ValueError("connector requires --dispatcher-url, --computer-id and --token-file")
        parts = validate_url(args.dispatcher_url)
        fingerprint = args.tls_fingerprint
        if fingerprint is not None:
            if parts.scheme != "wss":
                raise ValueError("a TLS fingerprint requires a wss:// dispatcher URL")
            fingerprint = normalize_pin(fingerprint)
        # Preserve the leaf path so read_credential can reject symlinks with O_NOFOLLOW.
        launch = cls(args.dispatcher_url, args.computer_id, args.token_file.absolute(), fingerprint)
        launch.hello(config)
        local_policies(config)
        return launch

    def hello(self, config):
        try:
            return Hello(
                version=2,
                computer_id=self.computer_id,
                agents=[
                    AgentManifest(
                        alias=p.alias,
                        display_name=p.display_name,
                        kind=p.kind,
                        default_cwd=p.default_cwd,
                        session_mode=p.session_mode,
                    )
                    for p in config.settings.agents
                ],
            )
        except ValueError:
            raise ValueError("invalid computer ID or agent manifest") from None

    def arguments(self):
        args = [
            "connector",
            "--dispatcher-url",
            self.dispatcher_url,
            "--computer-id",
            self.computer_id,
            "--token-file",
            str(self.token_file),
        ]
        if self.tls_fingerprint is not None:
            args += ["--tls-fingerprint", self.tls_fingerprint]
        return args
