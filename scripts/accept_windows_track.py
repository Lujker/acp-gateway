"""One native Windows, model-free acceptance run against the final preview wheel."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/integration/test_windows_connector_task.py",
    "tests/integration/test_connector_graceful_stop.py",
    "tests/integration/test_hermes_acp.py",
    "tests/integration/test_hermes_probe.py",
    "tests/integration/test_hermes_relay.py",
    "tests/integration/test_hermes_rotation.py",
    "tests/integration/test_hermes_setup_live.py",
    "tests/integration/test_hermes_binding_recovery.py",
    "tests/integration/test_hermes_live_sessions.py",
    "tests/integration/test_telegram.py",
    "tests/integration/test_maintenance_admission.py",
    "tests/integration/test_goose_probe.py",
    "tests/unit/test_windows_service.py",
    "tests/unit/test_windows_service_update.py",
    "tests/unit/test_windows_acl.py",
    "tests/unit/test_windows_setup_paths.py",
    "tests/unit/test_windows_wizard_task.py",
    "tests/unit/test_maintenance_cli.py",
    "tests/unit/test_owner_api.py",
    "tests/unit/test_model_control.py",
    "tests/unit/test_model_state.py",
    "tests/unit/test_model_cli.py",
    "tests/unit/test_connector_policy.py",
    "tests/unit/test_connector_lifecycle.py",
    "tests/unit/test_connector_maintenance.py",
    "tests/unit/test_connector_commands.py",
    "tests/unit/test_saved_connector_launch.py",
    "tests/unit/test_checkout_scripts.py",
    "tests/unit/test_session_bindings.py",
    "tests/unit/test_storage.py",
    "tests/unit/test_hermes_discovery.py",
    "tests/unit/test_hermes_dedicated.py",
    "tests/unit/test_hermes_setup.py",
    "tests/unit/test_hermes_config.py",
    "tests/unit/test_hermes_spike_cli.py",
    "tests/unit/test_doctor.py",
]


def verified_preview(directory):
    manifest = json.loads((directory / "PREVIEW.json").read_text(encoding="utf-8"))
    if manifest.get("preview") is not True:
        raise ValueError("a development preview manifest is required")
    hashes = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in hashes:
            raise ValueError("invalid preview checksum list")
        hashes[match[2]] = match[1]
    wheels = [name for name in hashes if name.endswith(".whl")]
    if len(wheels) != 1 or "requirements.dev.lock.txt" not in hashes:
        raise ValueError("preview needs one wheel and locked dev requirements")
    for name, expected in hashes.items():
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("preview checksum mismatch")
    evidence = manifest.get("wheel_tests")
    if (
        not isinstance(evidence, dict)
        or evidence.get("installed_wheel_sha256") != hashes[wheels[0]]
    ):
        raise ValueError("wheel does not match tested preview evidence")
    return directory / wheels[0], hashes, manifest


def junit_summary(path):
    cases = list(ET.parse(path).getroot().iter("testcase"))  # noqa: S314 — local pytest output
    native = [case for case in cases if "test_windows_connector_task" in case.get("classname", "")]

    def failed(case):
        return any(case.find(tag) is not None for tag in ("failure", "error"))

    def skipped(case):
        return case.find("skipped") is not None

    return {
        "tests": len(cases),
        "failed": sum(map(failed, cases)),
        "skipped": sum(map(skipped, cases)),
        "native_tests": len(native),
        "native_complete": len(native) >= 23 and not any(failed(c) or skipped(c) for c in native),
    }


def run(args):
    if sys.platform != "win32":
        raise ValueError("run this acceptance script with native Windows Python")
    preview = args.preview_dir.resolve()
    wheel, hashes, manifest = verified_preview(preview)
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("native Windows uv is required")
    if args.output_dir:
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix="acpgw-windows-acceptance-"))
    report_path = output / "report.json"
    report = {
        "status": "running",
        "preview": manifest,
        "artifact_hashes": hashes,
        "stages": [],
        "manual_checks_remaining": [
            "reboot/logon",
            "physical sleep/wake/network change",
            "real model/approvals",
            "VPS/two-PC/Telegram",
            "live Goose/matrix",
        ],
    }

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def command(name, argv, *, timeout=180, env=None, allow_failure=False):
        print(f"Running: {name}", flush=True)
        result = subprocess.run(  # noqa: S603 — explicit argv, no shell or credentials
            list(map(str, argv)),
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        log = output / (name + ".log")
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        report["stages"].append({"name": name, "exit_code": result.returncode, "log": log.name})
        save()
        if result.returncode and not allow_failure:
            raise ValueError(f"{name} failed; inspect {log}")
        return result.stdout

    def observer(name, python):
        argv = [
            python,
            "-m",
            "acp_gateway",
            "agents",
            "probe",
            "hermes",
            "--role",
            "desktop-serve",
            "--json",
        ]
        if args.hermes_lock_dir:
            argv += ["--lock-dir", args.hermes_lock_dir]
        rows = json.loads(command(name, argv, allow_failure=True))["agents"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("invalid existing Hermes Desktop observer report")
        return rows[0]

    try:
        save()
        environment = output / "wheel-env"
        command("create-env", [uv, "venv", environment, "--python", "3.12", "--managed-python"])
        python = environment / "Scripts" / "python.exe"
        command(
            "install-wheel",
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "--no-deps",
                "-r",
                preview / "requirements.dev.lock.txt",
                wheel,
            ],
            timeout=300,
        )
        info = command(
            "verify-import",
            [
                python,
                "-I",
                "-c",
                "import acp_gateway,sys,platform,json; from pathlib import Path; "
                "assert sys.platform == 'win32'; "
                "assert Path(acp_gateway.__file__).is_relative_to(Path(sys.prefix)); "
                "print(json.dumps({'python':sys.version,'platform':platform.platform(),"
                "'package':acp_gateway.__file__}))",
            ],
        )
        report["environment"] = json.loads(info)
        before = observer("hermes-before", python)
        report["hermes"] = {"before": before}
        save()
        if before.get("status") == "ready":
            selector = """
