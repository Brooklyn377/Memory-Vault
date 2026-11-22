"""The Helper: a small fifth model on an external server of its own. It works outside the turns (turns.MODELS does not list
it), so it never waits for another model and never makes one wait.

It does two things.
- Jobs for the Assistant. helper_start hands it a job and returns at once; up to the Helper server's slots run at the same
  time, more wait in line. A job reads the vault, state, logs and the Assistant's sandbox (never marking line numbers as
  seen for the Assistant), and proposes edits to sandbox files, which reach the sandbox only when the Assistant approves
  them. A job's answer must quote, and every quote is checked against the file; or it says it found nothing, and the
  code lists what it searched from its own calls. There is no limit on a job's calls: it runs until it answers or its
  context fills. Results reach the Assistant with its next HANDLER message (collect), or through helper_wait.
- The chat memory. After each of the Assistant's tool calls it writes a short entry on what the call did (recent
  progress), and after each answer it writes that message into the chat's summary (chatmemory.fold_with_helper).

When the Helper is off, has no server, or its server does not answer, nothing here runs: the Assistant is told so and
the chat memory works as before."""
import difflib
import json
import queue
import re
import threading
import time

import agent

MODEL = "hp"
HELPER_TOOLS = ("helper_start", "helper_wait", "helper_cancel", "helper_approve", "helper_reject")

# ---------- what the models read ----------
# The Helper's part of the Assistant's prompt: put in for {HELPER} when the Helper can work, left out when it cannot.
ASSIST_PART = (
    'HELPER: a smaller model that works for you in the background while you carry on. helper_start {"task":"<what to do, '
    'at most {CALL_CHARS} characters>"} gives it one job and returns the job number at once. Up to {HELPER_SLOTS} jobs run '
    'at the same time; more wait in line. Give it small, clear jobs: find where something is, read a file and report what '
    'it does, or write one small code change. It can read the vault, state, logs and your sandbox, and it can propose '
    'edits to sandbox files. It cannot run commands, use the web or change notes. When a job finishes, its result is added '
    'to the HANDLER message after your next tool call, so keep working meanwhile. helper_wait {} waits until the next job '
    'finishes; use it only when you have nothing else to do. Add "calls":<number> to stop a job after that many replies '
    'from the Helper (tool calls and replies that were sent back); leave it out for no limit. helper_cancel '
    '{"job":<number>} stops a job. The system checks '
    'that every quote in its answers is really in that file at that line, but check its conclusions before you rely on '
    'them. An edit it proposes is NOT in your sandbox until you approve it: read the diff, then helper_approve '
    '{"job":<number>,"edit":<number>} applies it exactly, or helper_reject {"job":<number>,"edit":<number>,"why":"<what '
    'is wrong and what to do instead>"} sends it back. You cannot answer the person while a job is running or an edit is '
    'waiting: wait for it, decide on it, or cancel it first.')

JOB_SYS = (
    "You are the Helper: a small model that does one job for the Assistant, another model that is working on a person's "
    "request. You never talk to the person. Messages that start with HANDLER come from the system that runs your tools. "
    'Reply with ONLY JSON, with nothing outside it: either exactly ONE tool call {"tool":"<name>","args":{...}}, then wait '
    "for its result, or your result. The text you write in one tool call (old, new or text) can be at most {CALL_CHARS} "
    'characters. Do only the job you were given, nothing more. TO READ: search {"text":"<words>","where":"all|notes|raw|'
    'inbox|vault|state|logs"} finds files in the vault and state; open {"path":"<path>","start":<line>} reads one of those '
    'files or lists a folder; links {"path":"<note path>"} lists linked notes; repos {} lists the Git servers and what each '
    'is for; list {"path":"<folder>"} lists a sandbox folder; grep {"path":"<folder or file>","pattern":"<regular '
    'expression>"} finds matching lines with their line numbers; view {"path":"<file>","start":<line>,"end":<line>} reads '
    "a sandbox file with line numbers. Sandbox paths look like git/<Git server name>/<file path>. TO PROPOSE A CHANGE to a "
    'sandbox file: propose_edit {"path":"<file>","old":"<exact text that is in the file once>","new":"<replacement>",'
    '"why":"<one sentence>"} changes one piece; propose_file {"path":"<new file>","text":"<the start of the file>","why":'
    '"<one sentence>"} makes a new file. A change is NOT made until the Assistant approves it: you wait, and a HANDLER '
    "message tells you whether it was applied or sent back, and why. View the file before propose_edit and copy old "
    "exactly. For a bigger change, send several small changes, one at a time. WHEN YOU ARE DONE, reply with "
    '{"answer":"<what you found or did>","quotes":[{"path":"<file>","line":<line number>,"text":"<exact text from that '
    'line>"}]}. Every quote is checked: it must be in that file at that line, exactly. Back every fact in your answer with '
    'a quote. If you could not find it, reply with {"not_found":"<what you looked for, and what you found instead, if '
    'anything>"}. Never guess.')

PROGRESS_SYS = (
    "You keep track of what the Assistant is doing while it works on the person's message. You get the person's message, "
    "the progress so far, and the Assistant's newest tool call with its result. Write a short entry, at most {CALL_CHARS} "
    "characters, saying what this call did and what came of it, for the Assistant to read later: the file and line "
    "numbers, what was found or changed, or the error. Keep exact names, paths, numbers and commands. Reply with ONLY "
    'JSON: {"entry":"<label>: <what it did and what came of it>"}, where <label> is exactly: {LABEL}')

MEMORY_SYS = (
    "You keep the memory of one chat between a person and their assistant, another model called the Assistant. You get THE "
    "MEMORY SO FAR (it may be empty) and the NEWEST MESSAGE of the chat: what the person wrote, what the Assistant did (its "
    "tool calls, and the progress lines written while it worked) and its reply. Write the new memory: everything in the "
    "memory so far that still matters, plus what this message adds. Keep exact names, file paths, commands, addresses, "
    "numbers, decisions and corrections. Where the person corrected something, keep only the corrected version. Leave out "
    "filler and the way things were said. Use at most {SUMMARY_WORDS} words in all. Reply with ONLY JSON: "
    '{"goal":"<what the person is trying to get done in this chat>","decisions":["<something decided, or a preference the '
    'person stated>"],"done":["<something finished, with the file or place>"],"files":["<path>: <what it is, or what was '
    'changed in it>"],"open":["<something still to do, waiting for an answer, or unanswered>"],"facts":["<anything else '
    'worth keeping>"]}. Use an empty list where there is nothing.')

