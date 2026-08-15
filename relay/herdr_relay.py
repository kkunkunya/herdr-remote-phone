#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio, json, logging, os, re, shutil, signal, socket, subprocess, time

from agent_state import complete_agent_update_message

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from logging.handlers import RotatingFileHandler
import sys

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = os.environ.get("HERDR_BIN") or shutil.which("herdr") or "/opt/homebrew/bin/herdr"
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
POLL_INTERVAL = 2
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Optional: shared secret for relay auth

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
CHROME_RE = re.compile(
    r"^[\s─━═_—│|◔◑◕●\s]+$"
    r"|Kiro\s[·•]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[◔◑◕●]\s+(Shell|Bash)"
)

clients = set()
last_statuses = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
known_panes = set()
agent_cache = {}

SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "C-c", "Up", "Down", "Left", "Right", "BSpace"} | {
    str(number) for number in range(10)
}

# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


# --- Web Push helpers ---
def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False):
    """Send push notification to all registered subscriptions.
    
    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()

_load_push_subs()


def run_herdr_result(*args, remote=None):
    if remote:
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, HERDR, *args]
    else:
        cmd = [HERDR, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def run_herdr(*args, remote=None):
    try:
        return run_herdr_result(*args, remote=remote).stdout.strip()
    except Exception:
        return ""


def get_tab_labels(remote=None):
    """Map tab_id -> user-facing tab label from herdr tab list."""
    raw = run_herdr("tab", "list", remote=remote)
    labels = {}
    try:
        data = json.loads(raw)
        for t in data.get("result", {}).get("tabs", []):
            tid = t.get("tab_id")
            if not tid:
                continue
            label = (t.get("label") or "").strip()
            if label:
                labels[tid] = label
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
    return labels

def get_workspace_labels(remote=None):
    """Map workspace_id -> (label, agent_status) from herdr workspace list."""
    raw = run_herdr("workspace", "list", remote=remote)
    labels = {}
    try:
        data = json.loads(raw)
        for w in data.get("result", {}).get("workspaces", []):
            wid = w.get("workspace_id")
            if not wid:
                continue
            label = (w.get("label") or "").strip()
            labels[wid] = {
                "label": label,
                "status": w.get("agent_status") or "unknown",
            }
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
    return labels

def get_agents_from_host(remote=None):
    raw = run_herdr("pane", "list", remote=remote)
    host_label = remote or "local"
    tab_labels = get_tab_labels(remote=remote)
    ws_info = get_workspace_labels(remote=remote)
    try:
        data = json.loads(raw)
        panes = data.get("result", {}).get("panes", [])
        agents = []
        agent_ws = set()
        for p in panes:
            ws_id = p.get("workspace_id", "") or ""
            if not p.get("agent"):
                continue
            agent_ws.add(ws_id)
            tab_id = p.get("tab_id", "") or ""
            tab_label = tab_labels.get(tab_id, "")
            # Prefer explicit pane label, else tab name, else empty (UI falls back to project)
            pane_label = (p.get("label") or "").strip()
            agents.append({
                "pane_id": p["pane_id"],
                "agent": p.get("agent", ""),
                "label": pane_label or tab_label,
                "tab_label": tab_label,
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": ws_id,
                "workspace_label": ws_info.get(ws_id, {}).get("label", ""),
                "tab_id": tab_id,
            })
        # One placeholder entry per agent-less tab so empty/new tabs show
        # up on the phone (mirroring herdr) and can be opened to type
        # commands. Workspaces where every tab has an agent are unaffected.
        seen_tabs = set()
        for p in panes:
            if p.get("agent"):
                continue
            tab_id = p.get("tab_id", "") or ""
            if tab_id in seen_tabs:
                continue
            seen_tabs.add(tab_id)
            ws_id = p.get("workspace_id", "") or ""
            ws = ws_info.get(ws_id, {})
            wl = ws.get("label", "")
            tl = tab_labels.get(tab_id, "")
            agents.append({
                "pane_id": p["pane_id"],
                "agent": "",
                "label": tl or wl,
                "tab_label": tl,
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": wl or os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": ws_id,
                "workspace_label": wl,
                "tab_id": tab_id,
            })
        return agents
    except (json.JSONDecodeError, KeyError):
        return []


def get_all_agents():
    agents = get_agents_from_host(remote=None)
    for remote in REMOTES:
        agents.extend(get_agents_from_host(remote=remote))
    return agents


def read_pane(pane_id, remote=None):
    raw = run_herdr("pane", "read", pane_id, "--lines", "50", "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    return "\n".join(lines[-20:])


def detect_options(text):
    lower = text.lower()
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower:
        return SUBAGENT_OPTIONS
    return None


INBOX_PATH = os.path.expanduser("~/.local/state/herdr-remote/inbox.json")
INBOX_MAX = 200


def _load_inbox():
    try:
        with open(INBOX_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        item for item in data
        if isinstance(item, dict) and re.fullmatch(r"in\d+", str(item.get("id", "")))
    ]


def _save_inbox(inbox):
    path = os.path.abspath(INBOX_PATH)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(inbox, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        log.exception("Failed to persist inbox")
        try:
            os.unlink(tmp)
        except OSError:
            pass


INBOX = _load_inbox()
_INBOX_SEQ = max((int(item["id"][2:]) for item in INBOX), default=0)


def inbox_add(pane_id, agent, project, host, prompt, item_type="blocked"):
    """Append an inbox item for a pane. Blocked items start pending; done
    items land directly in the resolved (已完成) section."""
    global _INBOX_SEQ
    if item_type != "done":
        for item in INBOX:
            if item.get("pane_id") == pane_id and not item.get("resolved"):
                return None
    _INBOX_SEQ += 1
    resolved = item_type == "done"
    item = {
        "id": f"in{_INBOX_SEQ}",
        "pane_id": pane_id,
        "agent": agent,
        "project": project,
        "host": host,
        "prompt": (prompt or "")[:500],
        "ts": time.time(),
        "type": item_type,
        "resolved": resolved,
        "resolved_ts": time.time() if resolved else None,
    }
    INBOX.append(item)
    if len(INBOX) > INBOX_MAX:
        del INBOX[: len(INBOX) - INBOX_MAX]
    _save_inbox(INBOX)
    return item


def inbox_resolve_pane(pane_id):
    """Mark all pending items of a pane resolved (agent got unblocked)."""
    changed = False
    for item in INBOX:
        if item.get("pane_id") == pane_id and not item.get("resolved"):
            item["resolved"] = True
            item["resolved_ts"] = time.time()
            changed = True
    if changed:
        _save_inbox(INBOX)
    return changed


def parse_questionnaire(content):
    """Detect a pi questionnaire UI in pane content.

    Render format (single question):
        ──...
        <prompt>
        1. option one
        2. option two
        ↑↓ navigate • Enter select • Esc cancel
        ──...
    Multi-question adds a tab bar above and a Submit tab with
    "Ready to submit" / "Press Enter to submit".
    Returns {"prompt", "options", "submit"} or None.
    """
    if not content:
        return None
    if "navigate" not in content and "Ready to submit" not in content and "Enter to submit" not in content:
        return None
    lines = content.splitlines()
    opts = []
    prompt_lines = []
    in_q = False
    for line in lines:
        t = line.strip()
        if not t:
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", t)
        if m and not in_q:
            in_q = True
            opts.append([int(m.group(1)), m.group(2).strip()])
            continue
        if in_q:
            if m:
                opts.append([int(m.group(1)), m.group(2).strip()])
                continue
            if ("navigate" in t or "Enter to submit" in t or "Unanswered" in t
                    or t.startswith("\u2500") or t.startswith("Ready")):
                break
            # description lines or the "> " cursor row: ignore for v1
            continue
        prompt_lines.append(line)
    # Submit tab has no numbered options — recognize it by its banner.
    if any(k in content for k in ("Ready to submit", "Press Enter to submit", "Unanswered:")):
        if not opts:
            return {"prompt": "Ready to submit", "options": [], "submit": True}
    if len(opts) < 2:
        return None
    # strip the top rule line and multi-question tab bar from the prompt
    clean = []
    for line in prompt_lines:
        t = line.strip()
        if not t or t.startswith("\u2500") or t.startswith("←") or t.startswith("→"):
            continue
        clean.append(line)
    options = [{"index": i, "label": label.rstrip()} for i, label in opts]
    prompt = "\n".join(clean).strip()
    submit = any(k in content for k in ("Ready to submit", "Press Enter to submit", "Unanswered:"))
    return {"prompt": prompt[:300], "options": options, "submit": submit}


async def report_done(pane_id, agent, project, host):
    """Completion notification: push + inbox record (resolved section)."""
    item = inbox_add(pane_id, agent, project, host, "", item_type="done")
    if item:
        await broadcast({"type": "inbox_update", "item": item})
    await send_web_push(
        title=f"✅ {project} 完成",
        body=f"{agent or 'agent'} · {pane_id}",
        url=f"/?pane={pane_id}",
    )


async def report_blocked(pane_id, agent, project, host, content, remote=None):
    """Broadcast blocked/questionnaire/inbox for a newly-blocked pane."""
    options = detect_options(content)
    await broadcast({
        "type": "blocked", "pane_id": pane_id,
        "agent": agent, "project": project,
        "host": host,
        "prompt": (content or "")[:500],
        "options": options or TOOL_OPTIONS
    })
    q = parse_questionnaire(content)
    if q:
        q["pane_id"] = pane_id
        q["agent"] = agent
        q["project"] = project
        q["host"] = host
        await broadcast({"type": "questionnaire", **q})
    item = inbox_add(pane_id, agent, project, host, content)
    if item:
        await broadcast({"type": "inbox_update", "item": item})
    await send_web_push(
        title=f"🐑 {project} blocked",
        body=(content or "")[:120],
        url=f"/?pane={pane_id}",
    )


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    for ws in list(clients):
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once():
        agents = get_all_agents()
        # Always broadcast (even empty list) so clients stay in sync
        for a in agents:
            pane_remote_map[a["pane_id"]] = a.get("remote")
            known_panes.add(a["pane_id"])
            agent_cache[a["pane_id"]] = a
        await broadcast({"type": "agents", "agents": agents})
        for a in agents:
            pid, status = a["pane_id"], a["status"]
            if status == "blocked" and last_statuses.get(pid) != "blocked":
                content = read_pane(pid, remote=a.get("remote"))
                await report_blocked(pid, a["agent"], a["project"], a.get("host", "local"), content, remote=a.get("remote"))
            # Send clear push when agent unblocks, and resolve its inbox item
            if status != "blocked" and last_statuses.get(pid) == "blocked":
                await send_web_push("", "", clear=True)
                await broadcast({"type": "questionnaire_clear", "pane_id": pid})
                if inbox_resolve_pane(pid):
                    await broadcast({"type": "inbox_resolve", "pane_id": pid})
            # Completion notification: working → idle/done (subagents run
            # inside the same pane, so they never flip the pane state)
            if status in ("idle", "done") and last_statuses.get(pid) == "working":
                await report_done(pid, a["agent"], a["project"], a.get("host", "local"))
            last_statuses[pid] = status
        await broadcast({"type": "inbox", "items": INBOX})
        # Clean up panes that are no longer reported
        current_pane_ids = {a["pane_id"] for a in agents}
        stale = known_panes - current_pane_ids
        if stale:
            known_panes.difference_update(stale)
            for pid in stale:
                pane_remote_map.pop(pid, None)
                last_statuses.pop(pid, None)
                agent_cache.pop(pid, None)


async def event_push():
    while True:
        event = await event_queue.get()
        pane_id = event.get("pane_id", "")
        update = None
        if pane_id and event.get("type") == "agent_event":
            update = complete_agent_update_message(
                event,
                current=agent_cache.get(pane_id),
                local_hostname=socket.gethostname(),
            )
            if update is None:
                continue
        agent_data = update["agent"] if update else event
        status = agent_data.get("status", "")
        host = agent_data.get("host", "local")

        if status == "blocked" and pane_id:
            remote = pane_remote_map.get(pane_id)
            if remote or host == "local":
                content = read_pane(pane_id, remote=remote)
            else:
                content = event.get("prompt", "Agent is blocked")
            await report_blocked(pane_id, agent_data.get("agent", ""), agent_data.get("project", ""), host, content, remote=remote)
            last_statuses[pane_id] = "blocked"
        elif status != "blocked" and last_statuses.get(pane_id) == "blocked":
            # event-driven unblock: clear push + resolve inbox
            await send_web_push("", "", clear=True)
            await broadcast({"type": "questionnaire_clear", "pane_id": pane_id})
            if inbox_resolve_pane(pane_id):
                await broadcast({"type": "inbox_resolve", "pane_id": pane_id})
            last_statuses[pane_id] = status
        elif status in ("idle", "done") and last_statuses.get(pane_id) == "working":
            # event-driven completion: push + inbox record
            await report_done(pane_id, agent_data.get("agent", ""), agent_data.get("project", ""), host)
            last_statuses[pane_id] = status

        if update:
            known_panes.add(pane_id)
            pane_remote_map.setdefault(pane_id, None)
            agent_cache[pane_id] = {**agent_cache.get(pane_id, {}), **update["agent"]}
            last_statuses[pane_id] = status
            await broadcast(update)


def _request_token(request):
    token = None
    for key, value in request.headers.raw_items():
        if key.lower() == "authorization":
            token = value.replace("Bearer ", "")
    if not token and "token=" in (request.path or ""):
        import urllib.parse
        _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
        params = urllib.parse.parse_qs(qs)
        token = params.get("token", [None])[0]
    return token


async def process_request(connection, request):
    """Handle HTTP POST on the same port as WebSocket."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    # Check if this is a WebSocket upgrade — requires valid token
    upgrade = None
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
    if upgrade == "websocket":
        if AUTH_TOKEN and _request_token(request) != AUTH_TOKEN:
            headers = Headers([("Content-Type", "text/plain")])
            return Response(401, "Unauthorized", headers, b"Invalid token\n")
        return None  # proceed with WebSocket handshake

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # ⚠ EVENT PUSH MUST BE HANDLED FIRST — ORDER IS LOAD-BEARING.
    # A pushed event arrives as `?d=<urlencoded json>` on ANY path.
    # The README shows POST to :8375 without naming a path, so `/` is common.
    # Every static route below `return`s, so if reached first the event is
    # dropped while caller still gets 200. Add new static routes BELOW, never above.
    import urllib.parse
    if "?" in (request.path or ""):
        _, qs = (request.path or "").split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            if AUTH_TOKEN and _request_token(request) != AUTH_TOKEN:
                headers = Headers([("Content-Type", "text/plain")])
                return Response(401, "Unauthorized", headers, b"Invalid token\n")
            try:
                event = json.loads(params["d"][0])  # parse_qs already decodes
                event_queue.put_nowait(event)
                log.debug("push: received event type=%s", event.get("type", "unknown"))
            except Exception as e:
                log.warning("push: unparseable event payload (%d bytes): %s", len(params["d"][0]), e)
            headers = Headers([("Access-Control-Allow-Origin", "*")])
            return Response(200, "OK", headers, b"ok\n")

    path = (request.path or "/").split("?")[0]

    # A cheap process-level health signal for launchd/guards. Keep it public:
    # it reveals no agent data and remains usable if auth config drifts.
    if path == "/healthz":
        headers = Headers([
            ("Content-Type", "text/plain"),
            ("Cache-Control", "no-store"),
        ])
        return Response(200, "OK", headers, b"ok\n")

    # Serve web app for GET / or GET /index.html
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        sw_path = os.path.join(web_dir, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        svg_path = os.path.join(web_dir, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    # Fallback for unmatched paths
    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(404, "Not Found", headers, b"not found\n")


async def handle_client(ws):
    remote_addr = ws.remote_address
    ip = remote_addr[0] if remote_addr else "unknown"
    ua = ws.request.headers.get("User-Agent", "unknown") if ws.request else "unknown"
    origin = ws.request.headers.get("Origin", "") if ws.request else ""

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    connected_at = time.monotonic()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if text.strip().lower() not in SAFE_RESPONSES:
                    await ws.send(json.dumps({"type": "error", "message": "response not in allowlist"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                run_herdr("pane", "send-text", pane_id, text + "\n", remote=remote)
            elif msg_type == "agent_event":
                event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                lines = msg.get("lines", "30")
                remote = pane_remote_map.get(pane_id)
                content = run_herdr("pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                await ws.send(json.dumps({"type": "pane_content", "pane_id": pane_id, "content": content}))
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                keys = msg.get("keys", [])
                if not all(k in SAFE_KEYS for k in keys):
                    await ws.send(json.dumps({"type": "error", "message": "keys contain disallowed values"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                try:
                    result = run_herdr_result("pane", "send-keys", pane_id, *keys, remote=remote)
                except Exception as e:
                    log.warning("send_keys command failed for pane %s: %s", pane_id, e)
                    await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                    continue
                if result.returncode != 0:
                    log.warning("send_keys command failed for pane %s with exit %s", pane_id, result.returncode)
                    await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                    continue
                await ws.send(json.dumps({"type": "command_result", "command": "send_keys", "ok": True}))
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                run_herdr("pane", "send-text", pane_id, text, remote=remote)
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    run_herdr("tab", "create", "--workspace", workspace_id, "--focus")
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            elif msg_type == "questionnaire_answer":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                idx = msg.get("index")
                submit = bool(msg.get("submit"))
                keys = []
                if submit:
                    keys = ["Enter"]
                else:
                    # pi questionnaire is cursor+Enter driven: jump to top with
                    # a burst of Up (clamped), then Down to the target, Enter.
                    if isinstance(idx, int) and idx >= 1:
                        keys = ["Up"] * 20 + ["Down"] * (idx - 1) + ["Enter"]
                if not keys:
                    await ws.send(json.dumps({"type": "error", "message": "invalid questionnaire answer"}))
                    continue
                log.info("Questionnaire answer from %s (%s): pane=%s index=%s submit=%s", ip, device, pane_id, idx, submit)
                audit("questionnaire_answer", ip, device, pane_id, f"index={idx} submit={submit}")
                result = run_herdr_result("pane", "send-keys", pane_id, *keys, remote=remote)
                if result.returncode != 0:
                    await ws.send(json.dumps({"type": "error", "message": "questionnaire answer failed"}))
                    continue
                await broadcast({"type": "questionnaire_clear", "pane_id": pane_id})
                await ws.send(json.dumps({"type": "command_result", "command": "questionnaire_answer", "ok": True}))
            elif msg_type == "inbox_resolve":
                item_id = msg.get("id", "")
                for item in INBOX:
                    if item.get("id") == item_id and not item.get("resolved"):
                        item["resolved"] = True
                        item["resolved_ts"] = time.time()
                        _save_inbox(INBOX)
                        await broadcast({"type": "inbox_update", "item": item})
                        await ws.send(json.dumps({"type": "command_result", "command": "inbox_resolve", "ok": True}))
                        break
                else:
                    await ws.send(json.dumps({"type": "error", "message": "inbox item not found"}))
            elif msg_type == "inbox_clear":
                # clear all resolved items
                before = len(INBOX)
                INBOX[:] = [i for i in INBOX if not i.get("resolved")]
                if len(INBOX) != before:
                    _save_inbox(INBOX)
                    await broadcast({"type": "inbox", "items": INBOX})
                    await ws.send(json.dumps({"type": "command_result", "command": "inbox_clear", "ok": True}))
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event_queue.put_nowait(json.loads(data.decode()))
        except Exception:
            pass


def start_mdns():
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        zc.register_service(info, allow_name_change=True)
        log.info("mDNS registered at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


async def main():
    zc, info = start_mdns()
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(UDPPlugin, local_addr=("127.0.0.1", 8376))
    except OSError:
        log.warning("UDP 8376 in use, plugin push disabled")
    asyncio.create_task(poll_loop())
    asyncio.create_task(event_push())
    server = await serve(handle_client, "0.0.0.0", WS_PORT, process_request=process_request)
    hosts = ["local"] + REMOTES
    log.info("herdr-remote relay on :%d (WebSocket + HTTP POST)", WS_PORT)
    log.info("Polling: %s", ", ".join(hosts))
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    await stop
    server.close()
    await server.wait_closed()
    if zc and info:
        try:
            zc.unregister_service(info)
        except Exception as e:
            log.warning("mDNS unregister failed: %s", e)
        finally:
            zc.close()


if __name__ == "__main__":
    asyncio.run(main())
