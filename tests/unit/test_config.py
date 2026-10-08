import textwrap

import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile, GatewaySettings, is_loopback_host, load_config
from acp_gateway.log import redact_text

FINGERPRINT_HEX = "ab" * 32
AGENT_SECRET = "agent-" + "secret-for-tests"


def write_config(path, body):
    path.write_text(textwrap.dedent(body))
    return path


def agent(**overrides):
    fields = {"alias": "work", "url": "ws://127.0.0.1:3284/acp", "default_cwd": "/w"}
    return AgentProfile(**(fields | overrides))


def test_defaults_without_files():
    cfg = load_config()
    assert cfg.config_path is None
    assert cfg.env_file is None
    assert cfg.settings.agents == []
    assert cfg.settings.gateway.host == "127.0.0.1"
    assert cfg.settings.policy.allow_always_approval is False


def test_loads_yaml_and_env_secrets(tmp_path):
    write_config(
        tmp_path / "config.yaml",
        """
        agents:
          - alias: work
            kind: goose
            url: wss://work-laptop.lan:3000/acp
            secret_env: AGENT_WORK_SECRET
            tls_fingerprint: ""
            default_cwd: /home/user/work
        logging:
          level: debug
        """,
    )
    (tmp_path / ".env").write_text(f"AGENT_WORK_SECRET={AGENT_SECRET}\n")

    cfg = load_config()

    work = cfg.settings.agent("work")
    assert work.kind == "goose"
    assert work.uses_tls
    assert work.tls_fingerprint is None
    assert cfg.settings.logging.level == "DEBUG"
    assert cfg.secrets.get("AGENT_WORK_SECRET").get_secret_value() == AGENT_SECRET
    assert cfg.missing_agent_secrets() == []
    # Loading registers referenced secrets for log redaction.
    assert AGENT_SECRET not in redact_text(f"connecting with {AGENT_SECRET}")


def test_environment_overrides_yaml_and_env_file(tmp_path, monkeypatch):
    write_config(tmp_path / "config.yaml", "gateway:\n  port: 9000\n")
    (tmp_path / ".env").write_text("AGENT_WORK_SECRET=from-file\n")
    monkeypatch.setenv("ACPGW_GATEWAY__PORT", "9100")
    monkeypatch.setenv("AGENT_WORK_SECRET", "from-environment")

    cfg = load_config()

    assert cfg.settings.gateway.port == 9100
    assert cfg.secrets.get("AGENT_WORK_SECRET").get_secret_value() == "from-environment"


def test_explicit_paths_via_environment(tmp_path, monkeypatch):
    conf = write_config(tmp_path / "custom.yaml", "gateway:\n  port: 9200\n")
    monkeypatch.setenv("ACPGW_CONFIG", str(conf))
    assert load_config().settings.gateway.port == 9200


def test_platform_config_dir_is_used(tmp_path):
    platform_dir = tmp_path / "platform-config"
    platform_dir.mkdir()
    write_config(platform_dir / "config.yaml", "gateway:\n  port: 9300\n")
    assert load_config().settings.gateway.port == 9300


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(config_path=tmp_path / "absent.yaml")


def test_missing_agent_secret_is_reported(tmp_path):
    write_config(
        tmp_path / "config.yaml",
        """
        agents:
          - alias: work
            url: ws://127.0.0.1:3284/acp
            secret_env: AGENT_WORK_SECRET
            default_cwd: /w
        """,
    )
    assert load_config().missing_agent_secrets() == ["work: AGENT_WORK_SECRET"]


def test_unknown_keys_are_rejected(tmp_path):
    write_config(tmp_path / "config.yaml", "gateway:\n  hots: 127.0.0.1\n")
    with pytest.raises(ValidationError):
        load_config()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "127.0.0.2"])
def test_loopback_hosts(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "work-laptop.lan", "::"])  # noqa: S104
def test_gateway_must_listen_on_loopback(host):
    with pytest.raises(ValidationError, match="loopback"):
        GatewaySettings(host=host)


def test_plaintext_to_remote_host_is_rejected():
    with pytest.raises(ValidationError, match="plaintext"):
        agent(url="ws://192.168.1.10:3000/acp")


def test_plaintext_to_remote_host_with_explicit_opt_in():
    assert agent(url="ws://192.168.1.10:3000/acp", allow_insecure_transport=True)


def test_plaintext_on_loopback_is_allowed():
    assert not agent(url="ws://127.0.0.1:3284/acp").uses_tls