# How each of the Helper's tools is called: sent back with a call that did not work.
HUSAGE = {
    "search": '{"text":"<words>","where":"all|notes|raw|inbox|vault|state|logs"}',
    "open": '{"path":"<path>","start":<line>}',
    "links": '{"path":"<note path>"}',
    "repos": "{}",
    "list": '{"path":"<folder>"}',
    "grep": '{"path":"<folder or file>","pattern":"<regular expression>"}',
    "view": '{"path":"<file>","start":<line>,"end":<line>}',
    "propose_edit": '{"path":"<file>","old":"<exact text that is in the file once>","new":"<replacement>","why":"<one sentence>"}',
    "propose_file": '{"path":"<new file>","text":"<the start of the file>","why":"<one sentence>"}',
}
# The Assistant's helper tools, as its instructions give them.
AUSAGE = {
    "helper_start": '{"task":"<what to do>","calls":<number, optional>}',
    "helper_wait": "{}",
    "helper_cancel": '{"job":<number>}',
    "helper_approve": '{"job":<number>,"edit":<number>}',
    "helper_reject": '{"job":<number>,"edit":<number>,"why":"<what is wrong and what to do instead>"}',
}


def _h():
    import ui_chat
    return ui_chat.H


def _calls():
    import tuning
    return int(tuning.get("chat.call_chars"))


# ---------- whether it can work ----------
def why_off():
    """'' when the Helper can work now, else why not, in words that fit "the Helper is off (...)"."""
    try:
        import power
        import remote
        if not remote.enabled(MODEL):
            return "it has no server set"
        if power.off(MODEL):
            return "its switch is off on the Settings page"
        state = remote.health(MODEL)
        if state != "ready":
            what = {"loading": "it is still loading its model", "key": "the server rejected the API key"}.get(
                state, f"no answer from {remote.base(MODEL)}")
            return f"its server cannot be reached: {what}"
        return ""
    except Exception as e:
        return f"its server cannot be reached: {type(e).__name__}"


def on():
    return not why_off()


def slots():
    import settings
    return max(1, int(settings.get(MODEL).get("parallel") or 1))


def slot_tokens():
    """How many tokens one call to the Helper can hold: its context shared by its slots, or what its server says, if less."""
    import settings
    s = settings.get(MODEL)
    n = int(s["context"]) // slots()
    try:
        import ui_think
        st = ui_think.slots(MODEL, whole_server=True)
        if st.get("up") and st.get("ctx"):
            n = min(n, int(st["ctx"]))
    except Exception:
        pass
    return n


def tokens(chars):
    return int(chars / 2.2)   # the same rough estimate as the token checks in tuning.py: on the safe side


def summary_tokens():
    """About how many tokens a summary at its full length takes (Summary length, in words)."""
    return int(_tune("assistant.summary_words") * 1.4)


def reserve(kind):
    """Tokens kept free in a slot for the reply: what it can need, never more than its max tokens. A memory reply is a
    whole summary; a progress reply is one entry. Thinking takes more: a reply that runs out stops at its max tokens."""
    import settings
    cap = int(settings.get(MODEL)["max_tokens"]["memory"])
    need = summary_tokens() + 500 if kind == "memory" else tokens(_calls()) + 500
    return min(cap, need)


def _tune(key):
    import tuning
    return tuning.get(key)


def assist_fill():
    """The placeholders of the Assistant's prompt."""
    return {"{HELPER}": ASSIST_PART if on() else "", "{HELPER_SLOTS}": str(slots())}


# ---------- one call to the Helper ----------
class Full(RuntimeError):
    """The request was bigger than the Helper's context: (tokens asked, tokens it holds)."""


class Limit(RuntimeError):
    """Its reply stopped at its max tokens."""


def _call(system, msgs, label, fill=None, cancel=None, on_wait=None):
    """One call to the Helper. A server that drops the connection, cannot be reached or answers with a server error is
    tried again every minute while cancel() is false; on_wait(reason) is called once when that starts. A switch or stop
    of the Helper (thinklog.Aborted) and cancel() end it. -> the reply text."""
    import remote
    import thinklog
    import ui_chat
    import ui_settings
    import settings
    s = settings.get(MODEL)
    cap = s["max_tokens"]["job" if label == "job" else "memory"]
    body = {"model": s.get("model_file") or "helper", "temperature": s["temperature"], "max_tokens": cap,
            "messages": [{"role": "system", "content": system}] + msgs, "fill": fill or {}}
    caps = ui_settings.caps(ui_settings.capname(MODEL, s))
    if caps["thinking"]:
        body["chat_template_kwargs"] = {"enable_thinking": bool(s.get("thinking", True))}
    elif caps["reasoning_effort"]:
        body["chat_template_kwargs"] = {"reasoning_effort": s.get("reasoning_effort", "low")}
    told = False
    while True:
        if cancel and cancel():
            raise thinklog.Aborted("helper: the call was canceled")
        try:
            r = thinklog.chat(MODEL, remote.endpoint(MODEL), body, label, cancel=cancel)
        except thinklog.Aborted:
            raise
        except Exception as e:
            big = re.search(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)", str(e))
            if big:
                raise Full(int(big.group(1)), int(big.group(2))) from None
            if not ui_chat._transient(e):
                raise
            if on_wait and not told:
                on_wait(f"{type(e).__name__}: {str(e)[:120]}")
                told = True
            agent.log(f"helper: its server did not answer ({type(e).__name__}: {str(e)[:100]}); trying again in 60s")
            for _ in range(60):
                if cancel and cancel():
                    break
                time.sleep(1)
            continue
        if r.get("finish_reason") == "length":
            raise Limit(r.get("tokens") or 0, cap)
        return (r.get("content") or "").strip()


