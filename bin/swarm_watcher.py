#!/usr/bin/env python3
"""Herdr plugin: show a Claude Code teammate swarm beside the pane that spawned it.

Claude Code (teammateMode = tmux) starts a private tmux server on
  $TMUX_TMPDIR-or-/tmp/tmux-<uid>/claude-swarm-<claude pid>
with one session, `claude-swarm`. This watcher polls that directory, decides
whether a swarm is live (socket + tmux server answering + owning pid alive),
finds the Herdr pane whose process tree contains the claude pid, and opens the
plugin's `swarm` pane as a split beside it. When the swarm dies it closes the
pane it opened, and only that pane.

Subcommands
  start     fork a detached watcher (single instance, pidfile + flock)
  stop      SIGTERM the watcher started by `start` (verified by cmdline)
  run       run the watcher loop in the foreground
  once      run one cycle and exit (see --only / --include-existing)
  status    print watcher pid and tracked swarms
  show      open the swarm view for HERDR_PANE_ID (manual action)
  hide      close the swarm view for HERDR_PANE_ID (manual action)
  toggle    close the view if one is on screen, else open it (one key, both ways)

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

PLUGIN_ID = "cw.claude-swarm"
ENTRYPOINT = "swarm"
SOCKET_ENV = "CW_SWARM_SOCKET"
SESSION_NAME = "claude-swarm"
SOCKET_RE = re.compile(r"^claude-swarm-(\d+)$")
DEFAULT_INTERVAL = 2.0
MAX_MAP_ATTEMPTS = 5  # cycles to keep looking for an owner pane before giving up
STATE_LOCK_WAIT = 5.0  # seconds an action waits for the watcher's state lock before going it alone
LOG_CAP_BYTES = 1_000_000

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"


def tmux_bin():
    """Absolute tmux path. Pane processes get a minimal PATH, so the watcher resolves it."""
    import shutil
    for c in (shutil.which("tmux"), "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if c and os.access(c, os.X_OK):
            return c
    return "tmux"


def plugin_root():
    """The linked plugin directory. Pane commands in the manifest are relative to it."""
    return os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# pure helpers (unit tested)
# ---------------------------------------------------------------------------

def tmux_dir(env=None, uid=None):
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    base = env.get("TMUX_TMPDIR") or "/tmp"
    return os.path.join(base, f"tmux-{uid}")


def parse_socket_name(name):
    m = SOCKET_RE.match(name)
    return int(m.group(1)) if m else None


def swarm_sockets_from_names(names):
    out = {}
    for n in names:
        pid = parse_socket_name(n)
        if pid is not None:
            out[n] = pid
    return out


def swarm_alive(name, pid, socket_exists, server_up, pid_alive):
    """A swarm is live only when all three legs hold. Order is cheapest first."""
    return bool(socket_exists(name) and pid_alive(pid) and server_up(name))


def ancestors(pid, ppid_map):
    seen = set()
    p = pid
    while p and p not in seen and p != 1:
        seen.add(p)
        p = ppid_map.get(p)
    return seen


def match_pid_to_pane(pid, process_infos, ppid_map):
    """process_infos: {pane_id: PaneProcessInfo}. Foreground match first, then shell ancestry."""
    for pane_id, info in process_infos.items():
        for proc in info.get("foreground_processes") or []:
            if proc.get("pid") == pid:
                return pane_id
    anc = ancestors(pid, ppid_map)
    for pane_id, info in process_infos.items():
        shell = info.get("shell_pid")
        if shell and shell in anc:
            return pane_id
    return None


def is_swarm_view(info, socket_name):
    """True when the pane's foreground process is `tmux -L <socket_name> ...`."""
    for proc in (info or {}).get("foreground_processes") or []:
        argv = proc.get("argv") or []
        if proc.get("name") == "tmux" or (argv and os.path.basename(argv[0]) == "tmux"):
            for i, a in enumerate(argv):
                if a == "-L" and i + 1 < len(argv) and argv[i + 1] == socket_name:
                    return True
    return False


