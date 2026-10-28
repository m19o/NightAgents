#!/usr/bin/env python3
"""BYO-Slack setup for NightAgents (attendee/presenter use).

Connects a NightAgents deployment to any Slack workspace in ~30 seconds:

    python demo/scripts/slack_setup.py \
        --bot-token xoxb-... --signing-secret <app signing secret> --channel <name-or-id>

What it does:
  1. validates the bot token against auth.test
  2. resolves the channel (tries conversations.list; falls back to the raw id/name)
  3. posts a test message so you SEE it working in your workspace
  4. writes NIGHTAGENTS_SLACK_* into the repo .env (never printed back)
  5. recreates the api container so the change takes effect

Interactivity (Approve/Reject buttons) needs a public Request URL, configured in
the Slack app itself: Interactivity & Shortcuts -> On ->
  https://<your-public-host>/api/v2/slack/interactions
(cloudflared tunnel --url http://127.0.0.1:8000 gives you a public URL for a laptop.)
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def slack_api(method: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload or {}).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}"}


def patch_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-token", required=True, help="xoxb-... bot token with chat:write")
    parser.add_argument("--signing-secret", required=True, help="Slack app signing secret (Basic Information)")
    parser.add_argument("--channel", required=True, help="channel id (C...) or user id (U...) for DMs")
    parser.add_argument("--no-restart", action="store_true", help="only write .env, do not recreate the api container")
    args = parser.parse_args()

    print("-> validating bot token (auth.test)")
    auth = slack_api("auth.test", args.bot_token)
    if not auth.get("ok"):
        sys.exit(f"FAIL: token rejected: {auth.get('error')}")
    print(f"   workspace: {auth.get('team')}  bot user: {auth.get('user')}")

    channel = args.channel
    print(f"-> posting test message to {channel}")
    posted = slack_api(
        "chat.postMessage",
        args.bot_token,
        {
            "channel": channel,
            "text": ":white_check_mark: NightAgents is now connected to this workspace. "
            "Approval requests and execution outcomes will land here.",
        },
    )
    if not posted.get("ok"):
        sys.exit(
            f"FAIL: could not post to {channel}: {posted.get('error')}\n"
            "hint: pass a channel ID (C...) or user ID (U...); for channels, /invite the bot first"
        )
    print(f"   delivered to {posted.get('channel')}")

    print("-> writing .env")
    patch_env(
        {
            "NIGHTAGENTS_SLACK_API_TOKEN": args.bot_token,
            "NIGHTAGENTS_SLACK_CHANNEL": posted.get("channel", channel),
            "NIGHTAGENTS_SLACK_SIGNING_SECRET": args.signing_secret,
        }
    )

    if not args.no_restart:
        print("-> recreating the api container")
        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "up", "-d", "api"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            sys.exit(f"FAIL: docker compose failed:\n{result.stderr}")
    print(
        "DONE. Buttons work once the app's Interactivity Request URL points at\n"
        "  https://<your-public-host>/api/v2/slack/interactions\n"
        "  (laptop: cloudflared tunnel --url http://127.0.0.1:8000)"
    )


if __name__ == "__main__":
    main()