# ---------- jobs ----------
LOCK = threading.Condition()
RUNS = {}   # message id -> {"m": the message, "next": last job number, "jobs": {n: Job}, "events": [text for the Assistant]}
OPEN = ("waiting", "running", "edit", "server")   # a job in one of these is not finished


class Cancel(Exception):
    """The job was stopped (helper_cancel, or the answer ended)."""


class Job:
    def __init__(self, mid, n, task, limit=None):
        self.mid, self.n, self.task, self.limit = mid, n, task, limit   # limit: most replies, set by the Assistant (None: none)
        self.replies = 0           # its replies so far: tool calls, and replies that were sent back
        self.state, self.cancelled, self.queued = "waiting", False, time.time()
        self.calls = []            # (tool, args) of its look-ups, for "what it searched"
        self.edits = []            # {"n", "kind", "path", "old", "new", "text", "why", "state"}
        self.decision = None       # set by approve / reject / a refused approve; the job reads it and goes on
        self.used = 0              # its tool calls that ran

    def stop(self):
        return self.cancelled


def begin(m):
    """The Assistant starts answering message m: jobs started now belong to it."""
    with LOCK:
        RUNS[m["id"]] = {"m": m, "next": 0, "jobs": {}, "events": []}
        m["helper"] = []


def end(mid):
    """The answer finished, was stopped or failed: every job still open is stopped. Edits not yet approved are dropped."""
    with LOCK:
        run = RUNS.pop(mid, None)
        if not run:
            return
        for j in run["jobs"].values():
            j.cancelled = True
        LOCK.notify_all()
        run["m"].pop("helper", None)
    left = [j.n for j in run["jobs"].values() if j.state in OPEN]
    if left:
        agent.log(f"helper: the answer to {mid} ended; stopped job(s) {', '.join(map(str, left))}")


def _show(run):
    """The jobs that are not finished, for the line under the message on the chat page."""
    rows = []
    for j in sorted(run["jobs"].values(), key=lambda x: x.n):
        if j.state in OPEN:
            wait = next((e["n"] for e in j.edits if e["state"] == "waiting"), None)
            rows.append({"n": j.n, "state": j.state, "edit": wait, "task": j.task[:80]})
    run["m"]["helper"] = rows
    try:
        import ui_chat
        ui_chat._save(run["m"])
    except Exception:
        pass


def _event(job, text):
    with LOCK:
        run = RUNS.get(job.mid)
        if run is not None and not job.cancelled:
            run["events"].append(text)
        LOCK.notify_all()


def collect(mid):
    """The results that came in since the Assistant last heard: each a paragraph for its next HANDLER message."""
    with LOCK:
        run = RUNS.get(mid)
        if not run:
            return []
        out, run["events"] = run["events"], []
        return out


def _running(run):
    return [j for j in run["jobs"].values() if j.state in ("running", "server")]


def _line(run):
    return sorted((j for j in run["jobs"].values() if j.state == "waiting" and not j.cancelled), key=lambda j: j.queued)


def _take_slot(job):
    """Wait until this job may run: a slot is free and it is first in line."""
    with LOCK:
        if job.cancelled:
            raise Cancel()
        job.state, job.queued = "waiting", time.time()
        while True:
            if job.cancelled:
                raise Cancel()
            run = RUNS.get(job.mid)
            if run is None:
                raise Cancel()
            line = _line(run)
            if len(_running(run)) < slots() and line and line[0] is job:
                job.state = "running"
                _show(run)
                LOCK.notify_all()
                return
            LOCK.wait(1)


def blocking(mid):
    """Why the Assistant may not answer yet: the text for its HANDLER message, or ''."""
    with LOCK:
        run = RUNS.get(mid)
        if not run:
            return ""
        going = sorted(j.n for j in run["jobs"].values() if j.state in ("waiting", "running", "server"))
        edits = [(e["n"], j.n) for j in sorted(run["jobs"].values(), key=lambda x: x.n) for e in j.edits if e["state"] == "waiting"]
        unseen = list(run["events"])
        run["events"] = []
    if not going and not edits and not unseen:
        return ""
    parts = []
    if going:
        parts.append(f"Helper job {going[0]} is still running" if len(going) == 1 else
                     f"Helper jobs {_and(going)} are still running")
    if edits:
        which = _and([f"edit {e} of job {j}" for e, j in edits])
        parts.append(f"{which} {'is' if len(edits) == 1 else 'are'} waiting for your decision")
    if unseen and not parts:
        return ("your answer was not sent to the person, because results of Helper jobs came in that you have not seen "
                "yet; they are below. Read them, then answer again.\n\n" + "\n\n".join(unseen))
    todo = (["wait for them with helper_wait"] if going else []) + \
        (["decide on the edit with helper_approve or helper_reject"] if edits else []) + ["stop them with helper_cancel"]
    say = ", ".join(todo[:-1]) + ", or " + todo[-1]   # only what applies: no edit to decide on, no clause for it
    text = "your answer was not sent to the person, because " + " and ".join(parts) + ". " + say[0].upper() + say[1:] + ", then answer again."
    if unseen:
        text += "\n\nResults that came in meanwhile:\n\n" + "\n\n".join(unseen)
    return text


def _and(xs):
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


# ---------- the Assistant's helper tools ----------
class Refused(ValueError):
    plain = True


def _num(args, key):
    v = args.get(key)
    try:
        n = int(v)
        if n == float(v):
            return n
    except (TypeError, ValueError):
        pass
    raise Refused(f'"{key}" must be a whole number, like {{"{key}":1}}.')


def _job(mid, args):
    n = _num(args, "job")
    run = RUNS.get(mid)
    jobs = (run or {}).get("jobs") or {}
    if n not in jobs:
        have = sorted(jobs)
        raise Refused(f"there is no Helper job {n}. " + (f"The jobs of this answer are {_and(have)}." if have else
                                                         "No Helper job was started for this answer."))
    return jobs[n]


