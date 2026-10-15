#!/usr/bin/env python3
"""Check the community dictionaries' download, without NVDA or GitHub.

The add-on can fetch the community's IBMTTSDictionaries itself and keep them up
to date. The part of that worth holding to account is not the fetching but when
the result is allowed to reach the engine. A dictionary file the engine has read
must not change while NVDA is running: the engine notices within two seconds,
reads every dictionary again, and freeing the community's seventy thousand root
words on the way is slow enough to silence the reader (upstream #49). So a
download is only ever written beside the set in use, and is put in its place
the next time the driver starts, before anything has been read.

What these checks stand in for is GitHub, NVDA's settings machinery and the
thread the download runs on, which here runs in line. One check does use the
network, a server on this machine, because the size limit is enforced by the
code that talks to the network and nothing else can show it holds.

usage: community.py
"""

import http.server
import io
import os
import shutil
import sys
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(os.path.dirname(HERE), "addon")

sys.path.insert(0, HERE)

import sequence  # noqa: E402  the NVDA stand-ins live there

FAILED = []


def check(what, got, want):
    if got == want:
        print("ok   %s" % what)
    else:
        print("FAIL %s\n     got  %r\n     want %r" % (what, got, want))
        FAILED.append(what)


# ---- GitHub, as the download sees it ---------------------------------------

FIRST = "a" * 40
SECOND = "b" * 40

#: The three files the community publishes for US English, as they really
#: are: Windows line ends, a tab between a word and what to say instead.
GOOD = {
    "ENUmain.dic": b"tabindex\tTab Index\r\nomg\toe em jee\r\n",
    "ENURoot.dic": b"neuro\t`[.1nUr.0o]\r\nbillet\t`[.1bI.0lIt]\r\n",
    "ENUabbr.dic": b"Dr.\tdoctor\r\n",
}


def archive(files=GOOD, revision=FIRST, extra=()):
    """A zip laid out as GitHub lays out a repository's archive: one folder
    named for the repository and the commit, everything inside it."""
    top = "IBMTTSDictionaries-%s/" % revision
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(top, b"")
        z.writestr(top + "README.md", b"# IBMTTSDictionaries\n")
        z.writestr(top + "LICENSE.md", b"CC0\n")
        for name, data in files.items():
            z.writestr(top + name, data)
        for name, data in extra:
            z.writestr(name, data)
    return out.getvalue()


class GitHub:
    """An answer for each address, and a note of every address asked.

    The size limit is the real reader's to enforce, so it is enforced here as
    well: a fake that handed back anything it was given would let a check pass
    that the real one would fail.
    """

    def __init__(self, head=FIRST, body=None, failure=None):
        self.head = head
        self.body = body if body is not None else archive(revision=head)
        self.failure = failure
        self.asked = []
        #: Run as the archive is asked for, for the checks that want
        #: something to happen while a download is in flight.
        self.during = None

    def __call__(self, url, accept, limit):
        self.asked.append(url)
        if self.failure is not None:
            raise self.failure
        if url == community.HEAD_URL:
            answer = self.head.encode("ascii") if isinstance(self.head, str) else self.head
        elif url == community.ARCHIVE_URL % self.head:
            if self.during is not None:
                self.during()
            answer = self.body
        else:
            raise OSError("HTTP Error 404: %s" % url)
        if len(answer) > limit:
            raise ValueError("%s is more than %d bytes" % (url, limit))
        return answer

    def fetchedArchive(self):
        return any("/archive/" in url for url in self.asked)


# ---- the stand-ins, made fresh for every check -----------------------------

SAID = []
NOW = [1_800_000_000.0]
SAVED = {}
#: Every time the reader was asked whether to restart.
ASKED = []
#: Offers waiting on NVDA's timer, run when a check says time has passed.
LATER = []
#: Whether a settings dialog, or a message box, is open.
OPEN = [False]
MODAL = [False]

DOWNLOADED = "Community dictionaries downloaded."

ROOT = os.path.join(sequence.CONFIG, "openevv-community")
CURRENT = os.path.join(ROOT, "current")
PENDING = os.path.join(ROOT, "pending")
PERSONAL = os.path.join(sequence.CONFIG, "eciDictionaries", "personal")
OWN = os.path.join(sequence.CONFIG, "openevv")


#: NVDA's configuration as the driver meets it (config/__init__.py): the
#: normal configuration first in `profiles`, any profile in force after it.
#: SAVED is the driver's own section of the normal configuration.
BASE = {}
CONF = sequence._Conf({"audio": {"outputDevice": "default"}})
CONF.profiles = [BASE]

_STUBS = sequence._install_stubs


def _install_stubs():
    """sequence's stand-ins, with the configuration these checks keep."""
    _STUBS()
    sys.modules["config"].conf = CONF


sequence._install_stubs = _install_stubs


def _section(profile, make=False):
    """The driver's own section of one profile, where NVDA files it."""
    if make:
        return profile.setdefault("speech", {}).setdefault("openevv", {})
    return profile.get("speech", {}).get("openevv", {})


