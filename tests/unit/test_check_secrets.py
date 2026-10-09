import subprocess

import pytest

import check_secrets

TELEGRAM_TOKEN = "123456789:" + "A" * 35
PRIVATE_KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f"TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN}", "telegram bot token"),
        (PRIVATE_KEY_HEADER, "private key"),
        ("GOOSE_SERVER__SECRET_KEY='" + "x" * 24 + "'", "assigned secret"),
        ("api_token: " + "q" * 16, "assigned secret"),
        ("key = 'sk-ant-" + "a" * 40 + "'", "anthropic/openai key"),
        ("token = 'ghp_" + "b" * 36 + "'", "github token"),
        (f"curl https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", "telegram bot token"),
        ("bot " + "123456789:" + "A" * 34 + "-", "telegram bot token"),
        ("-H 'Authorization: Bearer " + "c" * 40 + "'", "bearer token"),
    ],
)
def test_detects_secrets(line, kind):
    findings = check_secrets.scan_text("f.txt", line)
    assert [(f.line, f.kind) for f in findings] == [(1, kind)]


@pytest.mark.parametrize(
    "line",
    [
        "AGENT_WORK_SECRET=",
        "GOOSE_SERVER__SECRET_KEY='YOUR_SECRET'",
        "api_token_env: ACPGW_API_TOKEN",
        "secret_env: AGENT_WORK_SECRET",
        "token = os.environ['TOKEN']",
        "password: ${DB_PASSWORD_FROM_ENV}",
        "secret = <your-secret-goes-here>",
        "WORK_GOOSE_SECRET=change-me-please-now",
        "TELEGRAM_BOT_TOKEN=" + TELEGRAM_TOKEN + "  # secrets-check: ignore",
        "1234567890123:" + "A" * 35,  # longer number: not a bot id
        "123456789:" + "A" * 36,  # longer secret: not a bot token
        "Authorization: Bearer ***",
        'f"Bearer {token}"',
    ],
)
def test_ignores_placeholders_and_references(line):
    assert check_secrets.scan_text("f.txt", line) == []


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "deploy/.env",
        "config.yaml",
        "certs/server.pem",
        "id.key",
        "prod.env",
        "deploy/staging.env",
        "id_rsa",
        "home/.ssh/id_ed25519",
        "id_ecdsa_sk",
    ],
)
def test_forbidden_file_names(path):
    assert check_secrets.scan_name(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "config.example.yaml",
        "src/keys.py",
        "id_rsa.pub",
        "id_ed25519.pub",
        "env.py",
    ],
)
def test_allowed_file_names(path):
    assert check_secrets.scan_name(path) is None


def test_repository_files_are_clean():
    """The committed tree itself must pass the check (catches false positives early)."""
    from pathlib import Path

    root = Path(__file__).parents[2]
    # Include new source files but respect ignored private deployment/runtime
    # files, just as Git does. A staged secret still remains in this listing.
    listing = (
        subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split("\0")
    )
    files = [root / name for name in listing if name and (root / name).is_file()]
    findings = check_secrets.scan(
        (str(p.relative_to(root)), check_secrets._decode(p.read_bytes())) for p in files
    )
    assert findings == []


def test_staged_mode_blocks_env_file(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / "ok.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "-f", ".env", "ok.py"], cwd=tmp_path, check=True)
    assert check_secrets.main(["--staged"]) == 1


def test_staged_mode_passes_clean_tree(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "ok.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "ok.py"], cwd=tmp_path, check=True)
    assert check_secrets.main(["--staged"]) == 0