def assistant_call(name, args, m, halted):
    """Run one of the Assistant's helper tools. -> (text for its HANDLER message, whether it failed). halted() is true when
    the answer was stopped: helper_wait then returns at once."""
    args = args if isinstance(args, dict) else {}
    try:
        text = {"helper_start": _start, "helper_wait": _wait, "helper_cancel": _cancel,
                "helper_approve": _approve, "helper_reject": _reject}[name](m["id"], args, halted)
        failed = False
    except Refused as e:
        text, failed = str(e), True
    except Exception as e:
        text, failed = f"{name} failed ({type(e).__name__}): {e}", True
    agent.log(f"assistant: {name} {' '.join(json.dumps(args, ensure_ascii=False)[:160].split())} -> {' '.join(str(text).split())[:140]}")
    return text, failed


def _start(mid, args, halted):
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        raise Refused('give the job as text in "task".')
    limit = args.get("calls")
    if limit is not None:
        try:
            limit = int(limit)
            ok = limit == float(args["calls"]) and limit > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise Refused('"calls" must be a whole number above 0, like {"task":"<what to do>","calls":20}, or left out for no limit.')
    why = why_off()
    if why:
        return f"the Helper is off ({why}), so nothing was started. Do this work yourself with your own tools."
    with LOCK:
        run = RUNS.get(mid)
        if run is None:
            raise Refused("no answer is running, so no job can be started.")
        busy, ahead, most = len(_running(run)), len(_line(run)), slots()   # before this job joins the line
        run["next"] += 1
        job = Job(mid, run["next"], task.strip(), limit)
        run["jobs"][job.n] = job
        _show(run)
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"helper-job-{job.n}").start()
    agent.log(f"helper: job {job.n} started for {mid}: {task.strip()[:120]}")
    if busy < most and not ahead:
        return f"Helper job {job.n} started. Its result will come in a later HANDLER message; carry on with your own work."
    return (f"Helper job {job.n} is waiting in line: {busy} job{'s are' if busy != 1 else ' is'} running (the most at once "
            f"is {most}) and it is number {ahead + 1} in line. It starts when one finishes.")


def _wait(mid, args, halted):
    with LOCK:
        while True:
            run = RUNS.get(mid)
            if run is None:
                raise Refused("There are no Helper jobs running or waiting, so there is nothing to wait for.")
            if run["events"]:
                out, run["events"] = run["events"], []
                return "\n\n".join(out)
            if not any(j.state in ("waiting", "running", "server") for j in run["jobs"].values()):
                if any(e["state"] == "waiting" for j in run["jobs"].values() for e in j.edits):
                    raise Refused("There are no Helper jobs running or waiting, so there is nothing to wait for. An edit is "
                                  "waiting for your decision: use helper_approve or helper_reject.")
                raise Refused("There are no Helper jobs running or waiting, so there is nothing to wait for.")
            if halted():
                return "the answer was stopped."
            LOCK.wait(1)


def _cancel(mid, args, halted):
    with LOCK:
        job = _job(mid, args)
        if job.state not in OPEN:
            raise Refused(f"Helper job {job.n} has already finished, so there is nothing to stop.")
        job.cancelled = True
        for e in job.edits:
            if e["state"] == "waiting":
                e["state"] = "dropped"
        job.state = "cancelled"
        _show(RUNS[mid])
        LOCK.notify_all()
    agent.log(f"helper: job {job.n} stopped by the Assistant")
    return (f"Helper job {job.n} was stopped. Edits of it that you had not approved were thrown away; edits you already "
            "approved stay in your sandbox.")


def _edit(job, args):
    k = _num(args, "edit")
    hit = next((e for e in job.edits if e["n"] == k), None)
    if hit is None:
        raise Refused(f"Helper job {job.n} has no edit {k}." + (f" Its edits are {_and([e['n'] for e in job.edits])}."
                                                                 if job.edits else " It has proposed none."))
    if hit["state"] != "waiting":
        raise Refused(f"edit {k} of Helper job {job.n} is not waiting for a decision: it was already "
                      f"{ {'applied': 'applied', 'rejected': 'sent back', 'refused': 'refused', 'dropped': 'thrown away'}.get(hit['state'], hit['state']) }.")
    return hit


def _approve(mid, args, halted):
    import sandbox
    with LOCK:
        job = _job(mid, args)
        e = _edit(job, args)
    path = e["path"]
    try:
        if e["kind"] == "edit":
            _, _, p = sandbox._file(path, True)
            count = sandbox._text(p, path).count(e["old"])
            if count != 1:
                return _refuse(job, e, f'"old" is now there {count} times instead of once',
                               f"Edit {e['n']} of Helper job {job.n} was not applied, and nothing was changed: the text it "
                               f"replaces is now in {path} {count} times instead of exactly once, because the file changed "
                               f"after the edit was proposed. Job {job.n} has been told and will send a new edit, or you can "
                               "stop it with helper_cancel.",
                               f'edit {e["n"]} could not be applied, and nothing was changed: {path} changed after you '
                               f'proposed it, and "old" is now there {count} times instead of once. View the file again and '
                               "send a new edit.")
            sandbox.replace(path, e["old"], e["new"])
        else:
            try:
                sandbox._file(path, False)
            except sandbox.Refused:
                return _refuse(job, e, "the file now exists",
                               f"Edit {e['n']} of Helper job {job.n} was not applied, and nothing was changed: {path} now "
                               f"exists, because it was made after the edit was proposed. Job {job.n} has been told and will "
                               "send a new edit, or you can stop it with helper_cancel.",
                               f"edit {e['n']} could not be applied, and nothing was changed: {path} was made after you "
                               "proposed it. View it, and use propose_edit to change it.")
            sandbox.create(path, e["text"])
    except sandbox.Refused as ex:
        raise Refused(f"edit {e['n']} of Helper job {job.n} was not applied: {ex}") from None
    with LOCK:
        e["state"] = "applied"
        job.decision = f"the Assistant approved edit {e['n']}, and it is now in {path}. Carry on with the job, or send your answer if it is done."
        LOCK.notify_all()
    agent.log(f"helper: edit {e['n']} of job {job.n} applied to {path}")
    return f"Edit {e['n']} of Helper job {job.n} was applied to {path} exactly as shown. Job {job.n} carries on."