def _inForce(key):
    """What NVDA's configuration answers for a key: the last profile in force
    that holds it, and under them all the normal configuration."""
    for profile in reversed(CONF.profiles):
        section = _section(profile)
        if key in section:
            return section[key]
    return None


def _initSettings(self):
    """NVDA's initSettings (synthDriverHandler.py:360-384): a first load
    writes the defaults out, and any other hands back what was saved."""
    if not any(_section(profile) for profile in CONF.profiles):
        self.saveSettings()
    else:
        self.loadSettings()


def _loadSettings(self, onlyChanged=False):
    """NVDA's SynthDriver.loadSettings (synthDriverHandler.py:386-408): every
    setting the configuration in force holds is handed to its setter, or
    only those that differ from what the driver has."""
    for setting in self.supportedSettings:
        value = _inForce(setting.id)
        if value is None:
            continue
        if onlyChanged and getattr(self, setting.id) == value:
            continue
        setattr(self, setting.id, value)


def _saveSettings(self):
    """NVDA's saveSettings: every setting written into the configuration
    being edited, which is the profile in force when there is one."""
    section = _section(CONF.profiles[-1], make=True)
    for setting in self.supportedSettings:
        try:
            value = getattr(self, setting.id)
        except AttributeError:
            # The stand-in engine has no sample rate to report; nothing
            # these checks follow depends on one being saved.
            continue
        section[setting.id] = value


def _enter(d, values):
    """A profile coming into force: NVDA hands the synthesiser whatever in it
    differs (synthDriverHandler.handlePostConfigProfileSwitch)."""
    profile = {"speech": {"openevv": dict(values)}}
    CONF.profiles.append(profile)
    d.loadSettings(onlyChanged=True)
    return profile


def _leave(d):
    """The profile going, handed over the same way."""
    CONF.profiles.pop()
    d.loadSettings(onlyChanged=True)


def fresh(github=None):
    """A configuration with nothing of the community's in it, and GitHub,
    the clock, the thread and the voice all stood in for."""
    sequence._install_stubs()
    if ADDON not in sys.path:
        sys.path.insert(0, ADDON)
    global community
    import synthDrivers._openevv_community as community

    shutil.rmtree(ROOT, ignore_errors=True)
    del SAID[:]
    SAVED.clear()
    BASE.clear()
    BASE["speech"] = {"openevv": SAVED}
    CONF.profiles[:] = [BASE]
    NOW[0] = 1_800_000_000.0
    github = github if github is not None else GitHub()
    community._get = github
    community._spawn = lambda target: target()
    community._announce = SAID.append
    community._now = lambda: NOW[0]
    del ASKED[:]
    del LATER[:]
    OPEN[0] = False
    MODAL[0] = False
    community._onGui = lambda fn: fn()
    community._later = lambda milliseconds, fn: LATER.append(fn)
    community._settingsOpen = lambda: OPEN[0]
    community._modalOpen = lambda: MODAL[0]
    community._askRestart = lambda: ASKED.append(True)
    sequence._SynthDriver.initSettings = _initSettings
    sequence._SynthDriver.loadSettings = _loadSettings
    sequence._SynthDriver.saveSettings = _saveSettings
    return github


def tick():
    """NVDA's timer going off: every offer waiting on it runs once."""
    waiting = list(LATER)
    del LATER[:]
    for fn in waiting:
        fn()


def driver():
    d = sequence.driver()
    return d


def folders(d):
    """Every set of folders the driver has told the engine to read."""
    return [args[0] for name, *args in d._engine.calls
            if name == "setDictionaryFolders"]


def files(folder):
    try:
        return sorted(os.listdir(folder))
    except OSError:
        return None


def read(path):
    with open(path, "rb") as f:
        return f.read()


def heard(words):
    return [s for s in SAID if words in s]


# ---- what the download will take -------------------------------------------


