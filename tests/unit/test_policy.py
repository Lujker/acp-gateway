import pytest

from acp_gateway.config import PolicySettings
from acp_gateway.core import PolicyDenied
from acp_gateway.core.policy import Policy


@pytest.mark.parametrize(
    "flag,method",
    [
        ("allow_new_sessions", "check_new_session"),
        ("allow_cancel", "check_cancel"),
        ("allow_file_upload", "check_file_upload"),
        ("allow_file_download", "check_file_download"),
    ],
)
def test_disabled_operation_is_rejected(flag, method):
    policy = Policy(PolicySettings(**{flag: False}))
    with pytest.raises(PolicyDenied):
        getattr(policy, method)()
    setattr(policy.settings, flag, True)
    getattr(policy, method)()


def test_prompt_length_boundary():
    policy = Policy(PolicySettings(max_prompt_length=3))
    policy.check_prompt("abc")
    with pytest.raises(PolicyDenied):
        policy.check_prompt("abcd")
