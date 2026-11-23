"""
FocusLock — a study lock with Claude as the gatekeeper.

Opens the study page in Opera GX, maximises it and, while the timer runs,
brings it back to the front whenever you switch to something else.
On the right there is a timer and a chat with Claude (via `claude -p`, subscription):
to leave early or postpone you have to give a reason — Claude decides.
The stop phrase from config.json releases immediately, without Claude (emergency exit).

  pythonw focus.py run math|vass|java     run a slot from config.json
  python  focus.py run math --minutes 1       quick test
  python  focus.py ask "text"               test Claude's answer without locking
  python  focus.py postpone vass 20:30       move a slot for today only
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
# interface language: strings in the code are English, translations live in locales/<lang>.json
LANG = CONFIG.get("ui_lang", "en")
LANG_NAME = {"en": "English", "ru": "Russian"}.get(LANG, "English")
_LOCALE = ROOT / "locales" / f"{LANG}.json"
_TR = json.loads(_LOCALE.read_text(encoding="utf-8")) if LANG != "en" and _LOCALE.exists() else {}


def tr(text: str, *args) -> str:
    s = _TR.get(text, text)
    return s.format(*args) if args else s


USER = CONFIG.get("user_name", tr("User"))          # how the user is labelled in the chat and in prompts
STYLE = CONFIG.get("chat_style", "In English, casual, brief.")
SALARY_PROFILE = CONFIG.get("salary_profile", "")     # who is job hunting: experience, languages, city — used for salary estimates
LOG = ROOT / "focus.log"

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
for _fn in ("GetForegroundWindow", "GetAncestor"):
    getattr(user32, _fn).restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
for _fn in ("IsWindow", "IsIconic", "IsWindowVisible", "SetForegroundWindow", "BringWindowToTop",
            "GetWindowTextLengthW"):
    getattr(user32, _fn).argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


# ---------------------------------------------------------------- win32
def _title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wt.DWORD(1024)
    kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return Path(buf.value).name.lower()


def browser_windows() -> dict[int, str]:
    exe = Path(CONFIG["browser"]).name.lower()
    out: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _title(hwnd) and _exe(hwnd) == exe:
            out[hwnd] = _title(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def force_foreground(hwnd) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # Alt key press lifts Windows' SetForegroundWindow restriction
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


def screen() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------- Claude
PROMPT = ("You are Claude. You work as the gatekeeper in FocusLock on the user's computer ({user}): they "
          "asked to be \"frozen\" into studying themselves, because they keep putting it off. The current "
          "session is \"{title}\", {state}. Attempts to switch to another window: "
          "{escapes}.\n\nRules that {user} chose: leaving early or postponing only for a reason.\n- \"I don't "
          "want to\", \"I'm bored\", \"later\" are not reasons. Be supportive, gently and with humour; suggest "
          "holding on or doing at least a small piece, but don't let them go.\n- A real reason (feeling "
          "unwell, someone arrived, a call, something urgent, food on the stove): release (unlock) or give a "
          "pause (snooze) if it's a short thing. Health and safety: always release, no questioning.\n- A game "
          "match is running or something needs to be finished quickly: snooze up to 30 minutes, then the "
          "session continues.\n- They ask for more time (\"add 15 minutes\", \"one more exercise\"): "
          "always agree and extend (extend, up to 60 minutes); this works during the session and after it "
          "ended.\n- They say they've already done everything: believe it if it sounds specific, "
          "and release. Praise them.\n- If no reason is given, ask for it. No lecturing. "
          "{style}\n\nConversation in this session:\n{history}\n\nAnswer STRICTLY with a single JSON "
          "object:\n{{\"reply\": \"your answer\", \"action\": \"none\" | \"unlock\" | \"snooze\" | \"extend\", \"minutes\": number for "
          "snooze/extend or 0}}")


def ask_claude(title: str, left: int, total: int, escapes: int, history: list[tuple[str, str]],
               done: bool = False) -> dict:
    hist = "\n".join(f"{who}: {text}" for who, text in history[-16:])
    # once the session is over there is nothing to release or pause — only extend
    state = (f"already finished ({total} min done); unlock/snooze make no sense now, only extend" if done
             else f"{left} min left out of {total}")
    prompt = PROMPT.format(user=USER, style=STYLE, title=title, state=state, escapes=escapes, history=hist)
    exe = Path.home() / ".local" / "bin" / "claude.exe"
    r = subprocess.run([str(exe) if exe.exists() else "claude", "-p", "--output-format", "json", "--tools", "",
                        "--strict-mcp-config", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=180,
                       cwd=str(ROOT), creationflags=0x08000000)  # CREATE_NO_WINDOW
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(out.get("result"))
    m = re.search(r"\{.*\}", out.get("result", ""), re.S)
    if not m:
        return {"reply": out.get("result", "").strip(), "action": "none", "minutes": 0}
    ans = json.loads(m.group(0))
    ans["minutes"] = max(0, min(60 if ans.get("action") == "extend" else 30, int(ans.get("minutes") or 0)))
    return ans


# ---------------------------------------------------------------- JobHunter ("applications" slot)
JOBS_DB = Path(CONFIG.get("jobhunter_db", r"D:\JobHunter\data\jobs.db"))  # JobHunter database, if installed
JOBHUNTER_EMPTY_URL = "https://www.linkedin.com/jobs/"


def ready_jobs(limit: int) -> list[dict]:
    """Prepared applications from the JobHunter queue: best scored, only with a working link."""
    import sqlite3
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, title, company, url, app_dir, score, location, salary, source, "
                           "substr(description, 1, 1500) AS description FROM jobs WHERE status='ready' "
                           "AND url NOT IN ('', 'https://jobtoday.com') ORDER BY score DESC, prepared_at "
                           "LIMIT ?", (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log(f"jobhunter error: {e}")
        return []


APPLY_CACHE = ROOT / "apply_cache.json"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/131.0 Safari/537.36", "Accept-Language": "es-ES,es;q=0.9"}


def _get(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def apply_kind(job: dict) -> tuple[str, str]:
    """(kind, label): 'easy' — apply on the job board itself, 'ext' — redirect to the company site, '?' — unknown."""
    src, url = job.get("source"), job.get("url") or ""
    try:
        if src == "linkedin":
            m = re.search(r"-(\d{6,})/?(?:\?|$)", url)
            h = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}" if m else url)
            if "apply-link-offsite" in h or "offsite-apply" in h:
                return "ext", tr("↗ LinkedIn will redirect to the company site (sign-up)")
            if "apply-link-onsite" in h:
                return "easy", tr("⚡ Solicitud sencilla — right in LinkedIn")
        elif src == "jobtoday":
            h = _get(url)
            d = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S).group(1))
            j = d["props"]["pageProps"].get("job") or {}
            if j.get("isExternalJob"):
                return "ext", tr("↗ partner site (Job Today will redirect)")
            if j.get("easyApply") is not None:
                return "easy", tr("⚡ apply right in Job Today")
        elif src == "telegram":
            return "easy", tr("✉ contact for applying is in the post")
        elif src == "habr":
            return "easy", tr("⚡ apply on Habr Career (login required)")
    except Exception as e:
        log(f"apply_kind {job.get('company')}: {e}")
    return "?", tr("· couldn't tell where applying leads")


def classify_jobs(jobs: list[dict]) -> None:
    """Sets job['_kind'] and job['_how'] (cached) so that quick applications come first."""
    try:
        cache = json.loads(APPLY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for j in jobs:
        if j["id"] not in cache:
            cache[j["id"]] = apply_kind(j)
        j["_kind"], j["_how"] = cache[j["id"]]
    APPLY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


SALARY_CACHE = ROOT / "salary_cache.json"
SALARY_PROMPT = ("Help the user ({user}) fill in the \"desired salary\" field in applications. About them: "
                 "{profile}\n\nFor each job, estimate a realistic desired salary: gross per year in euros, "
                 "taking into account the employer's country/market (Spanish company: Spanish market; remote "
                 "from another country: that market's rates for hiring from Spain; job in rubles: convert "
                 "and say so), the role level and their experience. If the salary is stated in the job ad, "
                 "rely on it. The number is what is reasonable to put in the form: don't lowball, but not so "
                 "high that it gets filtered out.\n\nJobs:\n{jobs}\n\nAnswer STRICTLY with a JSON array in the "
                 "same order, no explanations:\n[{{\"id\": \"...\", \"ask\": 24000, \"range\": \"22–26k\", \"market\": "
                 "\"Spain\", \"note\": \"up to 8 words, in {lang}\"}}]")


def salary_estimates(jobs: list[dict]) -> dict:
    """{job_id: {...}} — from cache; missing ones in a single Claude call (claude -p, subscription)."""
    try:
        cache = json.loads(SALARY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    missing = [j for j in jobs if j["id"] not in cache]
    if missing:
        text = "\n\n".join(f"id: {j['id']}\n{j['title']} — {j['company']} ({j.get('location') or '?'}, source {j.get('source')})\nSalary in the job ad: {j.get('salary') or 'not specified'}\n{(j.get('description') or '')[:1200]}" for j in missing)
        exe = Path.home() / ".local" / "bin" / "claude.exe"
        r = subprocess.run([str(exe) if exe.exists() else "claude", "-p", "--output-format", "json", "--tools", "",
                            "--strict-mcp-config", "--no-session-persistence"],
                           input=SALARY_PROMPT.format(user=USER, profile=SALARY_PROFILE or "not specified", jobs=text, lang=LANG_NAME), capture_output=True, text=True, encoding="utf-8",
                           timeout=300, cwd=str(ROOT), creationflags=0x08000000)
        out = json.loads(r.stdout)
        if out.get("is_error"):
            raise RuntimeError(out.get("result"))
        m = re.search(r"\[.*\]", out.get("result", ""), re.S)
        for item in json.loads(m.group(0)) if m else []:
            if item.get("id"):
                cache[str(item["id"])] = item
        SALARY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return {j["id"]: cache[j["id"]] for j in jobs if j["id"] in cache}


def fmt_salary(e: dict) -> str:
    try:
        ask = f"{int(e['ask']):,}".replace(",", " ")
    except Exception:
        ask = str(e.get("ask", "?"))
    extra = ", ".join(x for x in (e.get("market"), e.get("range")) if x)
    return tr("💶 {0} € gross/year", ask) + (f" ({extra})" if extra else "") + (f" — {e['note']}" if e.get("note") else "")


def mark_applied(job_id: str) -> None:
    import sqlite3
    con = sqlite3.connect(JOBS_DB)
    con.execute("UPDATE jobs SET status='applied', applied_at=? WHERE id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), job_id))
    con.commit()
    con.close()


def remember(text: str) -> None:
    """Session summary goes to Claude's memory so it knows how studying is going."""
    try:
        sys.path.insert(0, CONFIG.get("claude_memory_dir", r"D:\ClaudeMemory"))  # optional Claude long-term memory
        import memory_core as mem
        mem.save(text, kind="fact", project="focus", importance=2)
    except Exception as e:
        log(f"memory error: {e}")