def validation_checks():
    fresh()

    taken = community.validate(archive(extra=[
        ("IBMTTSDictionaries-%s/nested/ENGmain.dic" % FIRST, b"a\tb\r\n"),
    ]))
    check("an archive gives up its dictionary files under lower-case names,"
          " and nothing else",
          sorted(taken), ["enuabbr.dic", "enumain.dic", "enuroot.dic"])
    check("byte for byte as published",
          taken["enuroot.dic"], GOOD["ENURoot.dic"])

    taken = community.validate(archive(files=dict(GOOD, **{
        "XYZmain.dic": b"a\tb\r\n",
        "ENUnotes.dic": b"a\tb\r\n",
        "DEUmain.dic": b"Hallo\thallo\n",
    })))
    check("a language the engine has is taken, a name it does not know is not",
          sorted(taken), ["deumain.dic", "enuabbr.dic", "enumain.dic",
                          "enuroot.dic"])

    def refused(what, body):
        try:
            community.validate(body)
        except ValueError:
            check(what, True, True)
        else:
            check(what, "taken", "refused")

    refused("a page that is not an archive at all is refused",
            b"<!DOCTYPE html><html><body>Rate limit exceeded</body></html>")
    refused("an archive with no dictionary in it is refused",
            archive(files={}))
    refused("an archive that is not one folder is refused",
            archive(extra=[("elsewhere/DEUmain.dic", b"a\tb\r\n")]))
    refused("a line with no tab in it is refused",
            archive(files=dict(GOOD, **{"ENUmain.dic": b"tabindex Tab Index\r\n"})))
    refused("a word with nothing to say instead is refused",
            archive(files=dict(GOOD, **{"ENUmain.dic": b"tabindex\t\r\n"})))
    refused("a file saved with a byte order mark is refused",
            archive(files=dict(GOOD, **{"ENUmain.dic": b"\xef\xbb\xbfomg\toe em jee\r\n"})))
    refused("a file with a nul in it is refused",
            archive(files=dict(GOOD, **{"ENUmain.dic": b"omg\toe\x00em\r\n"})))
    refused("two files that are one name on Windows are refused",
            archive(files=dict(GOOD, **{"enumain.dic": b"a\tb\r\n"})))

    limit = community.MAX_FILE
    try:
        community.MAX_FILE = 64
        refused("a file past the limit is refused",
                archive(files=dict(GOOD, **{"ENURoot.dic": b"a\tb\r\n" * 20})))
    finally:
        community.MAX_FILE = limit

    limits = community.MAX_FILE, community.MAX_TOTAL
    try:
        community.MAX_FILE = 64
        community.MAX_TOTAL = 100
        refused("files under the limit one at a time but over it together are"
                " refused", archive(files={"ENUmain.dic": b"a\tb\r\n" * 11,
                                           "ENURoot.dic": b"a\tb\r\n" * 11}))
    finally:
        community.MAX_FILE, community.MAX_TOTAL = limits

    # A file is checked a line at a time. Split into a list, a million short
    # lines are a million objects held at once, ten times the file's size.
    import tracemalloc

    many = archive(files={"ENURoot.dic": b"a\tb\n" * 1_000_000})
    tracemalloc.start()
    try:
        community.validate(many)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    check("a file of a million short lines is checked without holding a"
          " million lines at once", peak < 24 * 1024 * 1024, True)

    blank = community.validate(archive(files=dict(GOOD, **{
        "ENUmain.dic": b"omg\toe em jee\r\n\r\ntabindex\tTab Index",
    })))
    check("a blank line and a last line with no end are both left as they are",
          blank["enumain.dic"], b"omg\toe em jee\r\n\r\ntabindex\tTab Index")


# ---- putting a download in place -------------------------------------------


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def prepare_checks():
    fresh()
    check("with nothing downloaded there is nothing to read",
          community.prepare(ROOT), None)

    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    check("a finished download waits beside the set in use",
          (files(PENDING), files(CURRENT)), (["enumain.dic", "source.ini"], None))
    check("and is put in its place at the next start",
          community.prepare(ROOT), CURRENT)
    check("which leaves nothing waiting",
          (files(CURRENT), files(PENDING)), (["enumain.dic", "source.ini"], None))
    check("and says which commit it came from",
          community.revisionOf(CURRENT), FIRST)

    community.stage(ROOT, {"enuroot.dic": b"c\td\r\n"}, SECOND)
    check("a newer one replaces the set whole, so a file the community took"
          " out is gone too",
          (community.prepare(ROOT), files(CURRENT), community.revisionOf(CURRENT)),
          (CURRENT, ["enuroot.dic", "source.ini"], SECOND))

    _write(os.path.join(PENDING, "enumain.dic"), b"half")
    check("a download that never finished is thrown away, and the set in use"
          " kept",
          (community.prepare(ROOT), files(PENDING), community.revisionOf(CURRENT)),
          (CURRENT, None, SECOND))

    _write(os.path.join(ROOT, "pending.partial", "enumain.dic"), b"half")
    _write(os.path.join(ROOT, "current.old", "enumain.dic"), b"stale")
    community.prepare(ROOT)
    check("and so are the leavings of one interrupted at any other point",
          sorted(os.listdir(ROOT)), ["current"])

    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    check("a start whose saved setting is unticked takes the set away, with"
          " anything waiting to replace it",
          (community.prepare(ROOT, False), os.listdir(ROOT)), (None, []))

    # Saved unticked means not read, whether or not the files will go: a
    # virus scanner holding one open is not a reason to read them.
    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    community.prepare(ROOT)
    kept = community._remove

    def held(path):
        if path.endswith("current"):
            raise PermissionError("held open by a virus scanner")
        return kept(path)

    community._remove = held
    try:
        got = community.prepare(ROOT, False)
    finally:
        community._remove = kept
    check("saved unticked, a set that will not go is still not read", got, None)
    check("and the log says why",
          any("take the community dictionaries away" in e
              for e in sequence.LOGGED["error"]), True)
    check("and a start that is saved ticked reads it again",
          community.prepare(ROOT), CURRENT)

    # The swap is two renames. If the second fails the first is undone, so
    # a start never finds itself with no set at all.
    community.stage(ROOT, {"enuroot.dic": b"c\td\r\n"}, SECOND)
    real = os.replace
    calls = []

    def failing(src, dst):
        calls.append((src, dst))
        if src.endswith("pending"):
            raise PermissionError("in use")
        return real(src, dst)

    community.os.replace = failing
    try:
        got = community.prepare(ROOT)
    finally:
        community.os.replace = real
    check("a swap that fails half way puts the old set back",
          (got, community.revisionOf(CURRENT)), (CURRENT, FIRST))
    check("and the log says why",
          any("community" in e for e in sequence.LOGGED["error"]), True)