def _refuse(job, e, why, to_assistant, to_helper):
    with LOCK:
        e["state"] = "refused"
        job.decision = to_helper
        LOCK.notify_all()
    agent.log(f"helper: edit {e['n']} of job {job.n} not applied: {why}")
    return to_assistant


def _reject(mid, args, halted):
    why = args.get("why")
    if not isinstance(why, str) or not why.strip():
        raise Refused('say in "why" what is wrong and what to do instead.')
    with LOCK:
        job = _job(mid, args)
        e = _edit(job, args)
        e["state"] = "rejected"
        job.decision = (f'the Assistant did not accept edit {e["n"]}, and nothing was changed. Its reason: "{why.strip()}". '
                        "Send a new edit that fixes this, or your answer if the job cannot be done.")
        LOCK.notify_all()
    agent.log(f"helper: edit {e['n']} of job {job.n} sent back: {why.strip()[:120]}")
    return (f"Edit {e['n']} of Helper job {job.n} was sent back to it with your reason, and nothing was changed. Its next "
            "edit or its answer will come in a later HANDLER message.")


# ---------- a job at work ----------
def _searched(job):
    """What the job looked at, from its own calls (never from what it says)."""
    out = []
    for name, a in job.calls:
        if name == "search":
            out.append(f'search "{a.get("text", "")}" in {a.get("where") or "all"}')
        elif name == "grep":
            out.append(f'grep "{a.get("pattern", "")}" in {a.get("path", "")}')
        elif name == "view":
            s, e = a.get("start"), a.get("end")
            out.append(f"view {a.get('path', '')}" + (f" lines {s or 1}-{e}" if e else f" from line {s}" if s else ""))
        elif name == "open":
            out.append(f"open {a.get('path', '')}" + (f" from line {a['start']}" if a.get("start") else ""))
        elif name in ("list", "links"):
            out.append(f"{name} {a.get('path', '')}")
        elif name == "repos":
            out.append("repos")
    return "; ".join(out) or "nothing: it made no look-ups"


def _lines_of(path):
    """The lines of a file the Helper may read, as its tools number them. Raises when it does not exist or cannot be read."""
    import assistant_tools
    import sandbox
    path = str(path or "").strip()
    if path.startswith("git/"):
        _, _, p = sandbox._file(path, True, reading=True)
        return sandbox._text(p, path).splitlines()
    _, p = assistant_tools.resolve(path)
    if p.is_dir() or not assistant_tools._is_text(p):
        raise ValueError("not a text file")
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def _check_answer(d):
    """The Helper's final reply -> ("answer"|"not_found", text, quotes) or ("problem", what to tell it, None)."""
    h = _h()
    if "not_found" in d and "answer" not in d:
        t = d.get("not_found")
        if not isinstance(t, str) or not t.strip():
            return "problem", (h + 'the "not_found" in your reply is empty or not text, so it was not passed on. Say what you '
                               'looked for: {"not_found":"<what you looked for, and what you found instead, if anything>"}.'), None
        return "not_found", t.strip(), None
    a = d.get("answer")
    if not isinstance(a, str) or not a.strip():
        return "problem", (h + 'the "answer" in your reply is empty or not text, so it was not passed on. Put what you found '
                           'or did as text: {"answer":"<what you found or did>","quotes":[...]}.'), None
    quotes = d.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        return "problem", (h + "your answer was not passed on, because it has no quotes. Back what you found with quotes, or "
                           'reply with {"not_found":"..."} if you did not find it.'), None
    for i, q in enumerate(quotes, 1):
        if not (isinstance(q, dict) and isinstance(q.get("path"), str) and isinstance(q.get("text"), str) and q["text"].strip()):
            return "problem", (h + f"your answer was not passed on, because quote {i} is not in the form "
                               '{"path":"<file>","line":<line number>,"text":"<exact text from that line>"}. Fix it and '
                               "send your answer again."), None
        try:
            line = int(q.get("line"))
        except (TypeError, ValueError):
            return "problem", (h + f'your answer was not passed on, because the "line" of quote {i} is not a whole number. '
                               "Fix it and send your answer again."), None
        try:
            lines = _lines_of(q["path"])
        except Exception:
            return "problem", (h + f"your answer was not passed on, because quote {i} names {q['path']}, which does not exist "
                               "or you cannot read. Fix the quote or leave it out, then send your answer again."), None
        t = q["text"].strip()
        if 1 <= line <= len(lines) and t in lines[line - 1]:
            continue
        where = [n for n, l in enumerate(lines, 1) if t in l]
        there = (f'but line {line} is: "{lines[line - 1].strip()}".' if 1 <= line <= len(lines) else
                 f"but {q['path']} has {len(lines)} lines, so there is no line {line}.")
        found = (f" That text is on line {where[0]}." if len(where) == 1 else
                 f" That text is on lines {_and(where[:5])}." if where else "")
        return "problem", (h + f"your answer was not passed on, because quote {i} is not in that file at that line. You "
                           f'quoted {q["path"]}:{line} as "{t}", {there}{found} Fix the quote or leave it out, then send '
                           "your answer again."), None
    return "answer", a.strip(), quotes


