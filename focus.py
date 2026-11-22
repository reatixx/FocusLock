"""
FocusLock — «заморозка» на учёбу с Claude-привратником.

Открывает страницу учёбы в Opera GX, растягивает её на экран и, пока идёт таймер,
возвращает её на передний план, если переключиться на что-то другое.
Справа окно с таймером и чатом с Claude (через `claude -p`, по подписке):
чтобы выйти раньше или отложить, нужно назвать причину — Claude решает.
Стоп-фраза из config.json отпускает сразу и без Claude (аварийный выход).

  pythonw focus.py run math|vass|java     запустить слот из config.json
  python  focus.py run math --minutes 1       короткий тест
  python  focus.py ask "текст"               проверить ответ Claude без блокировки
  python  focus.py postpone vass 20:30       перенести слот только на сегодня
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
USER = CONFIG.get("user_name", "Пользователь")          # как подписан пользователь в чате и промптах
STYLE = CONFIG.get("chat_style", "По-русски, на «ты», коротко.")
SALARY_PROFILE = CONFIG.get("salary_profile", "")     # кто ищет работу: опыт, языки, город — для оценки зарплаты
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
    user32.keybd_event(0x12, 0, 0, 0)  # Alt: снимает запрет Windows на SetForegroundWindow
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


def screen() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------- Claude
PROMPT = """Ты — Claude. Ты работаешь привратником в FocusLock на компьютере пользователя ({user}): он сам попросил, \
чтобы его «замораживали» на учёбу, потому что постоянно откладывает. Сейчас идёт сессия «{title}», \
осталось {left} мин. из {total}. Попыток переключиться на другое окно: {escapes}.

Правила, которые выбрал сам {user}: отпускать раньше или откладывать — только по причине.
- «Не хочу», «надоело», «потом» — не причина. Мягко и с юмором поддержи, предложи дотерпеть или \
сделать хотя бы маленький кусок, но не отпускай.
- Реальная причина (плохо себя чувствует, кто-то пришёл, звонок, срочное дело, еда на плите) — отпусти \
(unlock) или дай паузу (snooze), если дело короткое. Здоровье и безопасность — отпускай всегда, без допроса.
- Идёт матч в игре или нужно что-то быстро доделать — snooze до 30 минут, потом сессия продолжится.
- Говорит, что всё уже сделал, — поверь, если звучит конкретно, и отпусти. Похвали.
- Если причина не названа, спроси её. Не читай нотаций. {style}

Переписка в этой сессии:
{history}

Ответь СТРОГО одним JSON-объектом:
{{"reply": "твой ответ", "action": "none" | "unlock" | "snooze", "minutes": число для snooze или 0}}"""


def ask_claude(title: str, left: int, total: int, escapes: int, history: list[tuple[str, str]]) -> dict:
    hist = "\n".join(f"{who}: {text}" for who, text in history[-16:])
    prompt = PROMPT.format(user=USER, style=STYLE, title=title, left=left, total=total, escapes=escapes, history=hist)
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
    ans["minutes"] = max(0, min(30, int(ans.get("minutes") or 0)))
    return ans


# ---------------------------------------------------------------- JobHunter (слот «Отклики»)
JOBS_DB = Path(CONFIG.get("jobhunter_db", r"D:\JobHunter\data\jobs.db"))  # база JobHunter, если он установлен
JOBHUNTER_EMPTY_URL = "https://www.linkedin.com/jobs/"


def ready_jobs(limit: int) -> list[dict]:
    """Готовые отклики из очереди JobHunter: лучшие по оценке, только с рабочей ссылкой."""
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
    """(вид, подпись): 'easy' — отклик прямо на площадке, 'ext' — уход на сайт компании, '?' — неясно."""
    src, url = job.get("source"), job.get("url") or ""
    try:
        if src == "linkedin":
            m = re.search(r"-(\d{6,})/?(?:\?|$)", url)
            h = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}" if m else url)
            if "apply-link-offsite" in h or "offsite-apply" in h:
                return "ext", "↗ LinkedIn отправит на сайт компании (регистрация)"
            if "apply-link-onsite" in h:
                return "easy", "⚡ Solicitud sencilla — прямо в LinkedIn"
        elif src == "jobtoday":
            h = _get(url)
            d = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S).group(1))
            j = d["props"]["pageProps"].get("job") or {}
            if j.get("isExternalJob"):
                return "ext", "↗ партнёрский сайт (Job Today перекинет)"
            if j.get("easyApply") is not None:
                return "easy", "⚡ отклик прямо в Job Today"
        elif src == "telegram":
            return "easy", "✉ контакт для отклика — в посте"
        elif src == "habr":
            return "easy", "⚡ отклик на Хабр Карьере (нужен вход)"
    except Exception as e:
        log(f"apply_kind {job.get('company')}: {e}")
    return "?", "· не понял, куда ведёт отклик"


def classify_jobs(jobs: list[dict]) -> None:
    """Проставляет job['_kind'], job['_how'] (с кэшем) — чтобы простые отклики шли первыми."""
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
SALARY_PROMPT = """Помоги пользователю ({user}) заполнить поле «желаемая зарплата» в откликах. О нём: {profile}