# ---- the download itself ---------------------------------------------------


def updater_checks():
    github = fresh()
    u = community.Updater(ROOT)
    check("nothing downloaded, nothing in use", u.prepare(), None)

    u.want(True, byReader=True)
    check("asked for, it fetches the newest commit and then that commit's"
          " archive",
          github.asked, [community.HEAD_URL, community.ARCHIVE_URL % FIRST])
    check("which waits for the next start",
          (files(PENDING), files(CURRENT)),
          (["enuabbr.dic", "enumain.dic", "enuroot.dic", "source.ini"], None))
    check("and the reader is told so", len(heard(DOWNLOADED)), 1)
    check("and asked whether to restart", ASKED, [True])

    del github.asked[:]
    del SAID[:]
    u.want(True, byReader=True)
    check("asked again before a restart, it does not fetch the same commit"
          " twice", github.fetchedArchive(), False)
    check("and says it is already there", len(heard("already downloaded")), 1)
    check("and asks again whether to restart, since it was asked again",
          ASKED, [True, True])

    u = community.Updater(ROOT)
    check("at the next start it is the set in use", u.prepare(), CURRENT)
    del github.asked[:]
    del SAID[:]
    u.want(True, byReader=True)
    del ASKED[:]
    check("and asked again, the reader hears it is up to date",
          (github.fetchedArchive(), len(heard("up to date"))), (False, 1))
    check("with nothing to restart for", ASKED, [])

    # Starting with the box ticked is NVDA restoring a setting, not the reader
    # asking for anything, so it is quiet and it is not every time.
    del github.asked[:]
    del SAID[:]
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=False)
    check("a start within a day of the last look does not look again",
          github.asked, [])
    NOW[0] += community.CHECK_EVERY + 1
    github.head = SECOND
    github.body = archive(files={"ENUmain.dic": b"new\tword\r\n"}, revision=SECOND)
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=False)
    check("a day later it looks, and fetches what is new",
          (github.fetchedArchive(), community.revisionOf(PENDING)), (True, SECOND))
    check("without a word to the reader", SAID, [])
    check("or a question: a start never asks to restart", ASKED, [])
    check("and the set in use is left exactly as it was",
          (community.revisionOf(CURRENT), files(CURRENT)),
          (FIRST, ["enuabbr.dic", "enumain.dic", "enuroot.dic", "source.ini"]))


def failure_checks():
    github = fresh(GitHub(failure=OSError("getaddrinfo failed")))
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("with no network nothing is written",
          os.listdir(ROOT) if os.path.isdir(ROOT) else [], [])
    check("and the reader hears that it could not",
          len(heard("Could not download")), 1)
    check("and the log says why",
          any("getaddrinfo" in str(e) for e in sequence.LOGGED["error"]), True)

    # A failed look is not a look: the next start tries again rather than
    # waiting out a day with nothing to show for it.
    github.failure = None
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=False)
    check("so the next start tries again", files(PENDING) is not None, True)

    github = fresh(GitHub())
    community.stage(ROOT, {"enumain.dic": b"kept\tword\r\n"}, FIRST)
    community.prepare(ROOT)
    github.head = SECOND
    github.body = archive(files={"ENUmain.dic": b"broken line\r\n"}, revision=SECOND)
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("a bad archive leaves the set in use alone and nothing waiting",
          (read(os.path.join(CURRENT, "enumain.dic")), files(PENDING)),
          (b"kept\tword\r\n", None))
    check("and the reader hears that it could not",
          len(heard("Could not download")), 1)

    github = fresh(GitHub(head=b"<html>Moved</html>"))
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("an answer that is not a commit is not followed to an archive",
          (github.fetchedArchive(), files(PENDING)), (False, None))

    github = fresh(GitHub())
    github.body = b"x" * 64
    limit = community.MAX_ARCHIVE
    try:
        community.MAX_ARCHIVE = 32
        u = community.Updater(ROOT)
        u.prepare()
        u.want(True, byReader=True)
    finally:
        community.MAX_ARCHIVE = limit
    check("an archive past the limit is not taken",
          (files(PENDING), len(heard("Could not download"))), (None, 1))

    github = fresh(GitHub())
    sys.modules["globalVars"].appArgs.secure = True
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("on a secure screen nothing is fetched at all", github.asked, [])
    sys.modules["globalVars"].appArgs.secure = False