def _proposal(job, name, args):
    """A propose_edit or propose_file call -> (the edit, its diff) or raises Refused with what to tell the Helper."""
    import sandbox
    path = str(args.get("path") or "").strip().strip("/")
    why = args.get("why")
    if not path.startswith("git/"):
        raise Refused("your edit was not sent to the Assistant, because edits can only be made to sandbox files, whose paths "
                      "start with git/<Git server name>/.")
    if not isinstance(why, str) or not why.strip():
        raise Refused('your edit was not sent to the Assistant, because "why" is empty: say in one sentence why this edit is needed.')
    try:
        if name == "propose_edit":
            old, new = args.get("old"), args.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise Refused('your edit was not sent to the Assistant, because "old" and "new" must both be text, and "old" '
                              "cannot be empty.")
            try:
                _, _, p = sandbox._file(path, True, reading=True)
            except sandbox.Refused:
                raise Refused(f"your edit was not sent to the Assistant, because {path} is not a file in the working copy. Use "
                              "propose_file to make a new file.") from None
            current = sandbox._text(p, path)
            n = current.count(old)
            if n != 1:
                raise Refused(f'your edit was not sent to the Assistant, because "old" is in {path} {n} times; it must be there '
                              "exactly once. View the file and copy the text exactly; if it is there more than once, include "
                              "more of the lines around it.")
            if old == new:
                raise Refused('your edit was not sent to the Assistant, because "new" is the same as "old".')
            after = current.replace(old, new, 1)
            e = {"kind": "edit", "path": path, "old": old, "new": new, "why": why.strip()}
        else:
            text = args.get("text")
            if not isinstance(text, str):
                raise Refused('your edit was not sent to the Assistant, because "text" must be the start of the file, as text.')
            try:
                sandbox._file(path, False)
            except sandbox.Refused:
                raise Refused(f"your edit was not sent to the Assistant, because {path} already exists. Use propose_edit to change it.") from None
            current, after = "", text
            e = {"kind": "file", "path": path, "text": text, "why": why.strip()}
    except sandbox.Refused as ex:
        raise Refused(f"your edit was not sent to the Assistant: {ex}") from None
    diff = "\n".join(difflib.unified_diff(current.splitlines(), after.splitlines(),
                                          "/dev/null" if e["kind"] == "file" else "a/" + path, "b/" + path, lineterm=""))
    return e, diff


def _tool(job, name, args):
    """One of the Helper's look-ups. -> (text, failed)."""
    import assistant_tools
    import sandbox
    from scrub import chat_text
    fns = {"search": assistant_tools.do_search,
           "open": lambda a: assistant_tools.open_file(a.get("path", ""), a.get("start", 1)),
           "links": lambda a: assistant_tools.links(a.get("path", "")),
           "repos": lambda a: assistant_tools.repos(),
           "list": lambda a: sandbox.list_dir(a.get("path")),
           "grep": lambda a: sandbox.grep(a.get("path"), a.get("pattern"), mark=False),
           "view": lambda a: sandbox.view(a.get("path"), a.get("start"), a.get("end"), mark=False)}
    failed = True
    try:
        text = fns[name](args)
        failed = False
    except (ValueError, OSError) as e:
        text = str(e) if getattr(e, "plain", False) else f"{name} failed: {e}"
    except Exception as e:
        text = f"{name} failed ({type(e).__name__}): {e}"
    text = chat_text(str(text))
    agent.log(f"helper: job {job.n}: {name} {' '.join(json.dumps(args, ensure_ascii=False)[:160].split())} -> {' '.join(text.split())[:120]}")
    return text, failed


def _run(job):
    import thinklog
    try:
        _work(job)
    except Cancel:
        pass
    except thinklog.Aborted:
        if not job.cancelled:
            _finish(job, "failed", f"Helper job {job.n} stopped: the Helper was turned off or its server was switched while it "
                                   f"worked. What it searched (from its own calls): {_searched(job)}. Edits you already "
                                   "approved stay in your sandbox.")
    except Full as e:
        _finish(job, "failed", f"Helper job {job.n} failed: its context filled up after {job.used} tool calls (its slot holds "
                               f"{e.args[1]:,} tokens) before it answered. What it searched (from its own calls): "
                               f"{_searched(job)}. Nothing it had not yet proposed was kept. To try again, start a new job "
                               "with a smaller task or one that says where to look.")
    except Limit as e:
        _finish(job, "failed", f"Helper job {job.n} failed: its reply hit the limit of {e.args[1]:,} tokens (Job max tokens on "
                               f"the Helper's card) before it finished. What it searched (from its own calls): "
                               f"{_searched(job)}. Raise Job max tokens, or start a new job with a smaller task.")
    except Exception as e:
        agent.log(f"helper: job {job.n} failed ({type(e).__name__}: {str(e)[:200]})")
        _finish(job, "failed", f"Helper job {job.n} failed: {type(e).__name__}: {str(e)[:300]}. What it searched (from its "
                               f"own calls): {_searched(job)}.")
    finally:
        with LOCK:
            if job.state in OPEN:
                job.state = "cancelled" if job.cancelled else "failed"
            run = RUNS.get(job.mid)
            if run is not None:
                _show(run)
            LOCK.notify_all()


def _finish(job, state, text):
    with LOCK:
        if job.cancelled:
            return
        job.state = state
    _event(job, text)
    agent.log(f"helper: job {job.n} {state}: {' '.join(text.split())[:160]}")


def _work(job):
    import ui_chat
    h = _h()
    msgs = [{"role": "user", "content": h + f'your job from the Assistant: "{job.task}"'}]
    last_fail = None
    stopping = lambda: job.cancelled or __import__("power").off(MODEL)

    def server_wait(why):
        with LOCK:
            if job.state == "running":
                job.state = "server"
        _event(job, f"Helper job {job.n} is waiting: the Helper's server cannot be reached ({why}). It tries again every "
                    "minute; helper_cancel stops it.")
        with LOCK:
            run = RUNS.get(job.mid)
            if run is not None:
                _show(run)

    back = None   # (the reply, why it was sent back) while the last reply was sent back

    def stuck():
        """The limit the Assistant set is reached: the job ends with what it did. -> True when it ended."""
        if not job.limit or job.replies < job.limit:
            return False
        why = f" Its last reply was sent back because: {ui_chat._sentence(back[1])}" if back else ""
        _finish(job, "failed", f"Helper job {job.n} stopped at the limit of {job.limit} replies you set, without an answer. "
                               f"What it searched (from its own calls): {_searched(job)}.{why}")
        return True

    _take_slot(job)
    while True:
        if job.cancelled:
            raise Cancel()
        text = _call(JOB_SYS, msgs, "job", cancel=stopping, on_wait=server_wait)
        with LOCK:
            if job.state == "server":
                job.state = "running"
        if job.cancelled:
            raise Cancel()
        job.replies += 1
        plain = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        say, sent_back = _reply(job, text, plain, back, last_fail)
        if say is None:   # its answer was accepted: the job is done
            return
        if isinstance(say, tuple):   # a tool call ran: (what to tell it, the failed call or None)
            say, last_fail = say
        if sent_back:
            back = (plain, say[len(h):])
            agent.log(f"helper: job {job.n}: sent its reply back: {' '.join(say[len(h):].split())[:160]}")
        else:
            back = None
        msgs += [{"role": "assistant", "content": text or "(empty reply)"}, {"role": "user", "content": say}]
        if stuck():
            return


