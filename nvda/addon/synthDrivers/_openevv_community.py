# The community's pronunciation dictionaries, fetched and kept up to date.
#
# IBMTTSDictionaries is a set of dictionary files the community keeps on
# GitHub, under CC0. Rather than copying them in by hand, the reader can tick a
# box in the voice settings and have the driver fetch them, and from then on
# look once a day for a newer commit.
#
# What decides how this is built is when a download may reach the engine. The
# engine looks at the files it has read every two seconds and reads everything
# again when one changes, and freeing the community's seventy thousand root
# words on the way is slow enough to leave the reader with no voice (upstream
# issue #49). So nothing here ever writes into a folder the engine is reading.
# A download is checked and written beside the set in use, and prepare() puts
# it in its place as the driver starts, before any dictionary has been read.
# Ticking the box fetches now and is heard from the next start; unticking takes
# the set away at the next start, for the same reason.
#
# A set is replaced whole and never merged into, so a word the community takes
# out is gone, and nothing of the reader's own is ever in these files. Their own
# entries are in the places the driver reads after this one, and win.

import configparser
import io
import os
import re
import shutil
import threading
import time
import zipfile

from logHandler import log

from ._openevv import DICTIONARY_CODES

#: Where the community keeps them.
REPOSITORY = "eigencrow/IBMTTSDictionaries"

#: The newest commit on the repository's default branch, asked for as the bare
#: forty characters of its hash rather than a page of JSON about it.
HEAD_URL = "https://api.github.com/repos/%s/commits/HEAD" % REPOSITORY
HEAD_ACCEPT = "application/vnd.github.sha"

#: The repository at one commit, as GitHub packs it.
ARCHIVE_URL = "https://github.com/%s/archive/%%s.zip" % REPOSITORY

#: How long a start waits before it looks again.
CHECK_EVERY = 24 * 60 * 60

#: The most each answer may be. The archive is under a megabyte and its largest
#: file a little over two; these leave room to grow and none to be fed
#: something absurd.
MAX_HEAD = 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
#: And all the files together, since an archive can unpack to far more than it
#: is and every file is held while the rest are checked.
MAX_TOTAL = 64 * 1024 * 1024

#: How long one request may take, in seconds.
TIMEOUT = 30

#: The community's folder in the NVDA configuration, and what is in it.
FOLDER = "openevv-community"
CURRENT = "current"
PENDING = "pending"
PARTIAL = "pending.partial"
OLD = "current.old"
REMOVE = "remove"
SOURCE = "source.ini"
STATE = "state.ini"

_NAME = re.compile(r"^([a-z]{3})(main|root|abbr)\.dic$", re.IGNORECASE)
_REVISION = re.compile(r"^[0-9a-f]{40}$")

#: One change to the folder at a time, whichever driver is making it: a driver
#: switched away from and back to can have a download still finishing while
#: the next one starts.
_FILES = threading.Lock()


def base():
	"""The community's folder in the reader's NVDA configuration.

	Not the add-on's own directory, which NVDA replaces whole on every update,
	and not the openevv folder beside this one, which is the reader's own.
	"""
	import globalVars

	return os.path.join(globalVars.appArgs.configPath, FOLDER)


def _secure():
	"""Whether this NVDA is on a sign-in or other secure screen, where nothing
	is fetched from anywhere."""
	import globalVars

	return bool(getattr(globalVars.appArgs, "secure", False))


# ---- what a download may contain -------------------------------------------