def unticked_checks():
    github = fresh()
    u = community.Updater(ROOT)
    u.prepare()

    def changedMind():
        u.want(False, byReader=True)

    github.during = changedMind
    u.want(True, byReader=True)
    check("unticked while it downloads, what arrives is thrown away",
          files(PENDING), None)
    check("and the reader is not told it was downloaded, or asked anything",
          (heard(DOWNLOADED), ASKED), ([], []))

    github = fresh()
    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    u = community.Updater(ROOT)
    check("a set in use", u.prepare(), CURRENT)
    u.want(False, byReader=True)
    check("unticked, it is still the set in use until the next start",
          files(CURRENT), ["enumain.dic", "source.ini"])
    check("and the reader is told when it goes",
          len(heard("stop using")), 1)
    check("which it does, at the start whose saved setting is unticked",
          community.Updater(ROOT).prepare(False), None)

    github = fresh()
    u = community.Updater(ROOT)
    u.prepare()
    u.want(False, byReader=True)
    check("unticked with nothing downloaded, there is nothing to say",
          (SAID, github.asked), ([], []))

    fresh()
    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    u = community.Updater(ROOT)
    u.prepare()
    community.stage(ROOT, {"enumain.dic": b"c\td\r\n"}, SECOND)
    real = community.discard

    def held(root):
        raise PermissionError("held open by a virus scanner")

    community.discard = held
    try:
        u.want(False, byReader=True)
    finally:
        community.discard = real
    check("a download waiting that cannot be thrown away is said in the log",
          any("throw away" in e for e in sequence.LOGGED["error"]), True)
    check("and the reader is still told the set goes", len(heard("stop using")), 1)
    check("and at the next start, saved unticked, neither is used",
          community.Updater(ROOT).prepare(False), None)

    fresh()
    u = community.Updater(ROOT)
    check("nothing in use at the start", u.prepare(), None)
    community.stage(ROOT, {"enumain.dic": b"a\tb\r\n"}, FIRST)
    u.want(False, byReader=True)
    check("unticked, what is waiting goes at once, since the engine never"
          " read it", (files(PENDING), os.listdir(ROOT)), (None, []))
    check("and the reader, who heard it was downloaded, hears it went",
          len(heard("discarded")), 1)

    github = fresh()
    u = community.Updater(ROOT)
    u.prepare()
    u.stop()
    u.want(True, byReader=False)
    check("once the driver has gone, nothing is written",
          files(PENDING), None)


# ---- asking to restart -------------------------------------------------------