import contextlib,io,json,sys
from acp_gateway.cli.hermes_setup import select
class Defaults:
    def ask(self,label,default='',validate=None):
        return validate(default) if validate else default
    def choice(self,label,choices,default):
        return default
    def yes(self,*args):
        return False
old={'role':'desktop-serve'}
if len(sys.argv)>1:
    old['lock_dir']=sys.argv[1]
with contextlib.redirect_stdout(io.StringIO()):
    recipe=select(Defaults(),old)
assert set(recipe)=={'lock_dir','role','profile'} and recipe['role']=='desktop-serve'
print(json.dumps({'role':recipe['role'],'profile':recipe['profile'],'config_saved':False}))
"""
            argv = [python, "-I", "-c", selector]
            if args.hermes_lock_dir:
                argv.append(args.hermes_lock_dir)
            report["selector"] = json.loads(command("live-setup-selector", argv))
        env = os.environ.copy()
        env["ACPGW_TEST_WINDOWS_TASK"] = "1"
        junit = output / "tests.xml"
        command(
            "native-tests",
            [python, "-m", "pytest", "-q", "--tb=short", f"--junitxml={junit}", *TESTS],
            timeout=1200,
            env=env,
        )
        report["tests"] = junit_summary(junit)
        if report["tests"]["failed"] or not report["tests"]["native_complete"]:
            raise ValueError("native Scheduler cases did not all run successfully")
        after = observer("hermes-after", python)
        if before.get("status") != "ready" or after.get("status") != "ready":
            report["hermes"]["after"] = after
            raise ValueError(
                "existing Hermes Desktop observer did not return ready before and after"
            )
        report["hermes"] = {
            "before": before,
            "after": after,
            "same_endpoint": before["endpoint"] == after["endpoint"],
        }
        if not report["hermes"]["same_endpoint"]:
            raise ValueError("Hermes endpoint changed during acceptance; inspect before/after")
        report["status"] = "passed-automated"
        save()
        print(f"Automated acceptance passed. Report: {report_path}", flush=True)
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        save()
        print(f"Acceptance failed. Report: {report_path}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-dir", type=Path, default=ROOT / "dist/p4.8-preview")
    parser.add_argument(
        "--output-dir", type=Path, help="new directory; existing reports are preserved"
    )
    parser.add_argument(
        "--hermes-lock-dir", type=Path, help="optional native Desktop rendezvous directory"
    )
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        print(f"Cannot start acceptance: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