def validate(archive):
	"""The dictionary files in an archive, by lower-case name, or ValueError.

	Only files at the top of the archive's one folder whose names say a
	language the engine has and one of its three volumes are taken; the rest
	of the repository is passed over. Every line of every file has to be a
	word, a tab and something to say instead, since that is the format and a
	file not in it is what an error page or a bad merge looks like. One bad
	file refuses the whole download, so a set is never half replaced.
	"""
	codes = set(DICTIONARY_CODES.values())
	try:
		z = zipfile.ZipFile(io.BytesIO(archive))
	except zipfile.BadZipFile as e:
		raise ValueError("not an archive: %s" % e)
	found = {}
	total = 0
	with z:
		# GitHub packs a repository as one folder named for it and the commit.
		# An archive that is anything else is not one of GitHub's.
		tops = {info.filename.split("/")[0] for info in z.infolist()}
		if len(tops) != 1:
			raise ValueError("the archive is %d folders, not one" % len(tops))
		for info in z.infolist():
			parts = info.filename.split("/")
			if info.is_dir() or len(parts) != 2:
				continue
			match = _NAME.match(parts[1])
			if match is None or match.group(1).lower() not in codes:
				continue
			# Lower case, because Windows takes ENUmain.dic and enumain.dic
			# for one file and the engine layer matches without regard to
			# case anyway.
			name = parts[1].lower()
			if name in found:
				raise ValueError("%s is in the archive twice" % name)
			if info.file_size > MAX_FILE:
				raise ValueError("%s is %d bytes" % (parts[1], info.file_size))
			if total + info.file_size > MAX_TOTAL:
				raise ValueError("the dictionaries come to more than %d bytes" % MAX_TOTAL)
			with z.open(info) as f:
				data = f.read(MAX_FILE + 1)
			if len(data) > MAX_FILE:
				raise ValueError("%s is more than %d bytes" % (parts[1], MAX_FILE))
			# Again for what was read, which a damaged archive can make more
			# than it said.
			total += len(data)
			if total > MAX_TOTAL:
				raise ValueError("the dictionaries come to more than %d bytes" % MAX_TOTAL)
			_checkFile(parts[1], data)
			found[name] = data
	if not found:
		raise ValueError("there is no dictionary file in the archive")
	return found


def _checkFile(name, data):
	if data.startswith(b"\xef\xbb\xbf"):
		# The engine reads bytes in the language's code page, so a byte order
		# mark would be read as part of the first word.
		raise ValueError("%s starts with a byte order mark" % name)
	if b"\0" in data:
		raise ValueError("%s has a nul in it" % name)
	for number, line in enumerate(data.split(b"\n"), 1):
		line = line.rstrip(b"\r")
		if not line:
			continue
		word, tab, said = line.partition(b"\t")
		if not tab or not word or not said:
			raise ValueError(
				"%s line %d is not a word, a tab and what to say instead" % (name, number)
			)


# ---- the folder ----------------------------------------------------------------


def revisionOf(folder):
	"""The commit a set came from, or None where the folder holds no finished
	set. source.ini is the last thing written, so it is what says a set is
	whole."""
	source = configparser.ConfigParser()
	try:
		if not source.read(os.path.join(folder, SOURCE), encoding="utf-8"):
			return None
	except (configparser.Error, UnicodeDecodeError):
		return None
	revision = source.get("source", "revision", fallback="")
	return revision if _REVISION.match(revision) else None


def _remove(path):
	if os.path.isdir(path):
		shutil.rmtree(path)
	elif os.path.lexists(path):
		os.remove(path)


def stage(root, files, revision):
	"""Write a set beside the one in use, to be put in its place at the next
	start.

	Written under a name of its own and renamed once it is whole, so what the
	next start finds is a finished set or nothing.
	"""
	partial = os.path.join(root, PARTIAL)
	pending = os.path.join(root, PENDING)
	with _FILES:
		_remove(partial)
		os.makedirs(partial)
		for name, data in files.items():
			with open(os.path.join(partial, name), "wb") as f:
				f.write(data)
		source = configparser.ConfigParser()
		source["source"] = {
			"repository": REPOSITORY,
			"revision": revision,
			"fetched": "%d" % _now(),
		}
		with open(os.path.join(partial, SOURCE), "w", encoding="utf-8") as f:
			source.write(f)
		_remove(pending)
		os.replace(partial, pending)


