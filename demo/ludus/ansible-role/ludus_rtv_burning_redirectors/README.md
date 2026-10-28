# Ansible Role: `ludus_rtv_burning_redirectors` ([Ludus](https://ludus.cloud))

An Ansible role that deploys the **NightAgents redirector-detection teaching lab** on a Debian 12 Ludus VM: the NightAgents control plane (API, worker, Elastic, Postgres, five MCP servers), the four-technique nginx redirector fleet, a victim container with a Poseidon implant beaconing through it, the simulated third-party intel feed, and squad Slack delivery — everything the Red Team Village labs in `demo/LABS.md` need.

> The role deploys the **tactic package** (curated teaching slice), not the full project.

## Requirements

- Debian 12 (bookworm) VM, 8 GB RAM / 4 vCPU recommended
- Outbound internet (image pulls, OpenAI API)
- A Mythic C2 reachable from the VM (shared Mythic VM or in-range install) with the poseidon agent, the http C2 profile on port 80, and an operation per squad
- Per squad: Slack app bot token (`chat:write`), app signing secret, channel id, and an OpenAI key (or an alternative OpenAI-compatible model endpoint)

## Role Variables

See `defaults/main.yml` for the full list. Required per squad:

```yaml
ludus_rtv_burning_redirectors_model_api_key: "sk-..."              # OpenAI key for the model roles
ludus_rtv_burning_redirectors_slack_bot_token: "xoxb-..."
ludus_rtv_burning_redirectors_slack_signing_secret: "..."
ludus_rtv_burning_redirectors_slack_app_token: "xapp-..."          # Socket Mode approvals, no public URL
ludus_rtv_burning_redirectors_slack_channel: "C0..."
```

Common overrides:

```yaml
ludus_rtv_burning_redirectors_repo_url: "https://github.com/m19o/nightagents.git"
ludus_rtv_burning_redirectors_squad: "squad-02"
ludus_rtv_burning_redirectors_mythic_url: "https://10.50.50.10:7443"
ludus_rtv_burning_redirectors_detection_interval: 120
ludus_rtv_burning_redirectors_swap_gb: 4
```

## Example Ludus Range Config

```yaml
ludus:
  - vm_name: "{{ range_id }}-squad-01-lab"
    hostname: "{{ range_id }}-squad-01-lab"
    template: debian-12-x64-server-template
    vlan: 51
    ip_last_octet: 10
    ram_gb: 8
    cpus: 4
    roles:
      - nightagents-group.nightagents
    role_vars:
      ludus_rtv_burning_redirectors_squad: "squad-01"
      ludus_rtv_burning_redirectors_model_api_key: "{{ lookup('env','NIGHTAGENTS_OPENAI_KEY') }}"
      ludus_rtv_burning_redirectors_slack_bot_token: "{{ lookup('env','NIGHTAGENTS_SLACK_BOT') }}"
      ludus_rtv_burning_redirectors_slack_signing_secret: "{{ lookup('env','NIGHTAGENTS_SLACK_SECRET') }}"
      ludus_rtv_burning_redirectors_slack_app_token: "{{ lookup('env','NIGHTAGENTS_SLACK_APP') }}"
      ludus_rtv_burning_redirectors_slack_channel: "C0ABCDE123"
      ludus_rtv_burning_redirectors_mythic_url: "https://10.50.50.10:7443"
```

Add the role to your Ludus server:

```bash
ludus ansible role add -d demo/ludus/ansible-role/nightagents
# or from the tactic repo directly:
ludus ansible role add <tactic-repo-url>/demo/ludus/ansible-role/nightagents
```

## What the role does (idempotent)

1. Installs docker, git, python3; enables docker
2. Adds a swapfile (freeze protection) and persists it in fstab
3. Clones the tactic package and renders `.env` from squad variables (fresh random secrets per VM)
4. Builds and tags the NightAgents images, starts the control plane, waits for readiness
5. Provisions the lab: Poseidon payload (cached after first build), redirector fleet, victim implant, Mythic callback verification
6. Verifies Slack delivery with a live test message
7. Installs a 3-hourly cron refreshing the Mythic API token

Attendees then run: `python3 demo/scripts/run_demo.py --blue-only` — the loop (hunt → investigate → Slack approval → execution) is otherwise autonomous (`ludus_rtv_burning_redirectors_detection_interval` seconds).

## License

MIT

## Author

[m19o](https://github.com/m19o), for the Red Team Village.
