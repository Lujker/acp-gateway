import pytest


def test_resume_requires_acknowledgement_before_any_connection(monkeypatch, capsys):
    import spike_hermes

    def never_connect(*args):
        pytest.fail("resume without acknowledgement reached discovery")

    monkeypatch.setattr(spike_hermes, "read_endpoint", never_connect)
    monkeypatch.setattr("sys.argv", ["spike_hermes.py", "--resume-session", "stored-test"])
    with pytest.raises(SystemExit) as error:
        spike_hermes.main()
    assert error.value.code == 2
    assert "requires --allow-auto-continue" in capsys.readouterr().err