def _reply(job, text, plain, back, last_fail):
    """One reply of the Helper -> (what to tell it, whether it was sent back), or (None, False) when its answer was
    accepted and the job finished. For a tool call that ran, the first item is (what to tell it, the call if it failed)."""
    import ui_chat
    h = _h()
    if back and plain == back[0]:   # the same reply as the one just sent back: the same reason, nothing checked again
        return (h + "this is exactly the reply that was just sent back, so it was not checked again. It was sent back "
                f"because: {ui_chat._sentence(back[1])} Change what that reason points to."), True
    if not plain:
        return (h + 'your last reply was empty, so nothing ran. Reply with one tool call {"tool":"<name>","args":{...}} or '
                'with {"answer":"<your reply>"}.'), True
    kind, value = ui_chat._read_reply(text, finals=("answer", "not_found"), whole=True, passed="passed on")
    if kind == "problem":
        return value, True
    if kind == "answer":
        what, said, quotes = _check_answer(value)
        if what == "problem":
            return said, True
        if what == "not_found":
            _finish(job, "done", f'Helper job {job.n} finished without finding it. Task: "{job.task}"\nWhat it says: '
                                 f"{said}\nWhat it searched (from its own calls): {_searched(job)}")
        else:
            shown = "\n".join(f'- {q["path"]}:{int(q["line"])}: "{q["text"].strip()}"' for q in quotes)
            _finish(job, "done", f'Helper job {job.n} finished. Task: "{job.task}"\nAnswer: {said}\n'
                                 f"Quotes (checked: each is in that file at that line):\n{shown}")
        return None, False
    name, args = str(value.get("tool")), value.get("args")
    if not isinstance(args, dict):
        args = {a: v for a, v in value.items() if a not in ("tool", "args")}
    same = json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)
    big = ui_chat._too_big(name, args, _calls())
    if name not in HUSAGE:
        return h + f'there is no tool called "{name}", so nothing ran. The tools are: {", ".join(HUSAGE)}.', True
    if last_fail and last_fail[0] == same:
        return (h + f"this call to {name} is exactly the same as the one that just failed, so it was not run again. It "
                f"failed because: {ui_chat._sentence(last_fail[1])} Change what that reason points to before you send it again."), True
    if big:
        return big, True
    job.used += 1
    if name in ("propose_edit", "propose_file"):
        try:
            e, diff = _proposal(job, name, args)
        except Refused as ex:
            return (h + str(ex), (same, str(ex))), True
        return (_propose(job, e, diff), None), False
    result, failed = _tool(job, name, args)
    if failed:
        said = ui_chat.chat_text(json.dumps(args, ensure_ascii=False, default=str))
        return ((h + f"your call to {name} with {said} did not work, and nothing was changed. The reason: "
                 f"{ui_chat._sentence(result)} The correct form is: {name} {HUSAGE[name]}."), (same, result)), True
    job.calls.append((name, args))
    return (h + f"the result of your call to {name}:\n{result}", None), False

def _propose(job, e, diff):
    """Hand an edit to the Assistant and wait for its decision, holding no slot. -> what to tell the Helper."""
    with LOCK:
        e["n"], e["state"] = len(job.edits) + 1, "waiting"
        job.edits.append(e)
        job.decision, job.state = None, "edit"
        run = RUNS.get(job.mid)
        if run is not None:
            _show(run)
        LOCK.notify_all()
    _event(job, f"Helper job {job.n} proposes edit {e['n']} to {e['path']}. It is NOT in your sandbox yet. Why: {e['why']}\n"
                f"{diff}\nApprove it with helper_approve {{\"job\":{job.n},\"edit\":{e['n']}}} or send it back with "
                f"helper_reject {{\"job\":{job.n},\"edit\":{e['n']},\"why\":\"<what is wrong>\"}}. Job {job.n} waits for your decision.")
    agent.log(f"helper: job {job.n} proposes edit {e['n']} to {e['path']}")
    with LOCK:
        while job.decision is None:
            if job.cancelled:
                raise Cancel()
            LOCK.wait(1)
        said = job.decision
    _take_slot(job)
    return _h() + said


# ---------- recent progress (after each of the Assistant's tool calls) ----------
_PQ = {}          # chat id -> queue of calls to write entries for, in order
_PQ_LOCK = threading.Lock()


def _target(name, a):
    """What a call was about, for its label: #7 view git/Memory-Vault/ui_chat.py."""
    a = a if isinstance(a, dict) else {}
    if name == "search":
        t = f'"{a.get("text", "")}" in {a.get("where") or "all"}'
    elif name == "grep":
        t = f'"{a.get("pattern", "")}" in {a.get("path", "")}'
    elif name == "web_search":
        t = f'"{a.get("query") or a.get("text") or ""}"'
    elif name == "web_open":
        t = str(a.get("url", ""))
    elif name in ("run", "status"):
        t = str(a.get("repo", ""))
    elif name == "propose":
        p = a.get("paths") or a.get("path") or []
        t = ", ".join(map(str, p)) if isinstance(p, list) else str(p)
    elif name in ("copy", "rename", "move_note"):
        t = str(a.get("from", ""))
    elif name == "chats":
        t = str(a.get("id", ""))
    elif name in ("helper_cancel", "helper_approve", "helper_reject"):
        t = f"job {a.get('job', '')}" + (f" edit {a['edit']}" if "edit" in a else "")
    else:
        t = str(a.get("path", ""))
    t = " ".join(t.split())
    return t[:120] + ("..." if len(t) > 120 else "")


def label(n, name, args):
    t = _target(name, args)
    return f"#{n} {name}" + (f" {t}" if t else "")