def offer_checks():
    """The add-on store asks to restart once it is closed. This asks once the
    reader is out of NVDA's settings, because restarting under an open one
    keeps nothing unsaved in it, and the tick that asked is unsaved until OK."""
    fresh()
    OPEN[0] = True
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("downloaded with the settings open, it does not ask yet", ASKED, [])
    check("but waits", len(LATER), 1)
    tick()
    check("and goes on waiting while they stay open", (ASKED, len(LATER)), ([], 1))
    OPEN[0] = False
    MODAL[0] = True
    tick()
    check("and while another message box wants an answer",
          (ASKED, len(LATER)), ([], 1))
    MODAL[0] = False
    tick()
    check("then asks once they are closed", (ASKED, LATER), ([True], []))

    fresh()
    OPEN[0] = True
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    u.want(False, byReader=True)
    OPEN[0] = False
    tick()
    check("cancelled before the settings closed, it never asks", ASKED, [])
    check("and what was downloaded is gone", files(PENDING), None)
    check("and the reader is told, since Escape is Cancel and an easy key to"
          " press", len(heard("discarded")), 1)

    fresh()
    OPEN[0] = True
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    u.want(True, byReader=True)
    check("asked twice while the settings are open, one offer waits",
          len(LATER), 1)
    OPEN[0] = False
    tick()
    check("and the reader is asked once", ASKED, [True])

    fresh()
    OPEN[0] = True
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    u.stop()
    OPEN[0] = False
    tick()
    check("a driver gone before the settings closed never asks", ASKED, [])

    fresh()

    def noWindow(fn):
        raise RuntimeError("no GUI thread")

    community._onGui = noWindow
    u = community.Updater(ROOT)
    u.prepare()
    u.want(True, byReader=True)
    check("a question that cannot be shown does not undo the download",
          (community.revisionOf(PENDING), len(heard(DOWNLOADED)),
           heard("Could not download")), (FIRST, 1, []))
    check("and the log says it was the question",
          any("offer to restart" in e for e in sequence.LOGGED["error"]), True)


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def restart_checks():
    """The question itself, with NVDA's windows and restart stood in for."""
    fresh()
    import importlib

    real = importlib.reload(community)
    done = []

    class Conf(dict):
        def save(self):
            done.append("saved")

    answer = [None]
    asked = []

    def messageBox(message=None, caption=None, style=None):
        asked.append((caption, style))
        return answer[0]

    wx = _Stub(YES=2, NO=8, ICON_WARNING=256)
    sys.modules["wx"] = wx
    sys.modules["core"] = _Stub(restart=lambda: done.append("restarted"))
    sys.modules["gui"] = _Stub(messageBox=messageBox)
    sys.modules["config"].conf = Conf(sys.modules["config"].conf)
    modal = [False]
    real._modalOpen = lambda: modal[0]
    real._later = lambda milliseconds, fn: None

    # NVDA's own record of its settings dialogs, as gui.settingsDialogs keeps it.
    import enum
    import weakref

    class SettingsDialog:
        class DialogState(enum.IntEnum):
            CREATED = 0
            DESTROYED = 1

        _instances = weakref.WeakKeyDictionary()

    sys.modules["gui.settingsDialogs"] = _Stub(SettingsDialog=SettingsDialog)
    sys.modules["gui.message"] = _Stub(isModalMessageBoxActive=lambda: "asked")
    try:
        check("no settings dialog, none open", real._settingsOpen(), False)
        one = SettingsDialog()
        SettingsDialog._instances[one] = SettingsDialog.DialogState.CREATED
        check("one made and not yet destroyed is open", real._settingsOpen(), True)
        SettingsDialog._instances[one] = SettingsDialog.DialogState.DESTROYED
        check("and once destroyed is not", real._settingsOpen(), False)
        del sys.modules["gui.settingsDialogs"]
        check("an NVDA without that record is taken as having none open",
              real._settingsOpen(), False)
        importlib.reload(real)
        check("a message box is asked about the way NVDA asks",
              real._modalOpen(), "asked")
        real._modalOpen = lambda: modal[0]
        real._later = lambda milliseconds, fn: None
    finally:
        sys.modules.pop("gui.settingsDialogs", None)
        sys.modules.pop("gui.message", None)
    try:
        answer[0] = wx.YES
        real._askRestart()
        check("yes saves the configuration and then restarts",
              done, ["saved", "restarted"])
        check("under NVDA's own title, as a yes or no warning",
              asked[-1], ("Restart NVDA", wx.YES | wx.NO | wx.ICON_WARNING))

        del done[:]
        answer[0] = wx.NO
        real._askRestart()
        check("no does neither", done, [])

        answer[0] = wx.YES
        modal[0] = True
        real._askRestart()
        check("and yes with another message box still open does not restart,"
              " as NVDA will not", done, [])

        class ReadOnly(dict):
            def save(self):
                raise PermissionError("the configuration is read-only")

        modal[0] = False
        sys.modules["config"].conf = ReadOnly(sys.modules["config"].conf)
        real._announce = SAID.append
        del SAID[:]
        real._askRestart()
        check("yes when the configuration cannot be saved does not restart",
              done, [])
        check("and says why", len(heard("could not be saved")), 1)
    finally:
        for name in ("wx", "core", "gui"):
            sys.modules.pop(name, None)


# ---- the driver --------------------------------------------------------------


def driver_checks():
    """The driver as NVDA runs it: made, then handed its saved settings, and
    only then reading dictionaries."""
    github = fresh()
    d = driver()
    check("the setting is offered",
          "communityDictionaries" in [s.id for s in d.supportedSettings], True)
    check("with nothing downloaded the engine reads what it always did",
          folders(d), [(PERSONAL, OWN)])

    d.communityDictionaries = True
    check("ticked, it downloads", community.revisionOf(PENDING), FIRST)
    check("and the reader is told it is downloaded", len(heard(DOWNLOADED)), 1)
    check("and asked whether to restart", ASKED, [True])
    # The fake engine writes down every call, where the real one ignores the
    # same folders asked for twice; what matters is that no call asked for
    # anything different.
    check("but the engine is not asked to read anything new while NVDA runs",
          set(folders(d)), {(PERSONAL, OWN)})
    d.terminate()

    SAVED["communityDictionaries"] = True
    del SAID[:]
    d = driver()
    check("at the next start the community's set is read first, under"
          " everything of the reader's own",
          folders(d), [(CURRENT, PERSONAL, OWN)])
    check("NVDA restoring the setting at the start says nothing", SAID, [])

    d.dictionarySet = "none"
    check("and a change to another dictionary setting keeps it in its place",
          set(folders(d)), {(CURRENT, PERSONAL, OWN)})

    d.communityDictionaries = False
    check("unticked, the engine still reads it until the next start",
          set(folders(d)), {(CURRENT, PERSONAL, OWN)})
    check("and the reader is told so", len(heard("stop using")), 1)
    d.terminate()

    SAVED["communityDictionaries"] = False
    d = driver()
    check("after which it is gone", folders(d), [(PERSONAL, OWN)])
    check("from the disk as well", os.path.isdir(CURRENT), False)
    d.terminate()

    # Read as each saved setting came back, the dictionaries were read, freed
    # and read again, and freeing the community's root file is the slow part.
    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED.update(communityDictionaries=True, personalDictionary=False,
                 dictionarySet="none")
    d = driver()
    check("saved settings that change the folders read the dictionaries once,"
          " from the folders they ask for", folders(d), [(CURRENT, OWN)])
    d.terminate()

    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    community.prepare(ROOT)
    community.stage(ROOT, {"enuroot.dic": b"c\td\r\n"}, SECOND)
    SAVED["communityDictionaries"] = False
    d = driver()
    check("saved unticked, a set on disk is never handed to the engine",
          folders(d), [(PERSONAL, OWN)])
    check("and it and the download waiting are gone at once",
          (os.path.isdir(CURRENT), os.path.isdir(PENDING)), (False, False))
    d.terminate()

    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    d = driver()
    check("with no setting saved at all, as on NVDA's first load, a set left"
          " on disk is not used", folders(d), [(PERSONAL, OWN)])
    d.terminate()

    fresh()
    OPEN[0] = True
    d = driver()
    d.communityDictionaries = True
    d.terminate()
    OPEN[0] = False
    tick()
    check("switched to another synthesiser before the settings closed, the"
          " reader is not asked to restart for this one", ASKED, [])