def find_existing_view(name, ids=None):
    """The pane already attached to this socket, if any. State says which view we opened; this
    asks Herdr what is actually on screen, which is the only answer that cannot be stale."""
    for pane_id in (pane_ids() if ids is None else ids):
        try:
            if is_swarm_view(process_info(pane_id), name):
                return pane_id
        except HerdrError:
            continue
    return None


def rect_for_pane(layout, pane_id):
    for p in (layout or {}).get("panes") or []:
        if p.get("pane_id") == pane_id:
            return p.get("rect")
    return None


def choose_direction(rect):
    """Terminal cells are about twice as tall as wide, so a pane is 'wide' when
    width >= 2 * height. Wide splits right, otherwise down."""
    if not rect:
        return "right"
    return "right" if rect.get("width", 0) >= 2 * rect.get("height", 0) else "down"


def load_state(path):
    try:
        with open(path) as fh:
            st = json.load(fh)
        if not isinstance(st, dict) or not isinstance(st.get("swarms"), dict):
            raise ValueError("bad shape")
        return st
    except (OSError, ValueError):
        return {"version": 1, "swarms": {}}


def save_state(path, st):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".swarms-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# live probes
# ---------------------------------------------------------------------------

def socket_exists(name):
    return os.path.exists(os.path.join(tmux_dir(), name))


