import pytest

import ssh_tunnel as tunnel


@pytest.mark.parametrize("action", ["start", "stop"])
def test_control_disabled_even_for_corrupt_marker(tmp_path, action):
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "owner.json").write_text("[]")
    calls = []
    with pytest.raises(ValueError, match="disabled"):
        tunnel.control(action, "demo", tmp_path, tmp_path, True, calls.append)
    assert not calls


def test_prepare_is_disabled_without_writing(tmp_path):
    tmp_path = tmp_path / "owned"
    tmp_path.mkdir()
    with pytest.raises(ValueError, match="disabled"):
        tunnel.prepare(
            name="demo",
            base=tmp_path,
            units=tmp_path,
            host="gateway",
            user="alice",
            port=22,
            jump="jump",
            jump_user="alice",
            jump_port=22,
            key=tmp_path / "key",
            known_hosts=tmp_path / "hosts",
            local_port=22,
            remote_port=8123,
            confirm=True,
        )
    assert not list(tmp_path.iterdir())


def test_preview_reverse_forward_and_jump_config():
    config, command = tunnel.preview(
        name="demo",
        host="gateway.example",
        user="alice",
        port=22,
        jump="jump.example",
        jump_user="bob",
        jump_port=2222,
        key="/home/user/key",
        known_hosts="/home/user/known_hosts",
        local_port=22,
        remote_port=8123,
        config="/home/user/ssh_config",
    )
    assert "ProxyJump acpgw-jump" in config
    assert "StrictHostKeyChecking yes" in config
    assert "-R 0.0.0.0:8123:127.0.0.1:22" in command
    assert " -F /home/user/ssh_config " in command


def test_preview_rejects_injection():
    with pytest.raises(ValueError):
        tunnel.preview(
            name="demo",
            host="bad\nHost evil",
            user="alice",
            port=22,
            jump="jump",
            jump_user="bob",
            jump_port=22,
            key="/home/user/key",
            known_hosts="/home/user/known_hosts",
            local_port=22,
            remote_port=8123,
            config="/home/user/ssh_config",
        )


def test_openssh_effective_jump_config_without_network(tmp_path):
    import shutil
    import subprocess

    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH unavailable")
    config_path = tmp_path / "ssh_config"
    if not str(config_path).startswith("/"):
        pytest.skip("POSIX config path required")
    config, _ = tunnel.preview(
        name="demo",
        host="gateway.example",
        user="alice",
        port=22,
        jump="jump.example",
        jump_user="bob",
        jump_port=2222,
        key="/home/user/key",
        known_hosts="/home/user/known_hosts",
        local_port=22,
        remote_port=8123,
        config=str(config_path),
    )
    config_path.write_text(config)

    def effective(alias):
        return subprocess.run(
            [ssh, "-G", "-F", str(config_path), alias],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.lower()

    target = effective("acpgw-target")
    jump = effective("acpgw-jump")
    assert "proxyjump acpgw-jump" in target
    assert "hostname gateway.example" in target
    assert "hostname jump.example" in jump
    assert "port 2222" in jump
    assert "user bob" in jump
    assert "userknownhostsfile /home/user/known_hosts" in jump