def prepare(root, wanted=True):
	"""Put a finished download in its place, or take the set away if the
	reader asked; then answer the folder to read, or None.

	The driver calls this once NVDA has handed back the saved settings and
	before it has read a dictionary, which is the one moment the set can
	change without the engine having read it. wanted is the saved setting:
	saved unticked, the set and anything waiting go now. Anything that goes
	wrong leaves the set that was there.
	"""
	current = os.path.join(root, CURRENT)
	pending = os.path.join(root, PENDING)
	old = os.path.join(root, OLD)
	with _FILES:
		try:
			if not wanted or os.path.lexists(os.path.join(root, REMOVE)):
				if any(os.path.lexists(os.path.join(root, leaf)) for leaf in (CURRENT, PENDING)):
					log.info("openevv: the community dictionaries are no longer used, as asked")
				for leaf in (CURRENT, PENDING, PARTIAL, OLD, REMOVE):
					_remove(os.path.join(root, leaf))
				return None
			# A start that stopped between the swap's two renames left the
			# set that was in use under the old name.
			if not os.path.isdir(current) and os.path.isdir(old):
				os.replace(old, current)
			_remove(old)
			_remove(os.path.join(root, PARTIAL))
			if revisionOf(pending) is not None:
				if os.path.isdir(current):
					os.replace(current, old)
				try:
					os.replace(pending, current)
				except OSError:
					if os.path.isdir(old):
						os.replace(old, current)
					raise
				_remove(old)
				log.info("openevv: community dictionaries %s are now in use" % revisionOf(current))
			else:
				_remove(pending)
		except Exception:  # noqa: BLE001
			log.error("openevv: could not put the community dictionaries in place", exc_info=True)
	return current if revisionOf(current) is not None else None


def requestRemoval(root):
	"""Ask the next start to take the set in use away."""
	with _FILES:
		os.makedirs(root, exist_ok=True)
		with open(os.path.join(root, REMOVE), "w", encoding="utf-8") as f:
			f.write("taken away at the next start\n")


def cancelRemoval(root):
	with _FILES:
		try:
			os.remove(os.path.join(root, REMOVE))
		except FileNotFoundError:
			pass


def discard(root):
	"""Throw away a download that is waiting. The engine never reads what is
	waiting, so unlike the set in use it can go at once."""
	with _FILES:
		_remove(os.path.join(root, PARTIAL))
		_remove(os.path.join(root, PENDING))


def _lastLooked(root):
	state = configparser.ConfigParser()
	try:
		state.read(os.path.join(root, STATE), encoding="utf-8")
		return state.getfloat("state", "checked", fallback=None)
	except (configparser.Error, ValueError, UnicodeDecodeError):
		return None


def _looked(root):
	"""Write down that GitHub answered, so the next start waits a day."""
	state = configparser.ConfigParser()
	state["state"] = {"checked": "%d" % _now()}
	os.makedirs(root, exist_ok=True)
	with open(os.path.join(root, STATE), "w", encoding="utf-8") as f:
		state.write(f)


# ---- the network, the thread, the voice and the restart --------------------
#
# Each of these is looked up when it is used, so the checks beside the add-on
# can stand in for GitHub, the thread, the reader's ears and NVDA's windows.


def _get(url, accept, limit):
	"""The body at an address, refused rather than read if it runs past limit.

	Through requests where NVDA has it, which is how NVDA's own add-on store
	fetches from GitHub and carries its own certificates; through the standard
	library where it has not.
	"""
	headers = {"Accept": accept, "User-Agent": "openevv-nvda"}
	try:
		import requests
	except ImportError:
		requests = None
	if requests is not None:
		with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as response:
			response.raise_for_status()
			body = bytearray()
			for chunk in response.iter_content(65536):
				body += chunk
				if len(body) > limit:
					raise ValueError("%s is more than %d bytes" % (url, limit))
			return bytes(body)
	import urllib.request

	request = urllib.request.Request(url, headers=headers)
	with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
		body = response.read(limit + 1)
	if len(body) > limit:
		raise ValueError("%s is more than %d bytes" % (url, limit))
	return body


def _spawn(target):
	threading.Thread(target=target, name="openevv.community", daemon=True).start()


def _announce(text):
	"""Say something to the reader, from whichever thread this is."""
	import queueHandler
	import ui

	queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)


def _now():
	return time.time()


def _onGui(fn):
	"""Run on NVDA's GUI thread, which is the only one that may show a window."""
	import wx

	wx.CallAfter(fn)


def _later(milliseconds, fn):
	import wx

	wx.CallLater(milliseconds, fn)


