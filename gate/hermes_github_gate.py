#!/usr/bin/env python3
"""Hermes webhook route script: decide whether a GitHub event should wake the bot, and answer fast.

Reads the GitHub webhook payload on stdin. Prints [SILENT] to drop the event (already fully handled
or not for the bot), or the payload plus a `bot` object (the route prompt templates against it) as JSON
so Hermes starts a Sonnet agent run.

Configured by hermes_github_gate.json next to this file (see config.example.json).

Wakes the bot only when the sender is owner_login and one of:
  - issue or PR assigned to BOT, or review requested from BOT
  - @<mention> (or /hermes) in a comment, inline review comment, review summary, or a new or edited description
  - any comment by owner_login in a thread the bot is already part of (assigned, reviewer, or commented before)

Then, in parallel: Haiku writes an instant reply, Jev Router picks the follow-up tier (none, sonnet, opus).
See README.md for the full flow.
"""
import json
import os
from pathlib import Path
import re
import sys
import concurrent.futures
import time
import urllib.request

CONFIG_FILE = Path(os.environ.get("HERMES_GATE_CONFIG") or Path(__file__).with_name("hermes_github_gate.json"))
DEFAULTS = {
    "bot_login": None,            # GitHub login of the bot: an account, or a GitHub App's "<slug>[bot]"
    "mention": None,              # handle people type after @ (default: bot_login without "[bot]")
    "bot_name": "Hermes",         # persona name used in prompts
    "owner_login": None,          # the only user who can wake the bot
    "token_file": "/root/.config/gh-bot/token",   # bot token on the Hermes host (app installation token or PAT)
    "hermes_env": "/root/.hermes/.env",           # holds OPENROUTER_API_KEY
    "router_log": "/root/.hermes/logs/github-router.jsonl",
    "router_model": "typesafe/jev-router",
    "instant_model": "anthropic/claude-haiku-4.5",
}
CFG = {**DEFAULTS, **(json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {})}

BOT = CFG["bot_login"] or ""
HANDLE = CFG["mention"] or BOT.removesuffix("[bot]")
OWNER = CFG["owner_login"] or ""
NAME = CFG["bot_name"]
TOKEN_FILE = CFG["token_file"]
HERMES_ENV = CFG["hermes_env"]
ROUTER_LOG = CFG["router_log"]
MENTION = (re.compile(rf"(?<![\w-])@{re.escape(HANDLE)}(?:\[bot\])?(?![\w-])|(?<![\w/])/hermes\b", re.IGNORECASE)
           if HANDLE else re.compile(r"(?!x)x"))
ACK_REACTIONS = ("heart", "+1", "hooray")


# Hermes kills route scripts after 30s (script_timeout_seconds) and then DROPS the event, so the gate
# must always answer well inside that. Every network call gets a timeout cut from this budget.
START = time.monotonic()
BUDGET_S = 24.0


def remaining() -> float:
    return BUDGET_S - (time.monotonic() - START)


def _exit(code: int = 0):
    """Exit without waiting on stuck worker threads (a hung HTTP call must not hold the script open)."""
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(code)


def silent(reason: str):
    print(f"gate: drop ({reason})", file=sys.stderr)
    print("[SILENT]")
    _exit(0)


def mentions(text) -> bool:
    return bool(text) and bool(MENTION.search(text))


def gh_get(path: str):
    token = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ctx-gate"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def thread_context(repo: str, number: int, skip_id=None) -> str:
    """Last few conversation comments so follow-ups make sense without extra agent calls."""
    try:
        comments = gh_get(f"/repos/{repo}/issues/{number}/comments?per_page=100")
    except Exception as e:
        print(f"gate: thread fetch failed: {e}", file=sys.stderr)
        return "(thread unavailable)"
    lines = []
    for c in [c for c in comments if c.get("id") != skip_id][-8:]:
        body = (c.get("body") or "").strip().replace("\r", "")
        lines.append(f"@{c['user']['login']}: {body[:700]}")
    return "\n\n".join(lines) or "(no earlier comments)"