# ---- configuration profiles ---------------------------------------------------


def profile_checks():
    """NVDA keeps a copy of every synthesiser setting in each configuration
    profile and hands a profile's copy to the driver whenever the profile
    comes into force or goes. The set is read once, at a start, and a start
    is always in the normal configuration, so the box is one setting for all
    of NVDA: a profile's copy is never taken, and OK writes the reader's
    choice into the normal configuration whichever profile is being edited."""
    github = fresh()
    SAVED["communityDictionaries"] = False
    d = driver()
    before = list(folders(d))
    _enter(d, {"communityDictionaries": True})
    check("a profile that ticks the box coming into force downloads nothing",
          github.asked, [])
    check("says nothing and asks nothing", (SAID, ASKED), ([], []))
    check("and the box stays as the normal configuration has it",
          d.communityDictionaries, False)
    _leave(d)
    check("nor does anything happen as it goes", (github.asked, SAID, ASKED),
          ([], [], []))
    check("and the engine is never asked to read anything else",
          folders(d), before)
    d.terminate()

    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = True
    d = driver()
    community.stage(ROOT, {"enuroot.dic": b"c\td\r\n"}, SECOND)
    del SAID[:]
    _enter(d, {"communityDictionaries": False})
    check("a profile that unticks it coming into force says nothing", SAID, [])
    check("and throws nothing waiting away", community.revisionOf(PENDING), SECOND)
    _leave(d)
    d.terminate()

    # A driver made while a profile is in force, as when the reader changes
    # synthesiser and back inside an application that has one.
    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = True
    CONF.profiles.append({"speech": {"openevv": {"communityDictionaries": False}}})
    d = driver()
    check("a driver made under a profile that unticks it reads what the"
          " normal configuration asks for", folders(d), [(CURRENT, PERSONAL, OWN)])
    d.terminate()

    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = False
    CONF.profiles.append({"speech": {"openevv": {"communityDictionaries": True}}})
    d = driver()
    check("and under one that ticks it, over a normal configuration that"
          " does not, reads no set", folders(d), [(PERSONAL, OWN)])
    d.terminate()

    # Ticked and saved inside a profile, over a normal configuration that has
    # never held the box: how a reader whose settings are saved in profiles
    # meets it.
    fresh()
    SAVED["rate"] = 50
    d = driver()
    profile = _enter(d, {})
    OPEN[0] = True
    d.communityDictionaries = True
    d.saveSettings()
    OPEN[0] = False
    check("OK inside a profile writes the box into the normal configuration"
          " as well as the profile",
          (SAVED.get("communityDictionaries"), _section(profile).get("communityDictionaries")),
          (True, True))
    tick()
    check("and the reader is asked to restart once out of the settings", ASKED, [True])
    d.terminate()
    CONF.profiles.pop()
    d = driver()
    check("the next start reads the set", folders(d), [(CURRENT, PERSONAL, OWN)])
    d.terminate()

    # Only a driver made under a profile can find the normal configuration
    # without a section of its own: at a start NVDA writes one first.
    fresh()
    BASE.clear()
    CONF.profiles.append({"speech": {"openevv": {"rate": 50}}})
    d = driver()
    d.communityDictionaries = True
    d.saveSettings()
    check("OK inside a profile over a normal configuration with no section of"
          " the driver's at all makes one",
          _section(BASE).get("communityDictionaries"), True)
    d.terminate()

    # OK inside a profile whose own copy differs, with the box untouched.
    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = True
    d = driver()
    profile = _enter(d, {"communityDictionaries": False})
    d.saveSettings()
    check("OK inside a profile with the box untouched keeps the normal"
          " configuration's", SAVED["communityDictionaries"], True)
    check("and makes the profile's copy agree with it",
          _section(profile)["communityDictionaries"], True)
    d.terminate()

    # Cancel puts the box back as the normal configuration has it, not as the
    # profile in force has it (gui/settingsDialogs.py: onDiscard hands every
    # saved setting back).
    fresh()
    SAVED["communityDictionaries"] = False
    d = driver()
    _enter(d, {"communityDictionaries": True})
    OPEN[0] = True
    d.communityDictionaries = True
    check("ticked inside the settings under that profile, it downloads",
          community.revisionOf(PENDING), FIRST)
    del SAID[:]
    d.loadSettings()
    OPEN[0] = False
    tick()
    check("Cancel puts the box back as the normal configuration has it",
          d.communityDictionaries, False)
    check("and throws the download away", community.revisionOf(PENDING), None)
    check("saying so", len(heard("discarded")), 1)
    check("and asks nothing", ASKED, [])
    d.terminate()

    # A profile coming into force while the reader has ticked the box and the
    # settings are still open: the tick is not a profile's to undo.
    fresh()
    SAVED["communityDictionaries"] = False
    d = driver()
    OPEN[0] = True
    d.communityDictionaries = True
    _enter(d, {"communityDictionaries": False})
    check("a profile coming into force before OK leaves the reader's tick alone",
          (d.communityDictionaries, community.revisionOf(PENDING)), (True, FIRST))
    d.saveSettings()
    OPEN[0] = False
    check("and OK then saves it", SAVED["communityDictionaries"], True)
    d.terminate()

    # The box as the normal configuration holds it after a restart, before
    # NVDA has checked it against the driver's specification: the text read
    # from the file.
    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = "True"
    d = driver()
    check("saved as the text True it is ticked", folders(d), [(CURRENT, PERSONAL, OWN)])
    d.terminate()

    fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    SAVED["communityDictionaries"] = "False"
    d = driver()
    check("and as the text False it is not", folders(d), [(PERSONAL, OWN)])
    d.terminate()

    # The look a start takes on its own, which the setter used to start.
    github = fresh()
    SAVED["communityDictionaries"] = True
    d = driver()
    check("a start saved ticked, with nothing downloaded, fetches",
          github.fetchedArchive(), True)
    check("saying nothing and asking nothing", (SAID, ASKED), ([], []))
    d.terminate()

    github = fresh(GitHub(head=SECOND))
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    community.prepare(ROOT)
    community._looked(ROOT)
    NOW[0] += community.CHECK_EVERY + 1
    SAVED["communityDictionaries"] = True
    d = driver()
    check("a start whose day is up fetches again", github.fetchedArchive(), True)
    check("quietly", (SAID, ASKED), ([], []))
    check("and the engine reads from the folders it always did",
          folders(d), [(CURRENT, PERSONAL, OWN)])
    d.terminate()

    github = fresh()
    community.stage(ROOT, {"enuroot.dic": b"a\tb\r\n"}, FIRST)
    community.prepare(ROOT)
    community._looked(ROOT)
    SAVED["communityDictionaries"] = True
    d = driver()
    check("a start that looked today does not look again", github.asked, [])
    d.terminate()

    # A setting that will not take while NVDA hands settings back.
    fresh()
    SAVED["communityDictionaries"] = False
    d = driver()
    cls = type(d)
    real = cls.__dict__["_set_headSize"]

    def refuse(self, value):
        raise RuntimeError("a setting that will not take")

    cls._set_headSize = refuse
    try:
        try:
            _enter(d, {"headSize": 10})
        except RuntimeError:
            pass
    finally:
        cls._set_headSize = real
    d.communityDictionaries = True
    check("after a setting failed as NVDA handed it back, a tick is still the"
          " reader's", (community.revisionOf(PENDING), len(heard(DOWNLOADED))), (FIRST, 1))
    d.terminate()


