"""Observed actions through Browser Harness; one CDP session, no per-step subprocess.

Vendored from jev-ultrafast. Local changes (see UPSTREAM.md): attach to the
user's active tab instead of creating an owned one, a separate `navigate()`,
a blocked-field guard in the executor, tab following, and an `AsyncBrowser`
facade that keeps every synchronous CDP call on one worker thread.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import activate_tab, cdp, current_tab, list_tabs, switch_tab

from core.sensitive import BLOCKED_AUTOCOMPLETE, blocked_label, js_pattern

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:", "edge://")

# Plan §7.9: the policy is not trusted to avoid these; the executor refuses them.
# The vocabulary itself lives in core/sensitive.py so every layer shares one list.


# CDP tells us the attached tab is gone in several wordings; all mean the same
# thing: re-attach before doing anything else.
DEAD_SESSION = (
    "Session with given id not found",
    "No target with given id",
    "Target closed",
    "-32001",
    "not_attached",
    "cdp_disconnected",
)


def dead_session(error):
    text = str(error)
    return any(marker in text for marker in DEAD_SESSION)


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class BlockedField(ValueError):
    """A field the user must fill themselves; the agent never types into it."""


class Browser:
    """Synchronous CDP body (upstream). Call it through `AsyncBrowser`, not directly."""

    def __init__(self, url=None, *, open_in="current_tab", activate=True):
        ensure_daemon()
        self.open_in = open_in
        self.activate = activate
        self.target = None
        self.session = None
        self.tab_stack: list[str] = []
        self.after_input = None
        # Ownership is a fact we record, never inferred from configuration: only
        # a tab this object actually created may ever be closed.
        self.owned = False
        self.reattached = 0
        if open_in == "new_tab":
            self.open_tab(url or "about:blank")
        else:
            self.attach()
            if url:
                self.navigate(url)

    # ---------------------------------------------------------------- attach

    def attach(self):
        """Attach to Chrome's active tab; open a blank one if nothing usable is focused."""
        tab = None
        try:
            tab = current_tab()
        except Exception:
            tab = None
        live = {t["targetId"] for t in list_tabs()}
        if not tab or tab.get("targetId") not in live or not tab.get("url") or tab["url"].startswith(INTERNAL):
            usable = [
                t for t in list_tabs(include_chrome=False)
                if not t["url"].startswith(INTERNAL) and t["targetId"] in live
            ]
            tab = usable[-1] if usable else None
        if tab is None:
            return self.open_tab("about:blank")
        self.target = tab["targetId"]
        self.owned = False
        self.session = switch_tab(self.target)
        self.show()
        self.prepare()
        return self.target

    def open_tab(self, url):
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.owned = True
        self.session = switch_tab(self.target)
        self.show()
        # An owned background tab needs a viewport of its own; the user's tab already has one.
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        self.prepare()
        if url and url != "about:blank":
            self.navigate(url)
        return self.target

    def show(self):
        """Make the agent's tab the visible one.

        `current_tab()` returns the tab the *daemon* last attached to, which
        persists between runs and need not be the one in front of the user. We
        cannot cheaply detect Chrome's focused tab -- evaluating in every target
        takes ~2 s per background tab, and focus emulation makes `hasFocus()`
        lie -- so instead the agent makes its choice visible.
        """
        if not self.activate or not self.target:
            return
        try:
            activate_tab(self.target)
        except Exception:
            pass

    def prepare(self):
        # Keep rAF/menus rendering even when the tab is not the frontmost one.
        try:
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            pass

    def switch(self, target_id):
        self.target = target_id
        self.owned = False  # a tab the page opened is not ours to close
        self.session = switch_tab(target_id)
        self.show()
        self.prepare()
        return target_id

    def opened_tabs(self):
        """Targets this page opened (Plan §7.11.5). The user's other tabs are never touched."""
        try:
            infos = cdp("Target.getTargets")["targetInfos"]
        except Exception:
            return []
        return [
            t["targetId"]
            for t in infos
            if t.get("type") == "page" and t.get("openerId") == self.target and t["targetId"] != self.target
        ]

    def alive(self, target_id):
        try:
            return any(t["targetId"] == target_id for t in list_tabs())
        except Exception:
            return False

    def follow_new_tab(self):
        """Adopt a tab this page opened; returns (from, to) when the agent moved."""
        opened = self.opened_tabs()
        if not opened:
            return None
        previous = self.target
        self.tab_stack.append(previous)
        self.switch(opened[-1])
        return previous, self.target

    def return_to_opener(self):
        while self.tab_stack:
            previous = self.tab_stack.pop()
            if self.alive(previous):
                gone = self.target
                self.switch(previous)
                return gone, previous
        return None

    # -------------------------------------------------------------- plumbing

    def call(self, method, **params):
        """Re-attach once if the tab went away.

        Tabs close, Chrome restarts, the daemon reattaches elsewhere. A session
        id cached at startup is not valid forever, and the first symptom used to
        be a raw `-32001` out of `Page.navigate`.
        """
        try:
            return cdp(method, session_id=self.session, **params)
        except RuntimeError as error:
            if not dead_session(error):
                raise
            self.reattach()
            return cdp(method, session_id=self.session, **params)

    def reattach(self):
        """Recover from a tab that disappeared, by opening a fresh one.

        Deliberately *not* `attach()`: falling back to whatever tab the user
        happens to have open would navigate their reading away to the agent's
        next URL. The old page is gone either way, so take an empty tab of our
        own -- which `owned` then lets us clean up. Never re-runs an action;
        callers decide that.
        """
        previous, self.session = self.session, None
        self.open_tab("about:blank")
        self.reattached = getattr(self, "reattached", 0) + 1
        return previous, self.session

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def navigate(self, url, timeout=15):
        """URLs come from sites.yaml, the current tab, observed links, or the search template."""
        if not isinstance(url, str) or not url.startswith(("http://", "https://", "about:blank")):
            raise ValueError("Refusing to navigate to a non-http URL")
        self.after_input = None
        self.show()
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except StalePage:
                pass
            time.sleep(0.02)
        return url

    def observe(self, screenshot=False):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
            except RuntimeError as error:
                if not dead_session(error) or attempt > 1:
                    raise
                self.reattach()
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if action["kind"] == "fill" and blocked_label(action.get("label")):
            raise BlockedField(action.get("label", "this field"))
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        try:
            result = browser_operation(
                {"operation": "act", "session": self.session, "action": action, "text": text}
            )
        except RuntimeError as error:
            if not dead_session(error):
                raise
            # Never re-run a mutation: re-attach, then make the caller observe
            # and decide again rather than guessing whether this one landed.
            self.reattach()
            raise StalePage("The tab went away mid-action. Observe again.") from None
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        # The user's own tab is never closed; only a tab this agent created is.
        if self.target and self.owned:
            try:
                cdp("Target.closeTarget", targetId=self.target)
            except Exception:
                pass
        self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              if (action.kind==='fill') {
                const blockedTypes=['password','file','hidden'];
                const blockedAuto=""" + json.dumps(list(BLOCKED_AUTOCOMPLETE)) + """;
                const auto=(e.getAttribute('autocomplete')||'').toLowerCase();
                const hint=((e.getAttribute('name')||'')+' '+(e.id||'')+' '+
                  (e.getAttribute('placeholder')||'')+' '+(e.getAttribute('aria-label')||''));
                const blocked=new RegExp(""" + json.dumps(js_pattern()) + """,'i');
                if (blockedTypes.includes(e.type) || blockedAuto.includes(auto) || blocked.test(hint))
                  return {blocked:true};
              }
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if isinstance(target, dict) and target.get("blocked"):
                raise BlockedField(action.get("label", "this field"))
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", False):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info


class AsyncBrowser:
    """The `BrowserDriver` seam. Every CDP call runs on one thread, so ordering holds."""

    def __init__(self, browser=None, **kwargs):  # kwargs: open_in, activate
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cdp")
        self.browser = browser
        self._kwargs = kwargs

    async def _run(self, function, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, lambda: function(*args, **kwargs))

    async def start(self):
        if self.browser is None:
            self.browser = await self._run(Browser, None, **self._kwargs)
        return self.browser

    @property
    def target(self):
        return self.browser.target if self.browser else None

    async def observe(self, screenshot=False):
        return await self._run(self.browser.observe, screenshot=screenshot)

    async def fresh(self, page, action=None):
        return await self._run(self.browser.fresh, page, action)

    async def act(self, action, page, text=None):
        return await self._run(self.browser.act, action, page, text=text)

    async def navigate(self, url):
        return await self._run(self.browser.navigate, url)

    async def attach(self):
        return await self._run(self.browser.attach)

    async def follow_new_tab(self):
        return await self._run(self.browser.follow_new_tab)

    async def return_to_opener(self):
        return await self._run(self.browser.return_to_opener)

    async def close(self):
        if self.browser:
            await self._run(self.browser.close)
        self.pool.shutdown(wait=False)