def react(path: str):
    try:
        token = open(TOKEN_FILE).read().strip()
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            data=json.dumps({"content": "eyes"}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ctx-gate",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"gate: 👀 {r.status} {path}", file=sys.stderr)
        return True
    except Exception as e:  # never block the agent run on a failed reaction
        print(f"gate: reaction failed {path}: {e}", file=sys.stderr)
        return False


ROUTER_MODEL = CFG["router_model"]
INSTANT_MODEL = CFG["instant_model"]

CONTEXT_NOTE = f"""{NAME} is an AI teammate on GitHub (user {BOT}). The repo owner ({OWNER}) just wrote to them."""

ROUTER_PROMPT = CONTEXT_NOTE + """
Another fast model already posts an instant reply (an answer from general knowledge, or a one-liner saying
what will be checked). YOUR job: decide whether real follow-up work is needed, and on which model.

- "none": the instant reply fully covers it (thanks/ok/praise, general-knowledge questions, opinions, quick
  explanations that need no repo access).
- "sonnet": needs repo/tool access but bounded: read a few files, check a diff or CI, answer a question about
  this codebase, a small fix, a normal-size PR review.
- "opus": heavy: multi-file implementation, deep debugging, architecture/design decisions, research across
  sources, large PR review, anything risky or long.
If unsure, pick the higher tier. Assignments and review requests are never "none".
Anything about THIS repo, PR, or issue (merge readiness, status, code, tests, CI) is never "none".
Questions about BOTNAME's own access or abilities are never "none" (the agent answers them with facts).
If the trigger says "no tag" and the message is clearly not for BOTNAME (a note to self, talking to someone
else), pick "none".

Return ONLY JSON: {"tier": "none|sonnet|opus", "why": "<10 words"}"""

INSTANT_PROMPT = CONTEXT_NOTE + """
You write BOTNAME's INSTANT reply, posted within seconds, speaking AS BOTNAME.

What BOTNAME can do: BOTNAME has full access to this repository. Right after your reply, a follow-up agent
can read the code and diff, run gh and git, check CI, review, push branches, and open PRs. YOU personally
only see the message and the thread below, so you never answer code/PR/CI questions yourself: you hand
them to the follow-up agent.

NEVER say or imply that BOTNAME lacks access, can't see the repo, or needs things pasted. That is false.
If the owner asks about BOTNAME's access or abilities, answer truthfully: yes, BOTNAME has repo access and
is checking. Never claim the owner pasted or shared something unless it's literally in the thread.

- Pure acknowledgement that expects nothing back (thanks, nice, ok, lgtm, praise): no text, just a reaction.
- Anything that expects an answer, INCLUDING greetings ("hi", "hey", "are you there?"): reply in text.
  For a bare greeting, a short friendly line that invites the next step, e.g. "Hey! What do you need on this PR?"
- When in doubt between reacting and replying, reply.
- If the trigger says "no tag": The owner wrote in a thread BOTNAME is part of without tagging them. If the message is
  clearly NOT for BOTNAME (a note to self, a reminder, talking to a teammate), set "ignore": true and nothing else.
  If it continues the conversation with BOTNAME or asks something, treat it normally.
- Answerable from the message, the thread, or general knowledge: answer it, concisely.
- Needs the codebase, the PR diff/status, CI, logs, research, or real work (e.g. "is this ready to merge?",
  "why does X fail?", "review this"): write ONE short line saying what you're checking, set "followup": true
  (e.g. "Checking the diff, tests, and merge state now."). Never guess about the code. Never invent results.

Style: direct, concise, contractions, no em dashes, markdown OK, no greetings, no "Great question".
Return ONLY JSON: {"reply": "markdown or null", "reaction": "heart|+1|hooray|null", "followup": true if your reply promises more work, "ignore": true only if the message is not for BOTNAME}"""


# persona name is configurable; prompts use the BOTNAME placeholder
ROUTER_PROMPT = ROUTER_PROMPT.replace("BOTNAME", NAME)
INSTANT_PROMPT = INSTANT_PROMPT.replace("BOTNAME", NAME)

def llm_json(model: str, system: str, user: str, max_tokens: int, timeout: int) -> dict:
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user[:12000]}]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {openrouter_key()}", "content-type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    txt = d["choices"][0]["message"]["content"]
    r = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    r["_model"] = d.get("model")
    return r


def describe(ctx: dict) -> str:
    return (f"Repo: {ctx['repo']}\n{ctx['target_type']} #{ctx['number']}: {ctx['title']}\n"
            f"Trigger: {ctx['kind']}\n{ctx.get('context','')}\n\nThread (oldest first):\n{ctx.get('thread','')}\n\n"
            f"New message from the owner:\n{ctx['text']}")


