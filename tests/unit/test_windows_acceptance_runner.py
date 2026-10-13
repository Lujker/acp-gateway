import hashlib
import json
from types import SimpleNamespace

import pytest

import accept_windows_track as runner


def test_preview_verification_refuses_changed_wheel_and_path_traversal(tmp_path):
    wheel = tmp_path / "preview.whl"
    wheel.write_bytes(b"preview fixture")
    req = tmp_path / "requirements.dev.lock.txt"
    req.write_text("# fixture\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(
        f"{digest}  {wheel.name}\n{hashlib.sha256(req.read_bytes()).hexdigest()}  {req.name}\n"
    )
    (tmp_path / "PREVIEW.json").write_text(
        json.dumps({"preview": True, "wheel_tests": {"installed_wheel_sha256": digest}})
    )
    assert runner.verified_preview(tmp_path)[0] == wheel
    wheel.write_bytes(b"changed fixture")
    with pytest.raises(ValueError, match="checksum mismatch"):
        runner.verified_preview(tmp_path)
    checksums.write_text(f"{digest}  ../outside.whl\n")
    with pytest.raises(ValueError, match="invalid preview checksum"):
        runner.verified_preview(tmp_path)


@pytest.mark.parametrize("skipped", [True, False])
def test_native_acceptance_cannot_pass_with_skipped_scheduler_cases(tmp_path, skipped):
    xml = tmp_path / "tests.xml"
    cases = [
        f'<testcase classname="test_windows_connector_task" name="case{i}">'
        + ("<skipped/>" if skipped and i == 0 else "")
        + "</testcase>"
        for i in range(23)
    ]
    xml.write_text("<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>")
    result = runner.junit_summary(xml)
    assert result["native_tests"] == 23
    assert result["native_complete"] is not skipped


@pytest.mark.parametrize("count", [0, 13, 19, 22])
def test_native_acceptance_refuses_incomplete_scheduler_profile(tmp_path, count):
    xml = tmp_path / "tests.xml"
    cases = '<testcase classname="test_windows_connector_task" name="case"/>' * count
    xml.write_text("<testsuites><testsuite>" + cases + "</testsuite></testsuites>")
    assert not runner.junit_summary(xml)["native_complete"]


def test_non_native_runner_refuses_before_creating_any_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(ValueError, match="native Windows"):
        runner.run(SimpleNamespace(output_dir=tmp_path / "report"))
    assert not (tmp_path / "report").exists()