# ---- the real reader, against a real server ----------------------------------


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def network_checks():
    """The limit is the network code's to keep, so it is checked there."""
    fresh()
    import importlib

    real = importlib.reload(community)
    served = os.path.join(sequence.CONFIG, "served")
    os.makedirs(served, exist_ok=True)
    _write(os.path.join(served, "small"), b"a" * 100)
    _write(os.path.join(served, "large"), b"a" * 5000)

    def handler(*args, **kwargs):
        return _Quiet(*args, directory=served, **kwargs)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % server.server_address[1]
    try:
        check("what is under the limit is read whole",
              len(real._get(base + "small", "*/*", 1000)), 100)
        try:
            real._get(base + "large", "*/*", 1000)
        except ValueError:
            check("what is over it is refused, not read", True, True)
        else:
            check("what is over it is refused, not read", "read", "refused")
        try:
            real._get(base + "missing", "*/*", 1000)
        except OSError:
            check("a missing page is an error, not an empty answer", True, True)
        else:
            check("a missing page is an error, not an empty answer",
                  "answered", "error")
    finally:
        server.shutdown()


def main():
    validation_checks()
    prepare_checks()
    updater_checks()
    failure_checks()
    unticked_checks()
    offer_checks()
    driver_checks()
    profile_checks()
    restart_checks()
    network_checks()

    if FAILED:
        print("\ncommunity: %d of the checks failed" % len(FAILED))
        return 1
    print("\ncommunity: every check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