def openrouter_key():
    for line in open(HERMES_ENV):
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("no OPENROUTER_API_KEY")


def route(ctx: dict) -> dict:
    err, r = None, None
    for attempt in range(2):
        timeout = min(10.0, remaining() - 4)
        if timeout < 2:
            err = err or "no time left in budget"
            break
        try:
            r = llm_json(ROUTER_MODEL, ROUTER_PROMPT, describe(ctx), 800, timeout)
            r["router_model"] = r.pop("_model", None)
            if attempt:
                r["why"] = (r.get("why") or "") + " (retry)"
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:200]
            print(f"gate: router attempt {attempt + 1} failed: {err}", file=sys.stderr)
    else:
        return {"tier": "sonnet", "why": "router unavailable", "error": err}
    if r is None:
        return {"tier": "sonnet", "why": "router out of time", "error": err}
    if r.get("tier") not in ("none", "sonnet", "opus"):
        r["tier"] = "sonnet"
    if ctx["kind"] in ("assigned to you", "review requested from you") and r["tier"] == "none":
        r["tier"] = "sonnet"
    return r


def instant(ctx: dict) -> dict:
    try:
        r = llm_json(INSTANT_MODEL, INSTANT_PROMPT, describe(ctx), 700, max(2.0, min(10.0, remaining() - 4)))
        return r
    except Exception as e:
        print(f"gate: instant reply failed: {e}", file=sys.stderr)
        return {"reply": None, "reaction": None, "followup": True}


def gh_post(path: str, payload: dict):
    token = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(f"https://api.github.com{path}", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ctx-gate"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def react_with(path: str, content: str):
    try:
        gh_post(path, {"content": content})
    except Exception as e:
        print(f"gate: reaction {content} failed: {e}", file=sys.stderr)


def reply_path(ctx: dict) -> str:
    if ctx.get("review_comment_id"):
        return f"/repos/{ctx['repo']}/pulls/{ctx['number']}/comments/{ctx['review_comment_id']}/replies"
    return f"/repos/{ctx['repo']}/issues/{ctx['number']}/comments"


def log_route(ctx: dict, r: dict, dt: float):
    try:
        with open(ROUTER_LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), "repo": ctx["repo"], "n": ctx["number"], "kind": ctx["kind"],
                                "tier": r.get("tier"), "why": r.get("why"), "router_model": r.get("router_model"),
                                "router_s": round(dt, 2), "error": r.get("error"), "text": ctx["text"][:200]}) + "\n")
    except Exception:
        pass


OTHER_MENTION = re.compile(rf"(?<![\w-])@(?!{re.escape(HANDLE)}(?![\w-]))[A-Za-z0-9][A-Za-z0-9-]*")


def addressed_elsewhere(text: str) -> bool:
    """The owner @-mentions someone else and not the bot: they're talking to that person, stay out."""
    return bool(text) and not mentions(text) and bool(OTHER_MENTION.search(text))


def engaged(repo: str, number: int, obj: dict, is_pr: bool) -> bool:
    """the bot is part of this thread: assigned, review requested, or has commented before."""
    if any((u or {}).get("login") == BOT for u in obj.get("assignees") or []):
        return True
    if any((u or {}).get("login") == BOT for u in obj.get("requested_reviewers") or []):
        return True
    try:
        if any(c["user"]["login"] == BOT for c in gh_get(f"/repos/{repo}/issues/{number}/comments?per_page=100")):
            return True
        if is_pr:
            if any(c["user"]["login"] == BOT for c in gh_get(f"/repos/{repo}/pulls/{number}/comments?per_page=100")):
                return True
            if any((r.get("user") or {}).get("login") == BOT for r in gh_get(f"/repos/{repo}/pulls/{number}/reviews?per_page=100")):
                return True
    except Exception as e:
        print(f"gate: engagement check failed: {e}", file=sys.stderr)
    return False


def wake_kind(repo: str, number: int, obj: dict, is_pr: bool, text: str, where: str) -> str:
    if mentions(text):
        return f"mention in {where}"
    if addressed_elsewhere(text):
        silent(f"{where} addressed to someone else")
    if engaged(repo, number, obj, is_pr):
        return f"follow-up in a thread you're part of ({where}, no tag)"
    silent(f"{where} without mention in unwatched thread")