Для каждой вакансии оцени реалистичную желаемую зарплату: брутто за год в евро, с учётом страны/рынка \
работодателя (испанская компания — испанский рынок; удалёнка из другой страны — её рынок для найма из Испании; \
вакансия в рублях — пересчитай и скажи об этом), уровня роли и его опыта. Если зарплата указана в вакансии — \
опирайся на неё. Число — то, что разумно вписать в форму: не занижай, но и не так, чтобы отсеяли.

Вакансии:
{jobs}

Ответь СТРОГО JSON-массивом в том же порядке, без пояснений:
[{{"id": "...", "ask": 24000, "range": "22–26k", "market": "Испания", "note": "до 8 слов, по-русски"}}]"""


def salary_estimates(jobs: list[dict]) -> dict:
    """{job_id: {...}} — из кэша, недостающие одним запросом к Claude (claude -p, подписка)."""
    try:
        cache = json.loads(SALARY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    missing = [j for j in jobs if j["id"] not in cache]
    if missing:
        text = "\n\n".join(f"id: {j['id']}\n{j['title']} — {j['company']} ({j.get('location') or '?'}, "
                           f"источник {j.get('source')})\nЗарплата в вакансии: {j.get('salary') or 'не указана'}\n"
                           f"{(j.get('description') or '')[:1200]}" for j in missing)
        exe = Path.home() / ".local" / "bin" / "claude.exe"
        r = subprocess.run([str(exe) if exe.exists() else "claude", "-p", "--output-format", "json", "--tools", "",
                            "--strict-mcp-config", "--no-session-persistence"],
                           input=SALARY_PROMPT.format(user=USER, profile=SALARY_PROFILE or "не указано", jobs=text), capture_output=True, text=True, encoding="utf-8",
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
    return f"💶 {ask} € брутто/год" + (f" ({extra})" if extra else "") + (f" — {e['note']}" if e.get("note") else "")


def mark_applied(job_id: str) -> None:
    import sqlite3
    con = sqlite3.connect(JOBS_DB)
    con.execute("UPDATE jobs SET status='applied', applied_at=? WHERE id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), job_id))
    con.commit()
    con.close()


def remember(text: str) -> None:
    """Итог сессии — в память Claude, чтобы он знал, как идёт учёба."""
    try:
        sys.path.insert(0, CONFIG.get("claude_memory_dir", r"D:\ClaudeMemory"))  # необязательная память Claude
        import memory_core as mem
        mem.save(text, kind="fact", project="focus", importance=2)
    except Exception as e:
        log(f"memory error: {e}")


# ---------------------------------------------------------------- session UI
SHELL_EXES = {"startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
              "shellhost.exe"}
BG, FG, ACC, DIM ="#16161d", "#e8e8ef", "#ff4f8b", "#8a8a99"


class Session:
    def __init__(self, slot: str, minutes: int | None = None):
        s = CONFIG["slots"][slot]
        self.slot, self.title = slot, s["title"]
        self.jobs = []
        if s.get("jobhunter"):
            limit = s.get("jobhunter_limit", 5)
            pool = ready_jobs(limit * 3)
            classify_jobs(pool)
            order = {"easy": 0, "?": 1, "ext": 2}  # сначала то, что отправляется в пару кликов
            pool.sort(key=lambda j: (order.get(j["_kind"], 1), -(j.get("score") or 0)))
            self.jobs = pool[:limit]
        self.urls = [j["url"] for j in self.jobs] or s.get("urls") or [s.get("url") or JOBHUNTER_EMPTY_URL]
        self.keywords = [k.lower() for k in s.get("keywords", [])]
        self.apps = [a.lower() for a in s.get("allowed_apps", [])]  # программы, которые тоже учёба (IDE)
        self.blocked = [b.lower() for b in CONFIG.get("blocked_sites", [])]
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
        self.say("Claude", f"Через {int(self.grace_until - time.time())} с начинаем «{self.title}» на {self.total} мин. "
                           f"Сохрани игру, налей чай. Нужно больше времени — ползунок «Отложить начало» "
                           f"(один раз, до старта). Выйти раньше — напиши сюда причину.")
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
        # «Отложить начало» — один раз и только до старта, без объяснений
        self.delay_row = tk.Frame(self.root, bg=BG)
        self.delay_row.pack(fill="x", padx=14, pady=(0, 14), before=self.chat)
        tk.Label(self.delay_row, text="Отложить начало, мин:", bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(side="left")
        self.delay_scale = tk.Scale(self.delay_row, from_=10, to=60, resolution=10, orient="horizontal",
                                    bg=BG, fg=FG, troughcolor="#26263a", highlightthickness=0, length=150)
        self.delay_scale.pack(side="left", padx=6)
        tk.Button(self.delay_row, text="Отложить", command=self.delay_start, bg="#26263a", fg=FG,
                  relief="flat", font=("Segoe UI", 10)).pack(side="left")
        if self.jobs:
            self._build_jobs()
        self.close_btn = tk.Button(self.root, text="Закрыть", command=self.root.destroy, bg="#26263a", fg=FG,
                                   relief="flat", font=f)

    def _build_jobs(self):
        """Список откликов: скопировать письмо, открыть папку с PDF, отметить отправку."""
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
            j["_sal"] = tk.Label(row, text="💶 считаю зарплату…", bg="#1f1f29", fg=DIM, font=small, anchor="w",
                                 wraplength=self.cw - 40, justify="left")
            j["_sal"].pack(fill="x", padx=6)
            j["_sal"].bind("<Button-1>", lambda e, j=j: self.copy_salary(j))
            btns = tk.Frame(row, bg="#1f1f29")
            btns.pack(fill="x", padx=6, pady=(2, 4))
            mk = lambda text, cmd: tk.Button(btns, text=text, command=cmd, bg="#26263a", fg=FG, relief="flat",
                                             font=small).pack(side="left", padx=(0, 6))
            mk("📋 Письмо", lambda j=j: self.copy_letter(j))
            mk("📁 PDF", lambda j=j: self.open_dir(j))
            mk("✓ Отправил", lambda j=j, lbl=lbl, row=row: self.applied(j, lbl, row))
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
                j["_sal"].config(text=fmt_salary(e) + "  (клик — скопировать)" if e else "💶 оценить не вышло",
                                 fg="#f5c66b" if e else DIM, cursor="hand2" if e else "")

    def copy_salary(self, j: dict):
        if j.get("_ask"):
            self.root.clipboard_clear()
            self.root.clipboard_append(str(j["_ask"]))
            self.say("sys", f"Скопировал {j['_ask']} для {j['company']}.")

    def _build_profile(self, parent, small):
        """Кнопка «🪪 Анкета»: раскрывает поля из profile.json, клик по полю — копирует значение."""
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
            lbl.bind("<Button-1>", lambda e, k=k, v=v: self._copy(v, f"{k} скопировано."))

        def toggle():
            if panel.winfo_ismapped():
                panel.pack_forget()
                btn.config(text="🪪 Анкета ▸")
            else:
                panel.pack(fill="x", pady=(0, 6), after=btn)
                btn.config(text="🪪 Анкета ▾ (клик по полю — копировать)")

        btn = tk.Button(parent, text="🪪 Анкета ▸", command=toggle, bg="#26263a", fg=FG, relief="flat", font=small,
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
            self.say("sys", "Письма для этой вакансии не нашлось.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.say("sys", f"Письмо для {j['company']} скопировано — вставляй Ctrl+V.")

    def open_dir(self, j: dict):
        if j.get("app_dir") and Path(j["app_dir"]).exists():
            os.startfile(j["app_dir"])  # Проводник в этом слоте разрешён (allowed_apps)

    def applied(self, j: dict, lbl, row):
        if j.get("_sent"):
            return
        try:
            mark_applied(j["id"])
        except Exception as e:
            self.say("sys", f"Не смог отметить: {e}")
            return
        j["_sent"] = True
        self.sent += 1
        lbl.config(fg="#4fd18b", text="✓ " + lbl.cget("text"))
        for w in row.winfo_children():
            if isinstance(w, tk.Frame):
                w.destroy()
        log(f"{self.slot}: отправлен отклик {j['company']} — {j['title']}")
        if self.sent == len(self.jobs):
            self.finish(f"все {self.sent} откликов отправлены через {self.elapsed()} мин.",
                        f"Все {self.sent} отправлены! Вот это я понимаю. Свободен 💪")
        else:
            self.say("Claude", f"Есть! {self.sent} из {len(self.jobs)}.")

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
        self.say("sys", f"Начало отложено на {mins} мин. — в {time.strftime('%H:%M', time.localtime(self.grace_until))}. "
                        f"Окно свернётся и вернётся само.")
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
        self.say("Claude", f"Время вышло — начинаем «{self.title}», {self.total} мин.")

    # ---------- browser
    def open_study(self):
        before = set(browser_windows())
        if not self.pre_windows:
            self.pre_windows = before  # окна, открытые до сессии (там может быть что угодно)
        subprocess.Popen([CONFIG["browser"], "--new-window", *self.urls])
        for _ in range(40):
            time.sleep(0.25)
            new = [h for h in browser_windows() if h not in before]
            if new:
                self.study = new[0]
                break
        if not self.study:  # Opera могла открыть вкладку в существующем окне
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
        """Окно, которое тоже считается учёбой: разрешённая программа (IDE), окно Opera с сайтом из
        keywords, или новое окно Opera, открытое во время сессии (всплывающий плеер курса и т.п.),
        если в нём не открыт заблокированный сайт."""
        if not root:
            return False
        exe = _exe(root)
        if self.apps and exe in self.apps:
            return True
        # Панель задач, рабочий стол, «Пуск», поиск: через них запускают IDE — это не побег.
        # Окна Проводника с заголовком (папки) по-прежнему считаются.
        if exe in SHELL_EXES or (exe == "explorer.exe" and not _title(root)):
            return True
        wins = browser_windows()
        if root not in wins:
            return False
        title = wins[root].lower()
        if any(b in title for b in self.blocked):
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
                self.delay_row.pack_forget()  # после старта откладывать уже нельзя
            if now < self.grace_until:
                self.timer.config(text="-" + self.fmt(self.grace_until - now))
                self.status.config(text="отложено, скоро начнём" if self.delayed else "скоро начнём")
            elif now < self.snooze_until:
                self.timer.config(text=self.fmt(self.snooze_until - now))
                self.status.config(text="пауза, потом продолжим")
            elif now >= self.end:
                self.finish("завершена полностью", f"Готово! {self.total} минут «{self.title}» позади. Горжусь 💪")
            else:
                self.timer.config(text=self.fmt(self.end - now))
                self.status.config(text=f"фокус · попыток сбежать: {self.escapes}")
            if self.locked():
                self.guard(now)
        self.root.after(1000, self.tick)

    def guard(self, now: float):
        if not self.study or not user32.IsWindow(self.study):
            self.say("sys", "Окно учёбы закрылось — открываю снова.")
            self.study = None
            self.open_study()
            return
        fg = user32.GetForegroundWindow()
        me = int(self.root.wm_frame(), 16)
        root = user32.GetAncestor(fg, 2) if fg else None
        if root not in (self.study, me) and self.is_helper(root):
            pass  # IDE, Mathway, плеер курса — тоже учёба
        elif fg not in (self.study, me) and root not in (self.study, me):
            self.escapes += 1
            t = f"{_exe(root) if root else '?'} | {_title(root)[:80] if root else ''}"
            if t not in self.logged_titles:  # чтобы было видно, что именно считалось побегом
                self.logged_titles.add(t)
                log(f"{self.slot} escape -> {t}")
            self.place_study()
            force_foreground(self.study)
            self.root.lift()
            if self.escapes in (1, 5, 15) or self.escapes % 30 == 0:
                self.say("sys", f"Вернул на учёбу (попытка №{self.escapes}). Нужно выйти — напиши причину.")
        elif now - self.last_offtab_note > 90 and not self.on_topic(_title(self.study)):
            self.last_offtab_note = now
            self.say("sys", f"Похоже, открыта не та вкладка: «{_title(self.study)[:60]}». Вернись к «{self.title}».")

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
            self.finish(f"остановлена стоп-фразой через {self.elapsed()} мин.", "Стоп-кран сработал, отпускаю.")
            return
        if self.done:
            return
        self.busy = True
        self.btn.config(text="…", state="disabled")
        self.say("sys", "Claude думает…")
        left = max(0, int((self.end - time.time()) / 60))
        threading.Thread(target=self._ask, args=(left,), daemon=True).start()

    def _ask(self, left: int):
        try:
            ans = ask_claude(self.title, left, self.total, self.escapes, self.history)
        except Exception as e:
            log(f"claude error: {e}")
            ans = {"reply": "Не могу сейчас достучаться до себя (ошибка запуска Claude). "
                            f"Если правда надо выйти — стоп-фраза «{CONFIG['stop_phrase']}».",
                   "action": "none", "minutes": 0}
        self.root.after(0, self._apply, ans)

    def _apply(self, ans: dict):
        self.busy = False
        self.btn.config(text="➤", state="normal")
        self.say("Claude", ans.get("reply", ""))
        act = ans.get("action")
        if act == "unlock":
            reason = next((t for w, t in reversed(self.history) if w == USER), "")
            self.finish(f"отпущена Claude через {self.elapsed()} мин., причина: «{reason[:200]}»", None)
        elif act == "snooze" and ans.get("minutes"):
            mins = ans["minutes"]
            now = time.time()
            new_until = now + mins * 60
            # время учёбы не съедается паузой; повторная пауза поверх текущей добавляет только разницу
            self.end += max(0.0, new_until - max(now, self.snooze_until))
            self.snooze_until = max(self.snooze_until, new_until)
            self.say("sys", f"Пауза {mins} мин. Потом продолжим.")
            log(f"{self.slot} snooze {mins}")

    def elapsed(self) -> int:
        return max(0, int((time.time() - self.grace_until) / 60))

    def finish(self, outcome: str, message: str | None):
        if self.done:
            return
        self.done = True
        self.outcome = outcome
        if message:
            self.say("Claude", message)
        self.timer.config(text="✓", fg="#4fd18b")
        self.status.config(text="свободен")
        self.close_btn.pack(pady=(0, 16))
        self.root.attributes("-topmost", False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        line = (f"Фокус-сессия «{self.title}» {time.strftime('%Y-%m-%d %H:%M')}: {outcome}; "
                f"план {self.total} мин., попыток переключиться: {self.escapes}."
                + (f" Откликов отправлено: {self.sent} из {len(self.jobs)}." if self.jobs else ""))
        log(line)
        remember(line)

    def run(self):
        self.root.mainloop()
        if not self.done:
            log(f"{self.slot}: окно закрыто без завершения")


POSTPONE = ROOT / "postpone.json"


def wait_postponed(slot: str) -> None:
    """Разовый перенос на сегодня: postpone.json {"ГГГГ-ММ-ДД": {"slot": "ЧЧ:ММ"}}.
    Задача Планировщика стартует в обычное время, а сессия ждёт до перенесённого."""
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
        log(f"{slot}: перенесено на {hhmm}, жду")
        time.sleep(target - time.time())


def postpone(slot: str, hhmm: str) -> None:
    try:
        data = json.loads(POSTPONE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    today = time.strftime("%Y-%m-%d")
    data = {d: v for d, v in data.items() if d >= today}  # старые даты выкидываем
    data.setdefault(today, {})[slot] = hhmm
    POSTPONE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{slot}: сегодня в {hhmm}")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "postpone":
        postpone(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "ask":
        print(json.dumps(ask_claude("Тест", 30, 40, 2, [(USER, " ".join(sys.argv[2:]))]),
                         ensure_ascii=False, indent=1))
        return
    if len(sys.argv) < 3 or sys.argv[1] != "run" or sys.argv[2] not in CONFIG["slots"]:
        print(__doc__)
        return
    minutes = int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else None
    wait_postponed(sys.argv[2])
    if CONFIG["slots"][sys.argv[2]].get("jobhunter") and not ready_jobs(1):
        log(f"{sys.argv[2]}: в очереди JobHunter нет готовых откликов — сессию пропускаю")
        return
    log(f"start {sys.argv[2]}")
    Session(sys.argv[2], minutes).run()


if __name__ == "__main__":
    main()
