"""Signing into iCloud, and the two very different ways it fails.

The bug behind most of this: iCloud answers the first, unauthenticated
PROPFIND with 401 and an `alt-svc: h3=":443"` header. The HTTP library
believes it, so the retry - the one carrying the password - goes out over
QUIC, which is UDP. On a path whose MTU is smaller than the datagram it wants
to send, that fails outright:

    ConnectionError: ('Connection aborted.', OSError(90, 'Message too long'))

Measured against caldav.icloud.com at roughly one attempt in four. What makes
it worth guarding is the shape, not the rate: the request that fails is
always the authenticated one, so it presents as the password being wrong -
and the obvious response, generating another app-specific password, cannot
possibly help.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from prep import icloud, scheduler


def _cfg(user="me@icloud.com", password="abcd-efgh-ijkl-mnop"):
    return SimpleNamespace(calendar=SimpleNamespace(
        caldav_url="https://caldav.icloud.com/",
        username=user, password=password, prep_calendar="Prep",
        ignore_calendars=[]))


# ------------------------------------------------- telling the two apart

class _FakeAuthError(Exception):
    pass


@pytest.fixture
def fake_caldav(monkeypatch):
    """A caldav whose failure mode the test chooses."""
    import sys
    import types

    calls = {"clients": 0}

    class FakeSession:
        def __init__(self, **kw):
            self.kw = kw
            self.closed = False
            self._multiplexed = kw.get("multiplexed", False)

        def close(self):
            self.closed = True

    class FakeClient:
        behaviour = "ok"

        def __init__(self, **kw):
            calls["clients"] += 1
            self.kw = kw
            self.session = FakeSession()

        def principal(self):
            if FakeClient.behaviour == "auth":
                raise _FakeAuthError("Unauthorized")
            if FakeClient.behaviour == "transport":
                raise ConnectionError(
                    "('Connection aborted.', OSError(90, 'Message too long'))")
            return "the principal"

    module = types.ModuleType("caldav")
    module.DAVClient = FakeClient
    error_module = types.ModuleType("caldav.lib.error")
    error_module.AuthorizationError = _FakeAuthError
    lib = types.ModuleType("caldav.lib")
    lib.error = error_module
    module.lib = lib

    monkeypatch.setitem(sys.modules, "caldav", module)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", error_module)
    monkeypatch.setattr(scheduler, "sleep", lambda seconds: None)
    return SimpleNamespace(client=FakeClient, calls=calls)


def test_a_rejected_password_is_reported_as_a_rejected_password(fake_caldav):
    fake_caldav.client.behaviour = "auth"
    with pytest.raises(scheduler.CalendarAuthFailed, match="me@icloud.com"):
        scheduler.connect(_cfg())


def test_a_rejected_password_is_not_retried(fake_caldav):
    """Credentials do not become correct on the second attempt, and three
    failed sign-ins in a row is how an account gets locked."""
    fake_caldav.client.behaviour = "auth"
    with pytest.raises(scheduler.CalendarAuthFailed):
        scheduler.connect(_cfg(), attempts=3)
    assert fake_caldav.calls["clients"] == 1


def test_a_transport_failure_is_never_called_an_auth_failure(fake_caldav):
    """The whole point. Reported as a bad password, this sends you to
    regenerate a credential that was working all along."""
    fake_caldav.client.behaviour = "transport"
    with pytest.raises(scheduler.CalendarUnreachable) as caught:
        scheduler.connect(_cfg(), attempts=2)
    assert "Message too long" in str(caught.value)
    assert "password" not in str(caught.value).lower()


def test_a_transport_failure_is_retried(fake_caldav):
    """It is intermittent - about one attempt in four - so a retry usually
    turns a failed unattended run into a successful one."""
    fake_caldav.client.behaviour = "transport"
    with pytest.raises(scheduler.CalendarUnreachable):
        scheduler.connect(_cfg(), attempts=3)
    assert fake_caldav.calls["clients"] == 3


def test_a_working_sign_in_returns_the_principal(fake_caldav):
    fake_caldav.client.behaviour = "ok"
    _client, principal = scheduler.connect(_cfg())
    assert principal == "the principal"


# ------------------------------------------------------- the QUIC fix

def test_http3_is_turned_off_on_the_session(monkeypatch):
    """HTTP/1.1 and HTTP/2 are TCP, which fragments properly. HTTP/3 is UDP,
    and the datagram carrying the credentials is the one that is too big."""
    niquests = pytest.importorskip("niquests")

    class Client:
        session = niquests.Session(multiplexed=False)

    client = Client()
    original = client.session
    assert scheduler._no_quic(client) is True
    assert client.session is not original
    assert client.session._disable_http3 is True


def test_the_multiplexing_choice_is_preserved(monkeypatch):
    """caldav decides that from its own feature set. Overriding it here would
    be a second change nobody asked for, in a fix about one thing."""
    niquests = pytest.importorskip("niquests")

    class Client:
        session = niquests.Session(multiplexed=True)

    client = Client()
    scheduler._no_quic(client)
    assert client.session.multiplexed is True


def test_a_client_without_a_session_is_left_alone():
    class Bare:
        pass

    assert scheduler._no_quic(Bare()) is False


def test_a_session_from_another_library_is_left_alone():
    """Older caldav uses requests, which has no QUIC to disable. Replacing
    that session would break the client to fix a bug it cannot have."""
    class Client:
        session = object()

    assert scheduler._no_quic(Client()) is False


# ----------------------------------------- what makes a correct password fail

def test_whitespace_around_a_secret_is_stripped(monkeypatch):
    """A trailing newline fails exactly like a wrong password, and you cannot
    see it in an export. `cred:` already stripped; `env:` did not."""
    from prep.config import _env

    monkeypatch.setenv("PREP_TEST_SECRET", "  abcd-efgh-ijkl-mnop\n")
    assert _env("env:PREP_TEST_SECRET") == "abcd-efgh-ijkl-mnop"


def test_quotes_left_around_a_password_are_caught():
    """The shell strips these; a naive .env reader does not, and the quotes
    then travel as part of the password."""
    problems = icloud.check_secret_shape("password", '"hunter2"',
                                         "env:ICLOUD_APP_PASSWORD")
    assert any("quotes" in text for _status, text in problems)


def test_an_empty_secret_says_it_was_never_exported():
    problems = icloud.check_secret_shape("password", "",
                                         "env:ICLOUD_APP_PASSWORD")
    assert any("exported" in text for _status, text in problems)


def test_an_apple_app_password_is_recognised():
    statuses = [s for s, _ in icloud.check_app_password("abcd-efgh-ijkl-mnop")]
    assert statuses == [icloud.OK]


def test_an_ordinary_apple_password_is_flagged_before_anything_is_sent():
    """It cannot work over CalDAV however correct it is, and saying so early
    saves a round trip and a wrong conclusion."""
    warnings = icloud.check_app_password("MyRealPassword1!")
    assert warnings[0][0] == icloud.WARN
    assert "app-specific" in warnings[0][1]


def test_a_password_is_never_printed():
    """This command exists to be run when something is wrong, which is when
    output gets pasted into a chat window."""
    described = icloud.shape("abcd-efgh-ijkl-mnop")
    assert "abcd" not in described
    assert "19 chars" in described
