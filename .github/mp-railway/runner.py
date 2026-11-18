"""MP Railway cloud runner (3.21.7): keeps the bot online from GitHub Actions, no server needed.

One workflow run = one ~5 h 40 min shift. The runner
  * restores accounts.db (AES-256-GCM encrypted with a key derived from AUTH_PRIVATE_KEY) from the
    "mp-railway-data" branch, so accounts survive between shifts,
  * starts main.py and restarts it if it crashes,
  * saves the encrypted database back to that branch ~10-30 s after an account change (5.00.05, ghstore.py:
    never with fewer accounts than restored, never when the saved copy could not be read; every save is a
    commit, so the branch history keeps every older copy),
  * auto deploys: a new commit on main is pulled and the bot restarted within 2 minutes,
  * near the end of the shift starts the next run (workflow_dispatch) and hands over.
"""
import base64, hashlib, os, signal, sqlite3, subprocess, sys, tempfile, time, json, urllib.request


ROOT = os.getcwd()
DATA_DIR = os.path.join(ROOT, "data")
DB = os.path.join(DATA_DIR, "accounts.db")
BRANCH = "mp-railway-data"
SHIFT = int(os.environ.get("SHIFT_SECONDS", str(5 * 3600 + 40 * 60)))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
started = time.time()
stop = False


def log(*a):
    print("[mp-railway]", *a, flush=True)


def sh(*cmd, check=True, cwd=None):
    r = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


# ------------------------------------------------------------------ encrypted DB on a branch
# 5.00.05: ghstore.py (same file format as before): never overwrites the GitHub copy with fewer accounts,
# never saves when the saved copy could not be read, and every save is a commit (history = backups).
sys.path.insert(0, ROOT)
import logging
logging.basicConfig(level=logging.INFO, format="[mp-railway] %(message)s")
from ghstore import GhStore

store = GhStore(REPO, TOKEN, os.environ.get("AUTH_PRIVATE_KEY", ""), BRANCH)


def restore():
    os.makedirs(DATA_DIR, exist_ok=True)
    log("restore:", store.restore(DB))


def save(reason):
    try:
        store.save(DB, reason)
    except Exception as e:
        log("save failed:", e)


# ------------------------------------------------------------------ bot process
bot = None


def start_bot():
    global bot
    env = dict(os.environ, DATA_DIR=DATA_DIR, MP_RAILWAY_APP="discordpy-bot", DATA_REPO_TOKEN="",   # the runner saves, not the bot
               MP_RAILWAY_HOST="MP Railway (cloud runner)",
               MP_RAILWAY_COMMIT=sh("git", "rev-parse", "HEAD"), PYTHONUNBUFFERED="1")
    bot = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env)
    log("bot started, pid", bot.pid)


def stop_bot():
    if bot and bot.poll() is None:
        bot.send_signal(signal.SIGINT)
        try:
            bot.wait(15)
        except subprocess.TimeoutExpired:
            bot.kill()


def new_commit():
    try:
        remote = sh("git", "ls-remote", "origin", "refs/heads/main").split()[0]
    except Exception:
        return False
    if remote and remote != sh("git", "rev-parse", "HEAD"):
        log("new commit", remote[:7], "- deploying")
        sh("git", "fetch", "-q", "--depth", "1", "origin", "main")
        sh("git", "reset", "-q", "--hard", "FETCH_HEAD")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
        return True
    return False


def dispatch_next():
    """Start the next shift (it waits in the queue until this one ends: same concurrency group)."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/workflows/mp-railway-cloud.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=20)
        log("next shift queued")
        return True
    except Exception as e:
        log("could not queue the next shift:", e)
        return False


def on_signal(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

restore()
start_bot()
queued = False
last_check = time.time()
last_mtime = os.path.getmtime(DB) if os.path.exists(DB) else 0
while not stop and time.time() - started < SHIFT:
    time.sleep(5)
    # 5.00.05: an account change (sign up, /create, /forgotpass, new phone) is on GitHub within ~10 s
    mt = os.path.getmtime(DB) if os.path.exists(DB) else 0
    if mt != last_mtime and time.time() - mt > 5 and time.time() - store.last_save > 30:
        last_mtime = mt
        save("account change")
    if bot.poll() is not None:
        log("bot exited with", bot.returncode, "- restarting in 5 s")
        time.sleep(5)
        start_bot()
    if time.time() - last_check >= 120:
        last_check = time.time()
        save("periodic")
        if new_commit():
            stop_bot(); start_bot()
    if not queued and time.time() - started > SHIFT - 600:
        queued = dispatch_next()

log("shift over" if not stop else "stopping (cancelled)")
stop_bot()
save("end of shift")
if not queued and not stop:
    dispatch_next()