# ---------------------------------------------------------------- one session at a time
# Two slots can overlap (a 30-min pause pushes VASS past 21:00 when Math starts). Two FocusLock
# windows would then fight each other: each one pulls its own study window to the front and treats
# the other as an escape. So only one session may be active; the next one waits for it to finish.
LOCK_PORT = 47612            # a bound port = a session is active (simple mutex, no dependencies)
ACTIVE = ROOT / "active.json"
MAX_WAIT_HOURS = 3


class SessionLock:
    def __init__(self):
        self.sock: socket.socket | None = None

    def acquire(self, slot: str, title: str) -> bool:
        if self.sock:
            return True
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
        except OSError:
            s.close()
            return False
        self.sock = s
        ACTIVE.write_text(json.dumps({"slot": slot, "title": title, "pid": os.getpid()}, ensure_ascii=False),
                          encoding="utf-8")
        return True

    def release(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None
            try:
                ACTIVE.unlink()
            except OSError:
                pass


def active_session() -> dict:
    try:
        return json.loads(ACTIVE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def wait_turn(lock: SessionLock, slot: str) -> str | None:
    """Blocks until no other session is active. Returns the title of the session we waited for
    ("" if none), or None if we should not start (same slot already running, or waited too long)."""
    title = CONFIG["slots"][slot]["title"]
    waited_for = ""
    deadline = time.time() + MAX_WAIT_HOURS * 3600
    while not lock.acquire(slot, title):
        other = active_session()
        if other.get("slot") == slot:
            log(f"{slot}: already running, skipping the duplicate")
            return None
        if not waited_for:
            waited_for = other.get("title") or "?"
            log(f"{slot}: waiting for «{waited_for}» to finish")
        if time.time() > deadline:
            log(f"{slot}: waited {MAX_WAIT_HOURS} h for «{waited_for}», giving up")
            return None
        time.sleep(15)
    return waited_for


# ---------------------------------------------------------------- session UI
SHELL_EXES = {"startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
              "shellhost.exe"}
BG, FG, ACC, DIM ="#16161d", "#e8e8ef", "#ff4f8b", "#8a8a99"


class Session:
    def __init__(self, slot: str, minutes: int | None = None, lock: SessionLock | None = None,
                 waited_for: str = ""):
        s = CONFIG["slots"][slot]
        self.lock = lock or SessionLock()
        self.slot, self.title = slot, s["title"]
        self.jobs = []
        if s.get("jobhunter"):
            limit = s.get("jobhunter_limit", 5)
            pool = ready_jobs(limit * 3)
            classify_jobs(pool)
            order = {"easy": 0, "?": 1, "ext": 2}  # quick-apply jobs first
            pool.sort(key=lambda j: (order.get(j["_kind"], 1), -(j.get("score") or 0)))
            self.jobs = pool[:limit]
        self.urls = [j["url"] for j in self.jobs] or s.get("urls") or [s.get("url") or JOBHUNTER_EMPTY_URL]
        self.keywords = [k.lower() for k in s.get("keywords", [])]
        self.apps = [a.lower() for a in s.get("allowed_apps", [])]  # programs that also count as studying (IDE)
        # whole words only: otherwise «vk» is found inside the course player's random sesskey=BAlVkiu6hN
        blocked = CONFIG.get("blocked_sites", [])
        self.blocked = re.compile(r"\b(?:" + "|".join(map(re.escape, blocked)) + r")\b", re.I) if blocked else None
        self.pre_windows: set = set()
        self.logged_titles: set = set()
        self.delayed = False
        self.total = minutes or s["minutes"]
        self.started = time.time()
        self.grace_until = self.started + (CONFIG.get("grace_seconds", 60) if not minutes or minutes > 2 else 5)
        self.end = self.grace_until + self.total * 60
        self.snooze_until = 0.0
        self.escapes = 0
        self.history: list[tuple[str, str]] = []
        self.done = False
        self.outcome = ""
        self.study = None
        self.last_offtab_note = 0.0
        self.busy = False

        self.root = tk.Tk()
        self.root.title("FocusLock")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        W, H = screen()
        cw = CONFIG.get("chat_width", 460)
        self.root.geometry(f"{cw}x{H - 80}+{W - cw}+0")
        self.W, self.H, self.cw = W, H, cw
        self._build()
        self.open_study()
        if waited_for:
            self.say("sys", tr("«{0}» has finished — now it's this session's turn.", waited_for))
        self.say("Claude", tr("Starting «{1}» for {2} min in {0} s. Save your game, make some tea. Need more "
                              "time? Use the «Delay start» slider (once, before the start). To leave early, "
                              "write the reason here.", int(self.grace_until - time.time()), self.title, self.total))
        self.tick()

    def _build(self):
        f = ("Segoe UI", 11)
        tk.Label(self.root, text=self.title, bg=BG, fg=FG, font=("Segoe UI Semibold", 14), wraplength=self.cw - 30
                 ).pack(pady=(18, 0))
        self.timer = tk.Label(self.root, text="", bg=BG, fg=ACC, font=("Consolas", 40, "bold"))
        self.timer.pack(pady=(6, 0))
        self.status = tk.Label(self.root, text="", bg=BG, fg=DIM, font=("Segoe UI", 10))
        self.status.pack()
        self.chat = tk.Text(self.root, bg="#1f1f29", fg=FG, font=f, wrap="word", relief="flat", padx=12, pady=10,
                            state="disabled", height=10)
        self.chat.tag_configure("me", foreground="#9ad1ff")
        self.chat.tag_configure("claude", foreground=FG)
        self.chat.tag_configure("sys", foreground=DIM)
        self.chat.pack(fill="both", expand=True, padx=14, pady=12)
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=14, pady=(0, 16))
        self.entry = tk.Entry(row, bg="#26263a", fg=FG, insertbackground=FG, relief="flat", font=f)
        self.entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.entry.bind("<Return>", lambda e: self.send())
        self.btn = tk.Button(row, text="➤", command=self.send, bg=ACC, fg="white", relief="flat", font=f, width=3)
        self.btn.pack(side="left", padx=(8, 0))
        # "Delay start": once, only before the session starts, no reason needed
        self.delay_row = tk.Frame(self.root, bg=BG)
        self.delay_row.pack(fill="x", padx=14, pady=(0, 14), before=self.chat)
        tk.Label(self.delay_row, text=tr("Delay start, min:"), bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(side="left")
        self.delay_scale = tk.Scale(self.delay_row, from_=10, to=60, resolution=10, orient="horizontal",
                                    bg=BG, fg=FG, troughcolor="#26263a", highlightthickness=0, length=150)
        self.delay_scale.pack(side="left", padx=6)
        tk.Button(self.delay_row, text=tr("Delay"), command=self.delay_start, bg="#26263a", fg=FG,
                  relief="flat", font=("Segoe UI", 10)).pack(side="left")
        if self.jobs:
            self._build_jobs()
        self.close_btn = tk.Button(self.root, text=tr("Close"), command=self.root.destroy, bg="#26263a", fg=FG,
                                   relief="flat", font=f)

    def _build_jobs(self):
        """Applications list: copy the letter, open the PDF folder, mark as sent."""
        self.sent = 0
        box = tk.Frame(self.root, bg=BG)
        box.pack(fill="x", padx=14, pady=(0, 6), before=self.chat)
        small = ("Segoe UI", 9)
        self._build_profile(box, small)
        for j in self.jobs:
            row = tk.Frame(box, bg="#1f1f29")
            row.pack(fill="x", pady=2)
            lbl = tk.Label(row, text=f"{j['company'][:22]} — {j['title'][:40]}", bg="#1f1f29", fg=FG, font=small,
                           anchor="w", wraplength=self.cw - 40, justify="left")
            lbl.pack(fill="x", padx=6, pady=(4, 0))
            tk.Label(row, text=j.get("_how", ""), bg="#1f1f29", font=small, anchor="w",
                     fg={"easy": "#4fd18b", "ext": "#ff9a6b"}.get(j.get("_kind"), DIM)).pack(fill="x", padx=6)
            j["_sal"] = tk.Label(row, text=tr("💶 estimating salary…"), bg="#1f1f29", fg=DIM, font=small, anchor="w",
                                 wraplength=self.cw - 40, justify="left")
            j["_sal"].pack(fill="x", padx=6)
            j["_sal"].bind("<Button-1>", lambda e, j=j: self.copy_salary(j))
            btns = tk.Frame(row, bg="#1f1f29")
            btns.pack(fill="x", padx=6, pady=(2, 4))
            mk = lambda text, cmd: tk.Button(btns, text=text, command=cmd, bg="#26263a", fg=FG, relief="flat",
                                             font=small).pack(side="left", padx=(0, 6))
            mk(tr("📋 Letter"), lambda j=j: self.copy_letter(j))
            mk("📁 PDF", lambda j=j: self.open_dir(j))
            mk(tr("✓ Sent"), lambda j=j, lbl=lbl, row=row: self.applied(j, lbl, row))
        threading.Thread(target=self._load_salaries, daemon=True).start()

    def _load_salaries(self):
        try:
            est = salary_estimates(self.jobs)
        except Exception as e:
            log(f"salary error: {e}")
            est = {}
        self.root.after(0, self._show_salaries, est)

    def _show_salaries(self, est: dict):
        for j in self.jobs:
            e = est.get(j["id"])
            j["_ask"] = e.get("ask") if e else None
            if j["_sal"].winfo_exists():
                j["_sal"].config(text=fmt_salary(e) + tr("  (click to copy)") if e else tr("💶 couldn't "
                                                                                            "estimate"),
                                 fg="#f5c66b" if e else DIM, cursor="hand2" if e else "")

    def copy_salary(self, j: dict):
        if j.get("_ask"):
            self.root.clipboard_clear()
            self.root.clipboard_append(str(j["_ask"]))
            self.say("sys", tr("Copied {0} for {1}.", j['_ask'], j['company']))

    def _build_profile(self, parent, small):
        """"🪪 Profile" button: expands fields from profile.json; clicking a field copies its value."""
        try:
            fields = [(k, v) for k, v in json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))["fields"] if v]
        except Exception as e:
            log(f"profile error: {e}")
            return
        panel = tk.Frame(parent, bg="#1f1f29")
        for k, v in fields:
            lbl = tk.Label(panel, text=f"{k}: {v}", bg="#1f1f29", fg=FG, font=small, anchor="w", cursor="hand2",
                           wraplength=self.cw - 40, justify="left")
            lbl.pack(fill="x", padx=6, pady=1)
            lbl.bind("<Button-1>", lambda e, k=k, v=v: self._copy(v, tr("{0} copied.", k)))

        def toggle():
            if panel.winfo_ismapped():
                panel.pack_forget()
                btn.config(text=tr("🪪 Profile ▸"))
            else:
                panel.pack(fill="x", pady=(0, 6), after=btn)
                btn.config(text=tr("🪪 Profile ▾ (click a field to copy)"))

        btn = tk.Button(parent, text=tr("🪪 Profile ▸"), command=toggle, bg="#26263a", fg=FG, relief="flat", font=small,
                        anchor="w")
        btn.pack(fill="x", pady=(0, 4))

    def _copy(self, text: str, note: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.say("sys", note)

    def copy_letter(self, j: dict):
        try:
            text = (Path(j["app_dir"]) / "carta.txt").read_text(encoding="utf-8")
        except Exception:
            self.say("sys", tr("No letter found for this job."))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.say("sys", tr("Letter for {0} copied — paste it with Ctrl+V.", j['company']))

    def open_dir(self, j: dict):
        if j.get("app_dir") and Path(j["app_dir"]).exists():
            os.startfile(j["app_dir"])  # File Explorer is allowed in this slot (allowed_apps)

    def applied(self, j: dict, lbl, row):
        if j.get("_sent"):
            return
        try:
            mark_applied(j["id"])
        except Exception as e:
            self.say("sys", tr("Couldn't mark it: {0}", e))
            return
        j["_sent"] = True
        self.sent += 1
        lbl.config(fg="#4fd18b", text="✓ " + lbl.cget("text"))
        for w in row.winfo_children():
            if isinstance(w, tk.Frame):
                w.destroy()
        log(tr("{0}: application sent {1} — {2}", self.slot, j['company'], j['title']))
        if self.sent == len(self.jobs):
            self.finish(tr("all {0} applications sent in {1} min.", self.sent, self.elapsed()),
                        tr("All {0} sent! That's what I'm talking about. You're free 💪", self.sent))
        else:
            self.say("Claude", tr("Done! {0} of {1}.", self.sent, len(self.jobs)))

    def say(self, who: str, text: str):
        tag = {USER: "me", "Claude": "claude"}.get(who, "sys")
        self.chat.configure(state="normal")
        self.chat.insert("end", (f"{who}: " if tag != "sys" else "") + text + "\n\n", tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")
        if tag != "sys":
            self.history.append((who, text))

    def delay_start(self):
        if self.delayed or self.done or time.time() >= self.grace_until:
            return
        mins = int(self.delay_scale.get())
        self.delayed = True
        self.grace_until = time.time() + mins * 60
        self.end = self.grace_until + self.total * 60
        self.delay_row.pack_forget()
        self.say("sys", tr("Start delayed by {0} min — until {1}. The window will minimise and come back by "
                           "itself.", mins, time.strftime('%H:%M', time.localtime(self.grace_until))))
        log(f"{self.slot} delayed {mins}")
        self.root.attributes("-topmost", False)
        self.root.after(1500, self.root.iconify)

    def resume_after_delay(self):
        self.delayed = False
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if not self.study or not user32.IsWindow(self.study):
            self.study = None
            self.open_study()
        self.place_study()
        force_foreground(self.study)
        self.root.lift()
        self.say("Claude", tr("Time's up — starting «{0}», {1} min.", self.title, self.total))

    # ---------- browser
    def open_study(self):
        before = set(browser_windows())
        if not self.pre_windows:
            self.pre_windows = before  # windows that were open before the session (could be anything)
        subprocess.Popen([CONFIG["browser"], "--new-window", *self.urls])
        for _ in range(40):
            time.sleep(0.25)
            new = [h for h in browser_windows() if h not in before]
            if new:
                self.study = new[0]
                break
        if not self.study:  # Opera may have opened the tab in an existing window
            wins = browser_windows()
            self.study = next((h for h, t in wins.items() if self.on_topic(t)), None) or next(iter(wins), None)
        self.place_study()

    def place_study(self):
        if self.study and user32.IsWindow(self.study):
            user32.ShowWindow(self.study, 9)
            user32.MoveWindow(self.study, 0, 0, self.W - self.cw, self.H - 40, True)

    def on_topic(self, title: str) -> bool:
        t = title.lower()
        return not self.keywords or any(k in t for k in self.keywords)

    def is_helper(self, root) -> bool:
        """A window that also counts as studying: an allowed program (IDE), an Opera window with a site from
        keywords, or a new Opera window opened during the session (course pop-up player etc.),
        unless it shows a blocked site."""
        if not root:
            return False
        exe = _exe(root)
        if self.apps and exe in self.apps:
            return True
        # Taskbar, desktop, Start menu, search: used to launch the IDE — not an escape.
        # File Explorer windows with a title (folders) still count.
        if exe in SHELL_EXES or (exe == "explorer.exe" and not _title(root)):
            return True
        wins = browser_windows()
        if root not in wins:
            return False
        title = wins[root].lower()
        if self.blocked and self.blocked.search(title):
            return False
        return self.on_topic(title) or root not in self.pre_windows

    # ---------- loop
    def locked(self) -> bool:
        now = time.time()
        return not self.done and now >= self.grace_until and now >= self.snooze_until

    def tick(self):
        now = time.time()
        if not self.done:
            if self.delayed and now >= self.grace_until:
                self.resume_after_delay()
            if now >= self.grace_until and self.delay_row.winfo_ismapped():
                self.delay_row.pack_forget()  # no more delaying once the session has started
            if now < self.grace_until:
                self.timer.config(text="-" + self.fmt(self.grace_until - now))
                self.status.config(text=tr("delayed, starting soon") if self.delayed else tr("starting soon"))
            elif now < self.snooze_until:
                self.timer.config(text=self.fmt(self.snooze_until - now))
                self.status.config(text=tr("paused, will continue"))
            elif now >= self.end:
                self.finish(tr("completed in full"), tr("Done! {0} minutes of «{1}» behind you. Proud of you 💪", self.total, self.title))
            else:
                self.timer.config(text=self.fmt(self.end - now))
                self.status.config(text=tr("focus · escape attempts: {0}", self.escapes))
            if self.locked():
                self.guard(now)
        self.root.after(1000, self.tick)

    def guard(self, now: float):
        if not self.study or not user32.IsWindow(self.study):
            self.say("sys", tr("The study window closed — opening it again."))
            self.study = None
            self.open_study()
            return
        fg = user32.GetForegroundWindow()
        me = int(self.root.wm_frame(), 16)
        root = user32.GetAncestor(fg, 2) if fg else None
        if root not in (self.study, me) and self.is_helper(root):
            pass  # IDE, Mathway, course player — also studying
        elif fg not in (self.study, me) and root not in (self.study, me):
            self.escapes += 1
            t = f"{_exe(root) if root else '?'} | {_title(root)[:80] if root else ''}"
            if t not in self.logged_titles:  # log exactly what was counted as an escape
                self.logged_titles.add(t)
                log(f"{self.slot} escape -> {t}")
            self.place_study()
            force_foreground(self.study)
            self.root.lift()
            if self.escapes in (1, 5, 15) or self.escapes % 30 == 0:
                self.say("sys", tr("Back to studying (attempt #{0}). Need to leave? Write the reason.", self.escapes))
        elif now - self.last_offtab_note > 90 and not self.on_topic(_title(self.study)):
            self.last_offtab_note = now
            self.say("sys", tr("Looks like the wrong tab is open: «{0}». Go back to «{1}».", _title(self.study)[:60], self.title))

    @staticmethod
    def fmt(sec: float) -> str:
        sec = max(0, int(sec))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    # ---------- chat
    def send(self):
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self.say(USER, text)
        if text.lower().strip(" .!") == CONFIG["stop_phrase"].lower():
            self.finish(tr("stopped with the stop phrase after {0} min.", self.elapsed()), tr("Emergency stop "
                                                                                   "triggered, releasing you."))
            return
        self.busy = True
        self.btn.config(text="…", state="disabled")
        self.say("sys", tr("Claude is thinking…"))
        left = max(0, int((self.end - time.time()) / 60))
        threading.Thread(target=self._ask, args=(left,), daemon=True).start()

    def _ask(self, left: int):
        try:
            ans = ask_claude(self.title, left, self.total, self.escapes, self.history, self.done)
        except Exception as e:
            log(f"claude error: {e}")
            ans = {"reply": tr("Can't reach myself right now (Claude failed to start). If you really need to "
                               "leave, use the stop phrase «{0}».", CONFIG['stop_phrase']),
                   "action": "none", "minutes": 0}
        self.root.after(0, self._apply, ans)

    def _apply(self, ans: dict):
        self.busy = False
        self.btn.config(text="➤", state="normal")
        self.say("Claude", ans.get("reply", ""))
        act = ans.get("action")
        if act == "extend" and ans.get("minutes"):
            self.extend(ans["minutes"])
        elif self.done:
            pass  # the session is already over: nothing to release or pause
        elif act == "unlock":
            reason = next((t for w, t in reversed(self.history) if w == USER), "")
            self.finish(tr("released by Claude after {0} min, reason: «{1}»", self.elapsed(), reason[:200]), None)
        elif act == "snooze" and ans.get("minutes"):
            mins = ans["minutes"]
            now = time.time()
            new_until = now + mins * 60
            # a pause doesn't eat study time; a second pause on top of the current one only adds the difference
            self.end += max(0.0, new_until - max(now, self.snooze_until))
            self.snooze_until = max(self.snooze_until, new_until)
            self.say("sys", tr("Pause {0} min. Then we continue.", mins))
            log(f"{self.slot} snooze {mins}")

    def extend(self, mins: int):
        """Add minutes; if the session has already ended, restart it for these minutes."""
        now = time.time()
        if self.done and not self.lock.acquire(self.slot, self.title):
            other = active_session().get("title") or "?"
            self.say("sys", tr("Can't extend: «{0}» has already started. Finish it first.", other))
            return
        self.total += mins
        if self.done:
            self.done = False
            self.end = max(now, self.snooze_until) + mins * 60
            self.close_btn.pack_forget()
            self.root.attributes("-topmost", True)
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            if not self.study or not user32.IsWindow(self.study):
                self.study = None
                self.open_study()
            self.place_study()
        else:
            self.end += mins * 60
        self.say("sys", tr("+{0} min. Session ends at {1}.", mins, time.strftime('%H:%M', time.localtime(self.end))))
        log(f"{self.slot} extend {mins}")

    def elapsed(self) -> int:
        return max(0, int((time.time() - self.grace_until) / 60))

    def finish(self, outcome: str, message: str | None):
        if self.done:
            return
        self.done = True
        self.lock.release()          # the next scheduled session may start now
        self.outcome = outcome
        if message:
            self.say("Claude", message)
        self.timer.config(text="✓", fg="#4fd18b")
        self.status.config(text=tr("free"))
        self.close_btn.pack(pady=(0, 16))
        self.root.attributes("-topmost", False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        line = (tr("Focus session «{0}» {1}: {2}; planned {3} min, switch attempts: {4}.", self.title, time.strftime('%Y-%m-%d %H:%M'), outcome, self.total, self.escapes)
                + (tr(" Applications sent: {0} of {1}.", self.sent, len(self.jobs)) if self.jobs else ""))
        log(line)
        remember(line)

    def run(self):
        self.root.mainloop()
        self.lock.release()
        if not self.done:
            log(tr("{0}: window closed before finishing", self.slot))


POSTPONE = ROOT / "postpone.json"


def wait_postponed(slot: str) -> None:
    """One-off move for today: postpone.json {"YYYY-MM-DD": {"slot": "HH:MM"}}.
    The scheduled task starts at the usual time and the session waits until the new time."""
    try:
        data = json.loads(POSTPONE.read_text(encoding="utf-8"))
    except Exception:
        return
    hhmm = data.get(time.strftime("%Y-%m-%d"), {}).get(slot)
    if not hhmm:
        return
    h, mi = map(int, hhmm.split(":"))
    t = time.localtime()
    target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
    if target > time.time():
        log(tr("{0}: moved to {1}, waiting", slot, hhmm))
        time.sleep(target - time.time())


def postpone(slot: str, hhmm: str) -> None:
    try:
        data = json.loads(POSTPONE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    today = time.strftime("%Y-%m-%d")
    data = {d: v for d, v in data.items() if d >= today}  # drop past dates
    data.setdefault(today, {})[slot] = hhmm
    POSTPONE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(tr("{0}: today at {1}", slot, hhmm))


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "postpone":
        postpone(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "ask":
        print(json.dumps(ask_claude("Test", 30, 40, 2, [(USER, " ".join(sys.argv[2:]))]),
                         ensure_ascii=False, indent=1))
        return
    if len(sys.argv) < 3 or sys.argv[1] != "run" or sys.argv[2] not in CONFIG["slots"]:
        print(__doc__)
        return
    minutes = int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else None
    wait_postponed(sys.argv[2])
    if CONFIG["slots"][sys.argv[2]].get("jobhunter") and not ready_jobs(1):
        log(tr("{0}: no ready applications in the JobHunter queue — skipping the session", sys.argv[2]))
        return
    lock = SessionLock()
    waited_for = wait_turn(lock, sys.argv[2])
    if waited_for is None:
        return
    log(f"start {sys.argv[2]}")
    Session(sys.argv[2], minutes, lock, waited_for).run()


if __name__ == "__main__":
    main()