@pytest.mark.parametrize(
    "raw",
    [FINGERPRINT_HEX, FINGERPRINT_HEX.upper(), ":".join(["AB"] * 32), " ".join(["ab"] * 32)],
)
def test_fingerprint_is_normalized(raw):
    profile = agent(url="wss://h.lan/acp", tls_fingerprint=raw)
    assert profile.tls_fingerprint == ":".join(["AB"] * 32)


def test_bad_fingerprint_is_rejected():
    with pytest.raises(ValidationError, match="SHA-256"):
        agent(url="wss://h.lan/acp", tls_fingerprint="AB:CD")


def test_fingerprint_requires_tls():
    with pytest.raises(ValidationError, match="requires wss"):
        agent(tls_fingerprint=FINGERPRINT_HEX)


@pytest.mark.parametrize("url", ["ftp://h/acp", "127.0.0.1:3284", "wss:///acp"])
def test_bad_agent_urls(url):
    with pytest.raises(ValidationError):
        agent(url=url)


@pytest.mark.parametrize("alias", ["Work", "1work", "work-goose", ""])
def test_bad_aliases(alias):
    with pytest.raises(ValidationError):
        agent(alias=alias)


def test_duplicate_aliases_are_rejected(tmp_path):
    write_config(
        tmp_path / "config.yaml",
        """
        agents:
          - {alias: work, url: "ws://127.0.0.1:1/acp", default_cwd: /w}
          - {alias: work, url: "ws://127.0.0.1:2/acp", default_cwd: /w}
        """,
    )
    with pytest.raises(ValidationError, match="duplicate"):
        load_config()


def test_bad_secret_env_name():
    with pytest.raises(ValidationError, match="environment variable"):
        agent(secret_env="lower-case")


def test_example_config_is_valid(monkeypatch):
    from pathlib import Path

    example = Path(__file__).parents[2] / "config.example.yaml"
    cfg = load_config(config_path=example)
    assert [a.alias for a in cfg.settings.agents] == ["work"]


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("https://work.lan:3284", "wss://work.lan:3284/acp"),
        ("https://work.lan:3284/", "wss://work.lan:3284/acp"),
        ("wss://work.lan:3284/acp", "wss://work.lan:3284/acp"),
        ("http://127.0.0.1:3284", "ws://127.0.0.1:3284/acp"),
        ("ws://127.0.0.1:3284/custom", "ws://127.0.0.1:3284/custom"),
    ],
)
def test_goose_acp_endpoint(url, endpoint):
    assert agent(url=url, kind="goose").acp_endpoint == endpoint


def test_generic_endpoint_keeps_empty_path():
    assert agent(url="ws://127.0.0.1:9000").acp_endpoint == "ws://127.0.0.1:9000"


def test_goose_session_mode_defaults_to_smart_approve():
    assert agent(kind="goose").session_mode == "smart_approve"
    assert agent(kind="goose", session_mode="approve").session_mode == "approve"
    assert agent().session_mode is None


@pytest.mark.parametrize(
    "url",
    [
        "wss://alice:hunter2pass@work.lan:3284/acp",
        "wss://only-user@work.lan/acp",
        "ws://127.0.0.1@evil.example/acp",
        "ws://evil.example\\@127.0.0.1/acp",
    ],
)
def test_agent_url_with_credentials_is_rejected(url):
    with pytest.raises(ValidationError, match="must not contain credentials") as error:
        agent(url=url)
    assert "hunter2pass" not in str(error.value)


def test_config_error_does_not_echo_url_credentials(tmp_path):
    write_config(
        tmp_path / "config.yaml",
        """
        agents:
          - {alias: work, url: "wss://alice:hunter2pass@work.lan/acp", default_cwd: /w}
        """,
    )
    with pytest.raises(ValidationError, match="must not contain credentials") as error:
        load_config()
    assert "hunter2pass" not in str(error.value)


def test_env_file_values_are_not_interpolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXPANDED", "expanded")
    write_config(
        tmp_path / "config.yaml",
        """
        agents:
          - alias: work
            url: ws://127.0.0.1:3284/acp
            secret_env: AGENT_WORK_SECRET
            default_cwd: /w
        """,
    )
    (tmp_path / ".env").write_text("AGENT_WORK_SECRET=pa${AGENT_EXPANDED}word\n")
    secret = load_config().secrets.get("AGENT_WORK_SECRET").get_secret_value()
    assert secret == "pa${AGENT_EXPANDED}word"
