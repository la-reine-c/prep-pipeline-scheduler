"""Resolving secrets from systemd credentials rather than a plaintext file.

The machine genuinely must hold some secrets unattended - the iCloud app
password, the timetable feed key. There is no human-present alternative for
those, so the question is only how well they are held. systemd decrypts them
into a private tmpfs for one service, which beats a .env that every process
of yours can read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from prep import config as config_module


def test_a_credential_is_read_from_the_systemd_directory(tmp_path, monkeypatch):
    (tmp_path / "ICLOUD_PASSWORD").write_text("abcd-efgh-ijkl-mnop")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert config_module._env("cred:ICLOUD_PASSWORD") == "abcd-efgh-ijkl-mnop"


def test_trailing_whitespace_is_stripped(tmp_path, monkeypatch):
    """A password with a stray newline fails as if it were the wrong password."""
    (tmp_path / "TOKEN").write_text("s3cret\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert config_module._env("cred:TOKEN") == "s3cret"


def test_running_outside_systemd_says_what_is_wrong(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(RuntimeError, match="not started by systemd"):
        config_module._env("cred:ICLOUD_PASSWORD")


def test_a_missing_credential_names_the_unit_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(RuntimeError, match="LoadCredentialEncrypted"):
        config_module._env("cred:NOT_PASSED")


def test_an_optional_credential_is_allowed_to_be_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert config_module._env("cred:OPTIONAL", required=False) == ""
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert config_module._env("cred:OPTIONAL", required=False) == ""


def test_env_and_literal_values_still_work(monkeypatch):
    """The new prefix must not break either of the existing two forms."""
    monkeypatch.setenv("SOME_VAR", "from-env")
    assert config_module._env("env:SOME_VAR") == "from-env"
    assert config_module._env("just a value") == "just a value"


def test_a_whole_config_can_be_loaded_from_credentials(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "ICLOUD_USER").write_text("someone@icloud.com")
    (creds / "ICLOUD_PASSWORD").write_text("abcd-efgh-ijkl-mnop")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/profile}}
calendar:
  caldav_url: https://caldav.icloud.com/
  username: cred:ICLOUD_USER
  password: cred:ICLOUD_PASSWORD
  prep_calendar: Prep
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    assert cfg.calendar.username == "someone@icloud.com"
    assert cfg.calendar.password == "abcd-efgh-ijkl-mnop"


# ------------------------------------------- .env when run by hand, not systemd

_DOTENV_CONFIG = """
db_path: {d}/p.db
dump_dir: {d}/dumps
export_path: {d}/p.txt
teams: {{assignments_url: x, url_hints: [a], profile_dir: {d}/profile}}
feeds: {{socs: env:PREP_TEST_FEED}}
calendar:
  caldav_url: https://caldav.icloud.com/
  username: env:PREP_TEST_USER
  password: env:PREP_TEST_PASSWORD
  prep_calendar: Prep
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
"""


def _forget(monkeypatch, *names):
    # delenv records each as absent, so whatever load() exports is undone.
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_a_dotenv_beside_the_config_is_read(tmp_path, monkeypatch):
    """Only systemd read .env, so `make phase-c` said ICLOUD_USER was not set
    with a perfectly good .env sitting next to config.yaml."""
    _forget(monkeypatch, "PREP_TEST_USER", "PREP_TEST_PASSWORD", "PREP_TEST_FEED")
    (tmp_path / "config.yaml").write_text(_DOTENV_CONFIG.format(d=tmp_path))
    (tmp_path / ".env").write_text(
        "# secrets\n"
        "\n"
        "PREP_TEST_USER=someone@icloud.com\n"
        'export PREP_TEST_PASSWORD="abcd-efgh-ijkl-mnop"\n'
        "PREP_TEST_FEED='https://feed.example/x?key=a=b'\n")

    cfg = config_module.load(tmp_path / "config.yaml")
    assert cfg.calendar.username == "someone@icloud.com"
    assert cfg.calendar.password == "abcd-efgh-ijkl-mnop"   # quotes gone
    assert cfg.feeds["socs"] == "https://feed.example/x?key=a=b"


def test_the_environment_wins_over_dotenv(tmp_path, monkeypatch):
    """systemd, a credential or an explicit export is never second-guessed."""
    _forget(monkeypatch, "PREP_TEST_PASSWORD", "PREP_TEST_FEED")
    monkeypatch.setenv("PREP_TEST_USER", "exported@icloud.com")
    (tmp_path / "config.yaml").write_text(_DOTENV_CONFIG.format(d=tmp_path))
    (tmp_path / ".env").write_text(
        "PREP_TEST_USER=stale@icloud.com\n"
        "PREP_TEST_PASSWORD=x\nPREP_TEST_FEED=y\n")

    cfg = config_module.load(tmp_path / "config.yaml")
    assert cfg.calendar.username == "exported@icloud.com"


def test_a_missing_secret_says_where_to_put_it(tmp_path, monkeypatch):
    _forget(monkeypatch, "PREP_TEST_USER", "PREP_TEST_PASSWORD", "PREP_TEST_FEED")
    (tmp_path / "config.yaml").write_text(_DOTENV_CONFIG.format(d=tmp_path))

    with pytest.raises(RuntimeError, match=r"\.env beside your config\.yaml"):
        config_module.load(tmp_path / "config.yaml")


@pytest.mark.parametrize("setting", ["username", "password"])
def test_a_blank_value_written_into_the_config_is_refused(tmp_path, setting):
    """A blank iCloud password reached CalDAV and came back as "the server
    never answered", which sends you to check the network, not the config."""
    other = "password" if setting == "username" else "username"
    (tmp_path / "config.yaml").write_text(f"""
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/profile}}
calendar:
  caldav_url: https://caldav.icloud.com/
  {setting}: ""
  {other}: written-in
  prep_calendar: Prep
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    with pytest.raises(RuntimeError, match=f"calendar.{setting} is empty"):
        config_module.load(tmp_path / "config.yaml")


def test_values_written_straight_into_the_config_are_used(tmp_path):
    (tmp_path / "config.yaml").write_text(f"""
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/profile}}
feeds: {{socs: "https://feed.example/x?key=1"}}
calendar:
  caldav_url: https://caldav.icloud.com/
  username: someone@icloud.com
  password: " abcd-efgh-ijkl-mnop "
  prep_calendar: Prep
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    assert cfg.calendar.username == "someone@icloud.com"
    assert cfg.calendar.password == "abcd-efgh-ijkl-mnop"
    assert cfg.feeds["socs"] == "https://feed.example/x?key=1"