NO_ACCESS = re.compile(
    r"(do(?:n't| not)|can(?:'t|not)|unable to|without)\s+(?:have\s+)?(?:any\s+)?(?:access|see|view|read|open)"
    r"|(?:no|without)\s+(?:any\s+)?(?:repo(?:sitory)?\s+|code\s+)?access"
    r"|paste (?:the|it|code|your)|share (?:the|what) (?:code|diff|relevant)",
    re.IGNORECASE)


def disclaims_access(reply: str) -> bool:
    """Instant replies must never claim the bot can't see the repo. If one does, drop it and escalate."""
    return bool(reply) and bool(NO_ACCESS.search(reply))


def decide(inst: dict, route_result: dict, posted: bool) -> str:
    """Merge the instant reply and the router verdict into one outcome: ignore, none, sonnet, or opus.

    - Both agree the message isn't for the bot -> ignore (no reaction, no reply).
    - Router says none, but the instant reply promised work, or nothing usable came back -> sonnet.
    """
    tier = route_result["tier"]
    if inst.get("ignore") is True and not posted and tier == "none":
        return "ignore"
    if tier == "none" and (inst.get("followup") or (not posted and inst.get("reaction") not in ACK_REACTIONS)):
        return "sonnet"
    return tier


def main():
    p = json.load(sys.stdin)
    if not (BOT and OWNER):
        silent(f"not configured: create {CONFIG_FILE.name} with bot_login and owner_login")
    if (p.get("sender") or {}).get("login") != OWNER:
        silent("sender not owner")
    action = p.get("action")
    repo = (p.get("repository") or {}).get("full_name")
    if not repo:
        silent("no repo")

    ctx = {"repo": repo, "context": ""}

    if "comment" in p and "issue" in p:  # issue_comment (issue or PR conversation)
        if action != "created":
            silent("comment not created")
        issue, c = p["issue"], p["comment"]
        kind = wake_kind(repo, issue["number"], issue, bool(issue.get("pull_request")), c.get("body") or "", "comment")
        ctx.update(kind=kind, number=issue["number"], title=issue["title"],
                   target_type="PR" if issue.get("pull_request") else "issue",
                   url=c["html_url"], text=c.get("body") or "")
        ctx["react_path"] = f"/repos/{repo}/issues/comments/{c['id']}/reactions"
        ctx["reacted"] = react(ctx["react_path"]) if "no tag" not in ctx["kind"] else False
        ctx["thread"] = thread_context(repo, issue["number"], skip_id=c["id"])

    elif "comment" in p and "pull_request" in p:  # pull_request_review_comment (inline diff comment)
        if action != "created":
            silent("review comment not created")
        pr, c = p["pull_request"], p["comment"]
        kind = wake_kind(repo, pr["number"], pr, True, c.get("body") or "", "inline review comment")
        ctx.update(kind=kind, number=pr["number"], title=pr["title"],
                   target_type="PR", url=c["html_url"], text=c.get("body") or "",
                   context=(f"File: {c.get('path')} line {c.get('line') or c.get('original_line')}\n"
                            f"Comment id (reply with gh api repos/{repo}/pulls/{pr['number']}/comments/{c['id']}/replies): {c['id']}\n"
                            f"Diff hunk:\n{(c.get('diff_hunk') or '')[-1500:]}"))
        ctx["review_comment_id"] = c["id"]
        ctx["react_path"] = f"/repos/{repo}/pulls/comments/{c['id']}/reactions"
        ctx["reacted"] = react(ctx["react_path"]) if "no tag" not in ctx["kind"] else False
        ctx["thread"] = thread_context(repo, pr["number"])

    elif "review" in p and "pull_request" in p:  # pull_request_review (review summary body)
        if action != "submitted" or not (p["review"].get("body") or "").strip():
            silent("review without body")
        pr, rv = p["pull_request"], p["review"]
        kind = wake_kind(repo, pr["number"], pr, True, rv.get("body") or "", "review summary")
        ctx.update(kind=kind, number=pr["number"], title=pr["title"],
                   target_type="PR", url=rv["html_url"], text=rv.get("body") or "",
                   context=f"Review state: {rv.get('state')}")
        # Reviews have no reactions API; react on the PR itself.
        ctx["react_path"] = f"/repos/{repo}/issues/{pr['number']}/reactions"
        ctx["reacted"] = react(ctx["react_path"]) if "no tag" not in ctx["kind"] else False
        ctx["thread"] = thread_context(repo, pr["number"])

    elif "pull_request" in p or "issue" in p:  # pull_request / issues lifecycle events
        is_pr = "pull_request" in p
        obj = p["pull_request"] if is_pr else p["issue"]
        body = obj.get("body") or ""
        if action == "assigned" and (p.get("assignee") or {}).get("login") == BOT:
            kind = "assigned to you"
        elif action == "review_requested" and (p.get("requested_reviewer") or {}).get("login") == BOT:
            kind = "review requested from you"
        elif action == "opened" and mentions(body):
            kind = "mention in description"
        elif action == "edited" and mentions(body) and not mentions(
                ((p.get("changes") or {}).get("body") or {}).get("from")):
            kind = "mention added to description"
        else:
            silent(f"{'pr' if is_pr else 'issue'} action {action} not for ctx")
        ctx.update(kind=kind, number=obj["number"], title=obj["title"],
                   target_type="PR" if is_pr else "issue", url=obj["html_url"], text=body)
        if is_pr:
            ctx["context"] = f"Branch: {obj['head']['ref']} -> {obj['base']['ref']}"
        ctx["react_path"] = f"/repos/{repo}/issues/{obj['number']}/reactions"
        ctx["reacted"] = react(ctx["react_path"]) if "no tag" not in ctx["kind"] else False
        ctx["thread"] = thread_context(repo, obj["number"]) if action != "opened" else "(new)"

    else:
        silent("unsupported event shape")

    ctx["text"] = ctx["text"][:6000]
    t0 = time.time()
    ex = concurrent.futures.ThreadPoolExecutor(2)
    f_route, f_inst = ex.submit(route, ctx), ex.submit(instant, ctx)
    try:
        inst = f_inst.result(timeout=max(1.0, remaining() - 3))
    except concurrent.futures.TimeoutError:
        inst = {"reply": None, "reaction": None, "followup": True}
    reply = (inst.get("reply") or "").strip() if isinstance(inst.get("reply"), str) else ""
    if reply.lower() in ("null", "none"):
        reply = ""
    reply = re.sub(r"\s*\u2014\s*", ", ", reply)  # house style: no em dashes
    posted = False
    if disclaims_access(reply):
        print(f"gate: dropped instant reply that disclaims access: {reply[:120]!r}", file=sys.stderr)
        reply, inst = "", dict(inst, followup=True)
    if reply:
        try:
            gh_post(reply_path(ctx), {"body": reply})
            posted = True
        except Exception as e:
            print(f"gate: instant post failed: {e}", file=sys.stderr)
    t_inst = time.time() - t0
    try:
        r = f_route.result(timeout=max(1.0, remaining() - 2))
    except concurrent.futures.TimeoutError:
        r = {"tier": "sonnet", "why": "router too slow, sending to agent", "error": "router timeout"}
    ex.shutdown(wait=False, cancel_futures=True)
    outcome = decide(inst, r, posted)
    if outcome == "ignore":
        log_route(ctx, dict(r, tier="ignored"), time.time() - t0)
        silent("not addressed to ctx")
    if not ctx.get("reacted"):
        ctx["reacted"] = react(ctx["react_path"])
    if outcome != r["tier"]:
        r["tier"], r["why"] = outcome, (r.get("why") or "") + " (instant reply promised follow-up)"
    log_route(ctx, r, time.time() - t0)
    ctx["tier"], ctx["why"] = r["tier"], r.get("why") or ""
    ctx["instant_reply"] = reply if posted else "(none posted)"
    print(f"gate: instant in {t_inst:.1f}s posted={posted}; tier={r['tier']} via {r.get('router_model')} "
          f"total {time.time()-t0:.1f}s ({ctx['why']})", file=sys.stderr)
    if r["tier"] == "none":
        if posted:
            react_with(ctx["react_path"], "rocket")
        elif inst.get("reaction") in ACK_REACTIONS:
            react_with(ctx["react_path"], inst["reaction"])
        silent("handled instantly")

    p["bot"] = ctx
    print(json.dumps(p))
    _exit(0)


if __name__ == "__main__":
    main()
