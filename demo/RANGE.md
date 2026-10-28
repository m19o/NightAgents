# Hosting the Demo for Many Attendees (Range Guide)

One shared Slack channel for 20+ attendees is a bad demo: everyone sees everyone's incidents and one person's approval lands on another's handoff. Isolation in NightAgents is **deployment-level** - every lab stack is fully parameterized by its own `.env`, fleet, incidents, and Slack channel, so the answer is multiple small labs, not one big one.

## Recommended model: per-squad labs

Squads of 4–5 attendees share one lab VM and one Slack channel; attendees rotate the operator role inside their squad.

| Attendees | VMs (8 GB each) | Slack channels |
|---|---|---|
| 10 | 2–3 | 2–3 |
| 20 | 4–5 | 4–5 |
| 40 | 8–10 | 8–10 |

Per-attendee VMs are the premium version (best isolation, 2–3× the cost) - same recipe.

Why not one shared stack with per-attendee Slack lanes: the beacon fleet would still be shared, so everyone watches the same incident anyway. Deployment isolation is what makes each squad's experience *theirs*.

## What the host provisions (Ludus / BadLabSector)

One Linux VM per squad: Docker + docker compose, 8 GB RAM, 4 vCPU, outbound internet (image pulls, OpenAI). Mythic inside each VM (poseidon agent + http C2 profile on host port 80, operation `Operation Chimera`) - or one shared Mythic with per-squad operations if RAM is tight (rename the operation per squad and pass it to the driver).

Post-deploy task per VM (Ludus ansible / cloud-init):

```bash
git clone <tactic-package-repo> /opt/nightagents && cd /opt/nightagents   # built by demo/ludus/package-tactic.sh
cp .env.example .env
# host injects: POSTGRES_PASSWORD, all signing secrets, OPENAI key (or Ollama),
# Mythic admin creds in ~/mythic-install/.env
docker compose --env-file .env up -d --build
bash demo/scripts/lab_provision.sh     # payload + fleet + implant, ~5 min first time
```

Resource rules per VM: 8–10 GB RAM cap, add the swapfile (`fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`), cloud models (OpenAI) so no LLM RAM is needed.

## What each squad does (day of)

1. Join their squad's Slack channel on their phones.
2. One command per squad (or the host pre-runs it): `python demo/scripts/slack_setup.py --bot-token <squad bot> --signing-secret <secret> --channel <squad channel>` (One Slack app per squad, or one app with per-squad channels - both work; the channel id is what routes messages.)
3. Work the labs in `demo/LABS.md` - rotate the operator each lab so everyone clicks Approve at least once.

## Stage/demo vs hands-on

- **Stage talk**: one shared workspace is fine - you present, the room watches one queue.
- **Tactic / hands-on**: squads. Never put 20 people in one channel.

## Verification before the event

Per VM, once: `python demo/scripts/run_demo.py --blue-only` completes with a handoff → Slack approval → execution (completed or rolled_back, both are designed outcomes), and `curl -s localhost:8000/api/v2/audit/verify` shows a valid chain. Then the VM is attendee-ready. Mythic tokens expire ~4 h: cron `run_demo.py --setup-only` every 3 h.