def _settingsOpen():
	"""Whether one of NVDA's settings dialogs is open.

	NVDA restarting closes them and keeps nothing unsaved in them, and the
	tick that asked for these dictionaries is unsaved until OK is pressed.
	"""
	try:
		from gui.settingsDialogs import SettingsDialog

		destroyed = SettingsDialog.DialogState.DESTROYED
		states = list(dict(SettingsDialog._instances).values())
	except Exception:  # noqa: BLE001
		log.debugWarning("openevv: could not tell whether a settings dialog is open", exc_info=True)
		return False
	return any(state != destroyed for state in states)


def _modalOpen():
	"""Whether a message box is waiting for an answer, which NVDA will not
	exit under."""
	try:
		from gui.message import isModalMessageBoxActive
	except ImportError:
		return False
	return isModalMessageBoxActive()


def _askRestart():
	"""Ask to restart, as NVDA asks once add-ons have changed.

	gui.addonGui.promptUserForRestart is the model: the same title, the same
	yes and no, and the same refusal to restart while another message box is
	waiting for an answer. What it adds is saving the configuration first, as
	NVDA does before the restart a change of language asks for, so the box
	that asked for these is still ticked when NVDA comes back even where
	the configuration is not saved on exit.
	"""
	import config
	import core
	import gui
	import wx

	result = gui.messageBox(
		# Translators: Asked once the community dictionaries have been
		# downloaded, which NVDA only starts using when it starts again.
		message=_(
			"The community dictionaries have been downloaded. "
			"You must restart NVDA for them to take effect, and your configuration will be saved first. "
			"Would you like to restart now?"
		),
		# Translators: Title for the message asking whether to restart NVDA.
		caption=_("Restart NVDA"),
		style=wx.YES | wx.NO | wx.ICON_WARNING,
	)
	if result != wx.YES:
		return
	if _modalOpen():
		try:
			import ui
			from gui import blockAction

			# NVDA says this the same way, after the same delay, for the same reason.
			_later(500, lambda: ui.message(blockAction.Context.MODAL_DIALOG_OPEN.translatedMessage))
		except Exception:  # noqa: BLE001
			log.debugWarning("openevv: could not say why NVDA was not restarted", exc_info=True)
		return
	try:
		config.conf.save()
	except Exception:  # noqa: BLE001
		# Restarting now would come back with the box unticked, having said
		# the configuration would be saved first. The download stays and is
		# used at the next start whenever that is.
		log.error("openevv: could not save the configuration, so NVDA was not restarted",
		          exc_info=True)
		# Translators: Said when NVDA was not restarted for the community
		# dictionaries because its configuration could not be saved.
		_announce(_("NVDA was not restarted, because its configuration could not be saved."))
		return
	core.restart()


# ---- one driver's downloads ------------------------------------------------


