#!/usr/bin/env python3
"""Provision one Slack channel per attendee and print the engagement-channel map.

Usage:
    python demo/scripts/slack_channels.py --bot-token xoxb-... alice bob carol
    python demo/scripts/slack_channels.py --bot-token xoxb-... --count 12

Creates channels named attendee-<name> (or attendee-01..N), invites the bot,
and writes the resulting map to demo/work/slack_channels.json. Use it as
NIGHTAGENTS_SLACK_CHANNELS_JSON (engagement_id -> channel id) after mapping
each attendee to their engagement, or as-is for the squad VM's slack_setup.py.

Bot scope required: channels:manage (Slack app -> OAuth & Permissions -> add,
reinstall to workspace). chat:write alone cannot create channels.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_FILE = REPO_ROOT / "demo" / "work" / "slack_channels.json"


def slack_api(method: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-token", required=True)
    parser.add_argument("--count", type=int, default=0, help="create attendee-01..N channels")
    parser.add_argument("--prefix", default="attendee")
    parser.add_argument("names", nargs="*", help="attendee names for channel suffixes")
    args = parser.parse_args()

    names = list(args.names)
    if args.count and not names:
        names = [f"{i:02d}" for i in range(1, args.count + 1)]
    if not names:
        sys.exit("give attendee names or --count N")

    auth = slack_api("auth.test", args.bot_token, {})
    if not auth.get("ok"):
        sys.exit(f"FAIL: token rejected: {auth.get('error')}")
    print(f"workspace: {auth.get('team')} bot: {auth.get('user')}")

    result = {}
    for name in names:
        channel_name = f"{args.prefix}-{name}".lower().replace(" ", "-")[:80]
        created = slack_api("conversations.create", args.bot_token, {"name": channel_name})
        channel = (created.get("channel") or {}).get("id")
        if not channel:
            print(f"  {channel_name}: FAILED ({created.get('error')}) - channels:manage scope needed?")
            continue
        bot_user = auth.get("bot_id") or auth["user_id"]
        slack_api("conversations.invite", args.bot_token, {"channel": channel, "users": bot_user})
        result[name] = channel
        print(f"  {channel_name}: {channel}")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nwrote {OUT_FILE} ({len(result)} channels)")
    print("next: map each attendee's engagement to their channel in")
    print("NIGHTAGENTS_SLACK_CHANNELS_JSON (engagement_id -> channel id)")


if __name__ == "__main__":
    main()
