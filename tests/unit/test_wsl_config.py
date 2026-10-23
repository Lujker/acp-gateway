"""Explicit WSL execution settings; never infer another execution environment."""

import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile


def profile(kind="codex", **overrides):
    settings = {
        "executable": "/home/alice/.local/bin/" + ("claude" if kind == "claude_code" else "codex"),
        "wsl": {"distribution": "Ubuntu-22.04", "user": "alice"},
    }
    settings.update(overrides)
    return AgentProfile(
        alias="code", kind=kind, backend=kind, default_cwd="/home/alice/project", **{kind: settings}
    )


def test_explicit_wsl_profile_roundtrips_without_changing_native_defaults():
    configured = profile()
    assert configured.codex.wsl.distribution == "Ubuntu-22.04"
    assert configured.codex.wsl.user == "alice"
    assert configured.codex.wsl.python == "/usr/bin/python3"
    assert configured.codex.wsl.agent_home is None
    assert configured.codex.wsl.path == []
    assert (
        AgentProfile.model_validate(configured.model_dump()).model_dump() == configured.model_dump()
    )
    assert (
        AgentProfile(alias="native", kind="codex", backend="codex", default_cwd="/work").codex.wsl
        is None
    )


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("distribution", "-Ubuntu"),
        ("distribution", " Ubuntu"),
        ("distribution", "Ubuntu\n"),
        ("distribution", "Ubuntu;whoami"),
        ("user", "root"),
        ("user", "--root"),
        ("user", "alice bob"),
        ("python", "python3"),
        ("python", "C:/python.exe"),
        ("python", "/usr/../bin/python3"),
        ("agent_home", "~/.codex"),
        ("agent_home", "//server/share"),
        ("agent_home", "/home/alice/../bob"),
        ("path", ["/bin:/private"]),
        ("path", ["relative"]),
        ("path", ["/home/alice\n"]),
    ],
)
def test_wsl_identity_and_paths_are_literal_and_fail_closed(kind, field, value):
    with pytest.raises(ValidationError):
        profile(kind, wsl={"distribution": "Ubuntu-22.04", "user": "alice", field: value})


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
@pytest.mark.parametrize("executable", ["codex", "C:/bin/codex.exe", "/bin/../bin/codex"])
def test_wsl_requires_explicit_absolute_linux_executable(kind, executable):
    with pytest.raises(ValidationError):
        profile(kind, executable=executable)


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_wsl_profile_rejects_windows_cwd(kind):
    configured = profile(kind).model_dump()
    configured["default_cwd"] = "C:/projects/example"
    with pytest.raises(ValidationError):
        AgentProfile.model_validate(configured)


def test_wsl_optional_profile_paths_and_spaced_cwd_preserve_exact_values():
    configured = profile(
        wsl={
            "distribution": "Custom Ubuntu",
            "user": "alice",
            "agent_home": "/home/alice/profiles/work",
            "path": ["/home/alice/node/bin", "/opt/extra tools/bin"],
        }
    ).model_dump()
    configured["default_cwd"] = "/home/alice/my project"
    result = AgentProfile.model_validate(configured)
    assert result.codex.wsl.distribution == "Custom Ubuntu"
    assert result.codex.wsl.agent_home == "/home/alice/profiles/work"
    assert result.default_cwd == "/home/alice/my project"


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_explicit_linux_home_override_is_target_side_and_validated(kind):
    result = profile(
        kind, wsl={"distribution": "Ubuntu-22.04", "user": "alice", "home": "/isolated/home"}
    )
    assert getattr(result, kind).wsl.home == "/isolated/home"
    with pytest.raises(ValidationError):
        profile(
            kind, wsl={"distribution": "Ubuntu-22.04", "user": "alice", "home": "C:/Users/alice"}
        )
