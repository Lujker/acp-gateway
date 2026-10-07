"""Policy checks shared by every channel; channel adapters cannot bypass them."""

from acp_gateway.channels.base import Channel
from acp_gateway.config import PolicySettings
from acp_gateway.core.errors import PolicyDenied


class Policy:
    def __init__(self, settings: PolicySettings | None = None) -> None:
        self.settings = (settings or PolicySettings()).model_copy(deep=True)

    def check_new_session(self) -> None:
        if not self.settings.allow_new_sessions:
            raise PolicyDenied("creating new sessions is disabled by policy")

    def check_prompt(self, text: str) -> None:
        if len(text) > self.settings.max_prompt_length:
            raise PolicyDenied(f"prompt exceeds {self.settings.max_prompt_length} characters")

    def check_cancel(self) -> None:
        if not self.settings.allow_cancel:
            raise PolicyDenied("cancelling jobs is disabled by policy")

    def check_file_upload(self) -> None:
        if not self.settings.allow_file_upload:
            raise PolicyDenied("file uploads are disabled by policy")

    def check_file_download(self) -> None:
        if not self.settings.allow_file_download:
            raise PolicyDenied("file downloads are disabled by policy")

    def can_approve(self, channel: Channel) -> bool:
        return (
            self.settings.allow_approvals
            and channel.name in self.settings.approver_channels
            and channel.name not in {"mcp", "hermes"}
            and channel.can_approve
            and channel.connected
        )

    def option_allowed(self, kind: str, channel: str) -> bool:
        if kind == "allow_always":
            return self.settings.allow_always_approval and channel == "cli"
        return kind in {"allow_once", "reject_once"}
