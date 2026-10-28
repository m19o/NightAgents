# Deploying NightAgents labs on Ludus

End-to-end flow for the range host (Ludus / BadLabSector). See `demo/RANGE.md` for the squad model and `demo/ludus/range.yml` for the VM definitions.

## 1. Prepare

- Build the **tactic package** (Ludus gets this, NOT the full project): `bash demo/ludus/package-tactic.sh` → push `dist/nightagents-tactic/` to a small dedicated repo (or drop the tarball on the VM image). The full project stays private.
- Per squad: a Slack app with a bot token (`chat:write`), its app-level token (`xapp-...`, `connections:write` - drives Socket Mode approvals with no public URL on the lab VM), its signing secret (only if you use the HTTP webhook path instead), and a squad channel id (see `demo/GUIDE.md` §5). One app can serve all squads - route by channel id.
- OpenAI key(s) for the model roles (cloud = zero LLM RAM on the lab VMs).

## 2. Define the range

```bash
ludus range config set -f demo/ludus/range.yml
ludus range deploy
```

One shared Mythic VM (8 GB) + one 8 GB lab VM per squad of 4–5 attendees. Scale by copying squad blocks. Create one Mythic operation per squad (`Operation Chimera squad-01`, …) so callback inventories stay isolated.

## 3. Provision each lab VM

**Primary path - the Ansible role** (`demo/ludus/ansible-role/ludus_rtv_burning_redirectors/`, follows the
[Ludus role template](https://github.com/badsectorlabs/ludus_ansible_role_template)):

```bash
ludus ansible role add -d demo/ludus/ansible-role/ludus_rtv_burning_redirectors
# then reference it per squad VM in the range config with role_vars (see the role README)
```

The role is idempotent: packages, swapfile, clone, templated `.env`, build, stack up, lab provisioning, Slack delivery check, Mythic token cron. It ships inside the tactic package, so the "choose your tactic" web flow can apply it to each squad VM directly.

**Fallback - the shell script** (`demo/ludus/provision-lab.sh`) with the squad env:

Run `demo/ludus/provision-lab.sh` as the post-deploy step (ansible shell task, or manually once per VM) with the squad's env:

```bash
REPO_URL=git@github.com:<you>/nightagents.git \
SQUAD=squad-01 \
OPENAI_KEY=sk-... \
SLACK_BOT_TOKEN=xoxb-... \
SLACK_SIGNING_SECRET=... \
SLACK_CHANNEL=C0... \
MYTHIC_HOST=10.50.50.10 \
bash demo/ludus/provision-lab.sh
```

The script is idempotent: base packages, swapfile, repo, `.env` (fresh random secrets + squad config), image build, stack up, `lab_provision.sh`, and a live Slack delivery check. It ends with `LAB READY`.

## 4. Verify per squad

```bash
python3 demo/scripts/run_demo.py --blue-only     # completes: handoff -> Slack -> execution
curl -s http://127.0.0.1:8000/api/v2/audit/verify
```

Then cron the Mythic token refresh on every lab VM:

```cron
17 */3 * * * cd /opt/nightagents && python3 demo/scripts/run_demo.py --setup-only >> /var/log/na-refresh.log 2>&1
```

## 5. Day of the event

- Attendees get: their squad's VM (SSH or Ludus console) and their squad's Slack channel.
- Instructor runs groups through `demo/LABS.md`; each squad runs `python3 demo/scripts/run_demo.py --blue-only` per lab cycle.
- Reset between groups: `docker exec nightagents-postgres-1 psql -U nightagents -c \ "update incidents set status='resolved' where external_id like 'auto-%';"`

## Notes

- Field names in `range.yml` follow the common Ludus schema - validate against your Ludus version's docs before deploy (`ludus range config get`).
- The NightAgents API binds loopback on each lab VM; approvals travel via Slack, so attendees need nothing but Slack on their phones.
- If RAM is tight on the Proxmox host: shared Mythic (already in `range.yml`), cloud models, 8 GB per lab VM, and the provision script's swapfile are what keep it stable.