def note_call(m, n, name, args, result, failed):
    """The Assistant's call number n ran: the Helper writes its progress entry, in the background and in order."""
    if not on():
        return
    import ui_chat
    cid = ui_chat.chat_of(m)
    item = {"cid": cid, "mid": m["id"], "text": m["text"], "label": label(n, name, args),
            "args": args, "result": str(result), "failed": failed}
    with _PQ_LOCK:
        q = _PQ.get(cid)
        if q is None:
            q = _PQ[cid] = queue.Queue()
            threading.Thread(target=_progress_loop, args=(q,), daemon=True, name=f"helper-progress-{cid}").start()
    q.put(item)


def progress_done(cid, wait=None):
    """Wait until every progress entry asked for in this chat is written (or given up). wait: most seconds, None for no limit."""
    with _PQ_LOCK:
        q = _PQ.get(cid)
    if q is None:
        return
    end = None if wait is None else time.time() + wait
    while q.unfinished_tasks and (end is None or time.time() < end):
        time.sleep(0.5)


def _progress_loop(q):
    while True:
        item = q.get()
        try:
            _progress(item)
        except Exception as e:
            agent.log(f"chat memory: the progress entry for {item['label']} was not written ({type(e).__name__}: {str(e)[:160]})")
        finally:
            q.task_done()


def _cut(text, room):
    """text if it fits in room characters, else its start and a note of how much is shown."""
    if len(text) <= room:
        return text
    room = max(0, room)
    return text[:room] + f"\n(the result is {len(text):,} characters; the first {room:,} are shown)"


def _progress(item):
    import chatmemory
    import settings
    import thinklog
    h = _h()
    lab = item["label"]
    room = (slot_tokens() - reserve("progress") - 300) * 2.2 - len(PROGRESS_SYS) - 400
    sofar = chatmemory.progress_entries(item["cid"], item["mid"])
    shown, left = [], room / 3
    for x in reversed(sofar):   # the newest entries that fit in a third of the room
        if len(x) + 3 > left:
            break
        shown.insert(0, x)
        left -= len(x) + 3
    hidden = len(sofar) - len(shown)
    person = item["text"] if len(item["text"]) <= room / 6 else item["text"][:int(room / 6)] + f"\n(the message is {len(item['text']):,} characters; the first {int(room / 6):,} are shown)"
    head = (f"PERSON'S MESSAGE:\n{person}\n\nPROGRESS SO FAR:\n"
            + (f"({hidden} earlier entries are not shown here, to fit)\n" if hidden else "")
            + ("\n".join(f"- {x}" for x in shown) or "(none yet)")
            + f"\n\nNEWEST TOOL CALL: {lab}\nARGUMENTS: {json.dumps(item['args'], ensure_ascii=False, default=str)}\n"
            + f"RESULT ({'it did not work' if item['failed'] else 'it worked'}):\n")
    if room - len(head) < 200:
        agent.log(f"chat memory: the progress entry for {lab} was not written: one Helper slot ({slot_tokens():,} tokens) "
                  "has no room for the call. Raise Context on the Helper's card, or lower its Slots.")
        return
    msgs = [{"role": "user", "content": head + _cut(item["result"], int(room - len(head)))}]
    lim = _calls()
    while True:
        try:
            text = _call(PROGRESS_SYS, msgs, "progress", fill={"{LABEL}": lab}, cancel=lambda: not on())
        except thinklog.Aborted:
            agent.log(f"chat memory: the progress entry for {lab} was not written: the Helper was turned off or switched")
            return
        entry, wrong = _check_entry(text, lab, lim)
        if entry:
            chatmemory.add_progress(item["cid"], item["mid"], entry)
            return
        agent.log(f"chat memory: sent the progress entry for {lab} back: {' '.join(wrong.split())[:160]}")
        msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": h + wrong}]


def _check_entry(text, lab, lim):
    """-> (the entry, '') or ('', what is wrong with it, for the Helper)."""
    s = re.sub(r"<think>.*?</think>", "", str(text), flags=re.DOTALL).strip()
    try:
        d = json.loads(s)
    except ValueError as e:
        return "", (f'that entry was not saved, because your reply is not valid JSON ({e}). Reply with ONLY '
                    f'{{"entry":"{lab}: <what it did and what came of it>"}}.')
    if not isinstance(d, dict) or set(d) != {"entry"} or not isinstance(d["entry"], str):
        return "", (f'that entry was not saved, because your reply must be exactly one JSON object with the one key "entry" '
                    f'holding text: {{"entry":"{lab}: <what it did and what came of it>"}}.')
    e = d["entry"].strip()
    if not e.startswith(lab + ": "):
        return "", (f'that entry was not saved, because it must start with "{lab}: " exactly, and it starts with '
                    f'"{e[:len(lab) + 2]}". Send it again with the label exactly as given.')
    if len(e) > lim:
        return "", f"that entry was not saved, because it is {len(e):,} characters and the limit is {lim}. Say it more briefly."
    if not e[len(lab) + 2:].strip():
        return "", f'that entry was not saved, because nothing follows the label. Say what the call did and what came of it.'
    return e, ""


# ---------- the chat memory, after each answer ----------
_FOLDING = set()
_FOLD_LOCK = threading.Lock()


def after_answer(m):
    """The Assistant answered m: the Helper writes that message (and any other not yet in the summary) into the chat's
    summary, in the background. Nothing happens when the Helper cannot work: the Assistant's own compression covers it."""
    if not on():
        return
    import ui_chat
    fold_soon(ui_chat.chat_of(m))


def fold_soon(cid):
    with _FOLD_LOCK:
        if cid in _FOLDING:
            return
        _FOLDING.add(cid)

    def go():
        try:
            progress_done(cid)
            import chatmemory
            chatmemory.fold_with_helper(cid)
        except Exception as e:
            agent.log(f"chat memory: the Helper could not write chat {cid} into its summary ({type(e).__name__}: {str(e)[:200]})")
        finally:
            with _FOLD_LOCK:
                _FOLDING.discard(cid)
    threading.Thread(target=go, daemon=True, name=f"helper-fold-{cid}").start()


def folding(cid):
    with _FOLD_LOCK:
        return cid in _FOLDING
