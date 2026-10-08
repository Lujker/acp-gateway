"""Validated local connector launch options shared by foreground and service modes."""

from dataclasses import dataclass
from pathlib import Path

from acp_gateway.connectors.control import normalize_pin, validate_url
from acp_gateway.connectors.policy import LocalAgentPolicy
from acp_gateway.connectors.protocol import AgentManifest, Hello


def local_policies(config):
    profiles = config.settings.agents
    if not profiles:
        raise ValueError("configure at least one local agent before registering a computer")
    if any(p.backend != "remote" for p in profiles):
        raise ValueError("a computer connector requires direct local agent profiles")
    missing = config.missing_agent_secrets()
    if missing:
        raise ValueError("missing agent secrets: " + ", ".join(missing))
    return {p.alias: LocalAgentPolicy.from_profile(p) for p in profiles}


@dataclass(frozen=True)
class ConnectorLaunch:
    dispatcher_url: str
    computer_id: str
    token_file: Path
    tls_fingerprint: str | None = None

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
                computer_id=self.computer_id,
                agents=[
                    AgentManifest(alias=p.alias, display_name=p.display_name)
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