def server_up(name):
    try:
        r = subprocess.run([tmux_bin(), "-L", name, "ls"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pid_alive(pid):
    """False for exited processes, including zombies not yet reaped (kill -0 succeeds on those)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        stat = r.stdout.strip()
        return bool(stat) and not stat.startswith("Z")
    except (OSError, subprocess.TimeoutExpired):
        return True


def ppid_map():
    out = {}
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


def cmdline_of(pid):
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


class HerdrError(Exception):
    def __init__(self, code, message, raw=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.raw = raw


def herdr(*args, timeout=20):
    """Run the herdr CLI and return the parsed `result`. Raises HerdrError on failure."""
    try:
        r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        raise HerdrError("spawn_failed", str(e))
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", " ".join(args))
    if r.returncode != 0:
        code, msg = "cli_error", (r.stderr or r.stdout).strip()
        try:
            err = json.loads(r.stderr.strip().splitlines()[-1])
            code = err.get("error", {}).get("code", code)
            msg = err.get("error", {}).get("message", msg)
        except (ValueError, IndexError, AttributeError):
            pass
        raise HerdrError(code, msg, raw=r.stderr)
    try:
        return json.loads(r.stdout)["result"]
    except (ValueError, KeyError):
        raise HerdrError("bad_json", r.stdout[:200])


def herdr_up():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if sock and not os.path.exists(sock):
        return False
    try:
        herdr("pane", "list", timeout=10)
        return True
    except HerdrError as e:
        return e.code not in ("spawn_failed", "cli_error", "timeout") or "connect" not in e.message.lower()


def pane_ids():
    return [p["pane_id"] for p in herdr("pane", "list").get("panes", [])]


def process_info(pane_id):
    return herdr("pane", "process-info", "--pane", pane_id).get("process_info") or {}


def all_process_infos():
    infos = {}
    for pid_ in pane_ids():
        try:
            infos[pid_] = process_info(pid_)
        except HerdrError:
            continue
    return infos


def find_owner_pane(pid):
    return match_pid_to_pane(pid, all_process_infos(), ppid_map())


def pane_exists(pane_id):
    try:
        herdr("pane", "get", pane_id)
        return True
    except HerdrError as e:
        if e.code in ("not_found", "pane_not_found"):
            return False
        return pane_id in pane_ids()


# ---------------------------------------------------------------------------
# plugin runtime
# ---------------------------------------------------------------------------

class Runtime:
    def __init__(self, state_dir, config_dir=None, log_to_stderr=False):
        self.state_dir = state_dir
        self.config_dir = config_dir
        self.state_path = os.path.join(state_dir, "swarms.json")
        self.pid_path = os.path.join(state_dir, "watcher.pid")
        self.lock_path = os.path.join(state_dir, "watcher.lock")
        self.state_lock_path = os.path.join(state_dir, "state.lock")
        self.log_path = os.path.join(state_dir, "watcher.log")
        self.log_to_stderr = log_to_stderr
        os.makedirs(state_dir, exist_ok=True)
        self.config = self.load_config()
        self.state = load_state(self.state_path)
        self.dead_socket_cache = {}  # name -> mtime at which we last saw it dead

    # -- config / log ------------------------------------------------------
    def load_config(self):
        cfg = {"interval_seconds": DEFAULT_INTERVAL, "direction": "right"}  # "right", "down" or "auto"
        if self.config_dir:
            p = os.path.join(self.config_dir, "config.json")
            try:
                with open(p) as fh:
                    user = json.load(fh)
                if isinstance(user, dict):
                    cfg.update({k: v for k, v in user.items() if k in cfg})
            except (OSError, ValueError):
                pass
        return cfg

    def log(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        if self.log_to_stderr:
            sys.stderr.write(line)
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_CAP_BYTES:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a") as fh:
                fh.write(line)
        except OSError:
            pass

    def save(self):
        save_state(self.state_path, self.state)

    @contextlib.contextmanager
    def state_txn(self):
        """Serialise read-modify-write on swarms.json across processes.

        The watcher loop and every pane action are separate processes holding their own copy of
        the state, and each save() writes the whole file. Without this, an action that opened a
        view could have its view_pane clobbered by a watcher cycle still holding the pre-action
        copy; the next toggle then saw no view, opened a second one, and the swarm appeared twice.
        Re-reading inside the lock is the half that matters -- the lock alone would still let a
        stale in-memory dict win.

        A wedged holder must never make the key dead, so the wait is bounded and we go ahead
        unlocked after it, which is no worse than the behaviour this replaces.
        """
        fd = None
        try:
            fd = os.open(self.state_lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            deadline = time.time() + STATE_LOCK_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        self.log(f"state lock busy for {STATE_LOCK_WAIT}s; proceeding without it")
                        break
                    time.sleep(0.05)
        except OSError as e:
            self.log(f"state lock unavailable ({e}); proceeding without it")
            if fd is not None:
                os.close(fd)
            fd = None
        self.state = load_state(self.state_path)
        before = json.dumps(self.state, sort_keys=True)
        try:
            yield self.state
            # Callers save as they go; this catches a mutation that did not, and writes
            # nothing when a cycle found no news.
            if json.dumps(self.state, sort_keys=True) != before:
                self.save()
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    # -- liveness ----------------------------------------------------------
    def alive(self, name, pid):
        return swarm_alive(name, pid, socket_exists, server_up, pid_alive)

    def live_sockets(self):
        """{name: pid} for sockets in the tmux dir that pass the liveness test.
        Dead sockets are cached by mtime so we do not shell out to tmux for them every cycle."""
        try:
            names = os.listdir(tmux_dir())
        except OSError:
            names = []
        live = {}
        for name, pid in swarm_sockets_from_names(names).items():
            try:
                mtime = os.stat(os.path.join(tmux_dir(), name)).st_mtime
            except OSError:
                continue
            if name in self.state["swarms"] or self.dead_socket_cache.get(name) != mtime:
                if self.alive(name, pid):
                    live[name] = pid
                    self.dead_socket_cache.pop(name, None)
                else:
                    self.dead_socket_cache[name] = mtime
        return live

    # -- show / hide -------------------------------------------------------
    def direction_for(self, owner_pane):
        forced = self.config.get("direction")
        if forced in ("right", "down"):
            return forced
        try:
            layout = herdr("pane", "layout", "--pane", owner_pane).get("layout")
        except HerdrError:
            layout = None
        return choose_direction(rect_for_pane(layout, owner_pane))

    def open_view(self, name, owner_pane):
        direction = self.direction_for(owner_pane)
        res = herdr(
            "plugin", "pane", "open",
            "--plugin", PLUGIN_ID, "--entrypoint", ENTRYPOINT,
            "--placement", "split", "--target-pane", owner_pane, "--direction", direction,
            "--cwd", plugin_root(),
            "--env", f"{SOCKET_ENV}={name}", "--env", f"CW_TMUX={tmux_bin()}", "--no-focus",
        )
        pane_id = (((res.get("plugin_pane") or {}).get("pane") or {}).get("pane_id"))
        if not pane_id:
            raise HerdrError("no_pane_id", json.dumps(res)[:300])
        self.log(f"opened {pane_id} ({direction} of {owner_pane}) for {name}")
        return pane_id

    def close_view(self, pane_id, name):
        """Close a pane this plugin opened. Refuses if the pane no longer runs our tmux attach."""
        try:
            info = process_info(pane_id)
        except HerdrError as e:
            self.log(f"view {pane_id} for {name} already gone ({e.code})")
            return True
        fg = info.get("foreground_processes") or []
        if fg and not is_swarm_view(info, name):
            self.log(f"refusing to close {pane_id}: foreground is {fg[0].get('cmdline')!r}, not our attach")
            return False
        try:
            herdr("plugin", "pane", "close", pane_id)
            self.log(f"closed {pane_id} for {name}")
        except HerdrError as e:
            if e.code in ("not_found", "pane_not_found"):
                self.log(f"view {pane_id} for {name} already gone")
            else:
                self.log(f"close {pane_id} failed: {e}")
                return False
        return True

    # -- state maintenance ---------------------------------------------------
    def verify_persisted_views(self):
        """On start: keep a persisted view pane only if it still runs our attach for that socket."""
        for name, rec in list(self.state["swarms"].items()):
            vp = rec.get("view_pane")
            if not vp:
                continue
            try:
                info = process_info(vp)
                ok = is_swarm_view(info, name)
            except HerdrError:
                ok = False
            if not ok:
                self.log(f"dropping persisted view {vp} for {name}: not our attach any more")
                rec["view_pane"] = None
                rec["dismissed"] = True
        self.save()

    def cycle(self, include_existing=False, only=None, baseline=False):
        """One poll. baseline=True records live swarms without opening anything."""
        with self.state_txn():
            return self.cycle_locked(include_existing, only, baseline)

    def cycle_locked(self, include_existing=False, only=None, baseline=False):
        swarms = self.state["swarms"]
        live = self.live_sockets()
        changed = False

        # 1. tracked swarms that died -> close our view, forget
        for name, rec in list(swarms.items()):
            if name in live:
                continue
            vp = rec.get("view_pane")
            if vp:
                self.close_view(vp, name)
            self.log(f"forgot {name} (swarm gone)")
            del swarms[name]
            changed = True

        # 2. tracked swarms whose view the user closed -> stop re-opening
        for name, rec in swarms.items():
            vp = rec.get("view_pane")
            if vp and not pane_exists(vp):
                self.log(f"view {vp} for {name} closed by someone else; not reopening")
                rec["view_pane"] = None
                rec["dismissed"] = True
                changed = True

        # 3. new or unmapped swarms -> find owner, open view
        def included(n):
            return include_existing and (only is None or n == only)

        for name, pid in live.items():
            rec = swarms.get(name)
            if rec is None:
                rec = {"pid": pid, "owner_pane": None, "view_pane": None, "dismissed": False,
                       "baseline": bool(baseline and not included(name)), "attempts": 0,
                       "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S")}
                swarms[name] = rec
                changed = True
                self.log(f"new swarm {name} (pid {pid}){' [baseline, not shown]' if rec['baseline'] else ''}")
            if rec.get("view_pane") or rec.get("dismissed"):
                continue
            if rec.get("baseline") and not included(name):
                continue
            if only and name != only:
                continue
            if rec.get("attempts", 0) >= MAX_MAP_ATTEMPTS:
                continue
            rec["attempts"] = rec.get("attempts", 0) + 1
            changed = True
            owner = rec.get("owner_pane") or find_owner_pane(pid)
            if not owner:
                self.log(f"{name}: pid {pid} not in any Herdr pane (attempt {rec['attempts']}/{MAX_MAP_ATTEMPTS})")
                continue
            rec["owner_pane"] = owner
            try:
                rec["view_pane"] = self.open_view(name, owner)
                rec["baseline"] = False
                rec["opened_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            except HerdrError as e:
                self.log(f"{name}: open failed: {e}")

        if changed:
            self.save()
        return live

    # -- manual actions ------------------------------------------------------
    def show_for_pane(self, pane_id):
        with self.state_txn():
            return self.show_locked(pane_id)

    def show_locked(self, pane_id):
        try:
            info = process_info(pane_id)
        except HerdrError as e:
            return f"cannot read pane {pane_id}: {e}"
        live = self.live_sockets()
        pmap = ppid_map()
        for name, pid in live.items():
            if match_pid_to_pane(pid, {pane_id: info}, pmap) != pane_id:
                continue
            rec = self.state["swarms"].setdefault(name, {"pid": pid, "owner_pane": pane_id, "view_pane": None,
                                                         "dismissed": False, "baseline": False, "attempts": 0})
            rec["owner_pane"] = pane_id
            if rec.get("view_pane") and pane_exists(rec["view_pane"]):
                self.save()
                return f"{name} already shown in {rec['view_pane']}"
            # Last line of defence against a second copy: state can be wrong (a crash between
            # opening a pane and saving, an older build's lost update), so ask what is on screen
            # and adopt it rather than attaching to the same swarm twice.
            stray = find_existing_view(name)
            if stray:
                rec["view_pane"] = stray
                rec["dismissed"] = False
                self.save()
                self.log(f"adopted existing view {stray} for {name} instead of opening a second")
                return f"{name} already shown in {stray}"
            try:
                rec["view_pane"] = self.open_view(name, pane_id)
                rec["dismissed"] = False
                rec["baseline"] = False
                rec["opened_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.save()
                return f"opened {rec['view_pane']} for {name}"
            except HerdrError as e:
                self.save()
                return f"open failed for {name}: {e}"
        return f"no live swarm owned by {pane_id}"

    def hide_for_pane(self, pane_id):
        with self.state_txn():
            return self.hide_locked(pane_id)

    def hide_locked(self, pane_id):
        for name, rec in self.state["swarms"].items():
            vp = rec.get("view_pane")
            if vp and pane_id in (vp, rec.get("owner_pane")):
                ok = self.close_view(vp, name)
                rec["view_pane"] = None
                rec["dismissed"] = True
                self.save()
                return f"closed {vp} for {name}" if ok else f"could not close {vp} for {name} (see log)"
        return f"no swarm view tracked for {pane_id}"

    def toggle_for_pane(self, pane_id):
        """One binding for both directions. Hides only a view that is really on screen, so a
        record left behind by a pane that went away re-opens instead of eating the keypress.
        Reaches as far as hide does: the key works from the swarm view as well as its owner."""
        with self.state_txn():
            for rec in self.state["swarms"].values():
                vp = rec.get("view_pane")
                if vp and pane_id in (vp, rec.get("owner_pane")) and pane_exists(vp):
                    return self.hide_locked(pane_id)
            return self.show_locked(pane_id)

    # -- daemon --------------------------------------------------------------
    def running_pid(self):
        try:
            with open(self.pid_path) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return None
        if not pid_alive(pid):
            return None
        if "swarm_watcher.py" not in cmdline_of(pid):
            return None
        return pid

    def try_lock(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    def loop(self):
        lock = self.try_lock()
        if lock is None:
            self.log(f"another watcher holds {self.lock_path}; exiting")
            return 0
        with open(self.pid_path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        stop = {"flag": False}

        def on_term(signum, _frame):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        self.log(f"watcher started pid {os.getpid()} interval {self.config['interval_seconds']}s dir {tmux_dir()}")
        self.verify_persisted_views()
        self.cycle(baseline=True)
        herdr_misses = 0
        try:
            while not stop["flag"]:
                time.sleep(float(self.config["interval_seconds"]))
                if stop["flag"]:
                    break
                if not herdr_up():
                    herdr_misses += 1
                    if herdr_misses >= 3:
                        self.log("Herdr server gone; exiting")
                        break
                    continue
                herdr_misses = 0
                try:
                    self.cycle()
                except HerdrError as e:
                    self.log(f"cycle error: {e}")
                except Exception as e:  # keep the loop alive on anything unexpected
                    self.log(f"cycle crashed: {e!r}")
        finally:
            self.log("watcher stopped")
            try:
                os.unlink(self.pid_path)
            except OSError:
                pass
            os.close(lock)
        return 0

    def start_detached(self):
        pid = self.running_pid()
        if pid:
            print(json.dumps({"status": "already_running", "pid": pid}))
            return 0
        if os.fork() != 0:
            # parent: give the child a moment to write its pidfile, then report
            for _ in range(20):
                time.sleep(0.05)
                pid = self.running_pid()
                if pid:
                    break
            print(json.dumps({"status": "started", "pid": pid, "log": self.log_path}))
            return 0
        # child: detach fully
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        os.chdir(plugin_root())
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        logfd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
        self.log_to_stderr = False
        os._exit(self.loop())

    def stop(self):
        pid = self.running_pid()
        if not pid:
            print(json.dumps({"status": "not_running"}))
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            if not pid_alive(pid):
                break
        exited = not pid_alive(pid)
        if exited:
            try:
                os.unlink(self.pid_path)
            except OSError:
                pass
        print(json.dumps({"status": "stopped", "pid": pid, "exited": exited}))
        return 0

    def status(self):
        print(json.dumps({
            "watcher_pid": self.running_pid(),
            "state_dir": self.state_dir,
            "tmux_dir": tmux_dir(),
            "config": self.config,
            "swarms": self.state["swarms"],
        }, indent=1, sort_keys=True))
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def default_state_dir():
    d = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~/.local/state"), "herdr-claude-swarm")


def notify(title, body=""):
    try:
        herdr("notification", "show", title[:80], *(["--body", body[:240]] if body else []), timeout=5)
    except HerdrError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["start", "stop", "run", "once", "status", "show", "hide", "toggle"])
    ap.add_argument("--state-dir", default=default_state_dir())
    ap.add_argument("--config-dir", default=os.environ.get("HERDR_PLUGIN_CONFIG_DIR"))
    ap.add_argument("--pane", default=os.environ.get("HERDR_PANE_ID"), help="pane for show/hide/toggle (default $HERDR_PANE_ID)")
    ap.add_argument("--only", help="once: only act on this socket name")
    ap.add_argument("--include-existing", action="store_true",
                    help="once: treat swarms that were already live as new (default records them without opening)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    rt = Runtime(a.state_dir, a.config_dir, log_to_stderr=not a.quiet and a.cmd != "start")

    if a.cmd == "start":
        return rt.start_detached()
    if a.cmd == "stop":
        return rt.stop()
    if a.cmd == "status":
        return rt.status()
    if a.cmd == "run":
        return rt.loop()
    if a.cmd == "once":
        if rt.running_pid():
            print(json.dumps({"error": "watcher_running", "pid": rt.running_pid(),
                              "hint": "stop it first or the two will race on swarms.json"}))
            return 1
        rt.verify_persisted_views()
        live = rt.cycle(include_existing=a.include_existing, only=a.only, baseline=True)
        print(json.dumps({"live": live, "swarms": rt.state["swarms"]}, indent=1, sort_keys=True))
        return 0
    if a.cmd in ("show", "hide", "toggle"):
        if not a.pane:
            print(json.dumps({"error": "no pane: pass --pane or run as a Herdr action"}))
            return 1
        msg = {"show": rt.show_for_pane, "hide": rt.hide_for_pane, "toggle": rt.toggle_for_pane}[a.cmd](a.pane)
        rt.log(f"{a.cmd} {a.pane}: {msg}")
        print(json.dumps({"result": msg}))
        if os.environ.get("HERDR_PLUGIN_ACTION_ID"):
            notify(f"swarm {a.cmd}", msg)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