class Updater:
	"""The download, for one driver: asked for when the reader ticks the box
	and when NVDA starts with it ticked, and never more than one at a time."""

	def __init__(self, root=None):
		self.root = root if root is not None else base()
		#: Whether a set has been in use since this start, which unticking has
		#: to know to say when it goes.
		self.inUse = False
		self._lock = threading.Lock()
		self._running = False
		self._wanted = False
		self._stopped = False
		#: Whether the download under way owes the reader an answer. NVDA
		#: starting with the box ticked does not; the reader ticking it does,
		#: even when it lands on a download NVDA's start had already begun.
		self._tell = False
		#: Whether an offer to restart is already on its way, so a reader who
		#: ticks the box twice is asked once.
		self._offering = False

	def prepare(self, wanted=True):
		folder = prepare(self.root, wanted)
		self.inUse = folder is not None
		return folder

	def want(self, enable, byReader):
		"""What the setting says now. byReader is whether the reader changed it,
		as opposed to NVDA handing back what was saved."""
		current = os.path.join(self.root, CURRENT)
		going = False
		with self._lock:
			if self._stopped:
				return
			self._wanted = bool(enable)
			if not enable:
				# Under the lock a download stages under, so one finishing now
				# is either thrown away here or sees it is no longer wanted.
				# The set in use is asked to go first, and whatever happens to
				# the download waiting does not stop that.
				if self.inUse or os.path.isdir(current):
					try:
						requestRemoval(self.root)
						going = True
					except OSError:
						log.error("openevv: could not ask for the community dictionaries"
						          " to be taken away", exc_info=True)
				try:
					discard(self.root)
				except OSError:
					log.error("openevv: could not throw away a community dictionary download",
					          exc_info=True)
				if byReader and going:
					# Translators: Said when the reader stops using the community
					# dictionaries, which takes effect when NVDA next starts.
					_announce(_("NVDA will stop using the community dictionaries the next time it starts."))
				return
		try:
			cancelRemoval(self.root)
		except OSError:
			log.error("openevv: could not keep the community dictionaries", exc_info=True)
		self._check(force=byReader, tell=byReader)

	def stop(self):
		"""The driver is going: write nothing more, and offer nothing."""
		with self._lock:
			self._stopped = True
			self._wanted = False

	def _due(self):
		"""Whether a start should look: whenever there is nothing to show for
		the last look, and otherwise once a day."""
		if (revisionOf(os.path.join(self.root, CURRENT)) is None
		    and revisionOf(os.path.join(self.root, PENDING)) is None):
			return True
		last = _lastLooked(self.root)
		if last is None:
			return True
		now = _now()
		return now - last >= CHECK_EVERY or now < last

	def _check(self, force, tell):
		with self._lock:
			if self._stopped:
				return
			if self._running:
				self._tell = self._tell or tell
				return
			if not force and not self._due():
				return
			self._running = True
			self._tell = tell
		_spawn(self._run)

	def _telling(self):
		with self._lock:
			return self._tell

	def _say(self, text):
		if self._telling():
			_announce(text)

	def _offerRestart(self):
		"""Once the reader asked for these and they are waiting, offer to
		restart; NVDA starting with the box ticked never asks."""
		if not self._telling():
			return
		with self._lock:
			if self._offering:
				return
			self._offering = True
		try:
			_onGui(self._offer)
		except Exception:  # noqa: BLE001
			# The download is done whatever happens here, and is used from the
			# next start; only the question is lost.
			log.error("openevv: could not offer to restart NVDA", exc_info=True)
			with self._lock:
				self._offering = False

	def _offer(self):
		"""Ask once the reader is out of NVDA's settings, as the add-on store
		asks once it is closed, and only while what was asked for still is."""
		with self._lock:
			wanted = self._wanted and not self._stopped
		if not wanted or revisionOf(os.path.join(self.root, PENDING)) is None:
			with self._lock:
				self._offering = False
			return
		if _settingsOpen() or _modalOpen():
			_later(500, self._offer)
			return
		with self._lock:
			self._offering = False
		_askRestart()

	def _run(self):
		try:
			self._download()
		except Exception as e:  # noqa: BLE001
			log.error("openevv: could not download the community dictionaries: %s" % e,
			          exc_info=True)
			# Translators: Said when the community dictionaries could not be
			# downloaded.
			self._say(_("Could not download the community dictionaries."))
		finally:
			with self._lock:
				self._running = False
				self._tell = False

	def _download(self):
		if _secure():
			log.info("openevv: not fetching the community dictionaries on a secure screen")
			return
		head = _get(HEAD_URL, HEAD_ACCEPT, MAX_HEAD).decode("ascii", "replace").strip()
		if not _REVISION.match(head):
			raise ValueError("GitHub answered %r rather than a commit" % head[:80])
		waiting = revisionOf(os.path.join(self.root, PENDING))
		current = revisionOf(os.path.join(self.root, CURRENT))
		if head == waiting:
			_looked(self.root)
			# Translators: Said when the newest community dictionaries were
			# already downloaded and are waiting for NVDA to restart.
			self._say(_("The latest community dictionaries are already downloaded."))
			self._offerRestart()
			return
		if head == current and waiting is None:
			_looked(self.root)
			# Translators: Said when the community dictionaries in use are the
			# newest there are.
			self._say(_("The community dictionaries are up to date."))
			return
		files = validate(_get(ARCHIVE_URL % head, "application/zip", MAX_ARCHIVE))
		with self._lock:
			if not self._wanted or self._stopped:
				log.info("openevv: a community dictionary download was no longer wanted")
				return
			stage(self.root, files, head)
		_looked(self.root)
		log.info("openevv: community dictionaries %s downloaded" % head)
		# Translators: Said when the community dictionaries have been downloaded.
		self._say(_("Community dictionaries downloaded."))
		self._offerRestart()
