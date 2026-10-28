# NightAgents Teaching Labs - Red Team Village Tactic

Six hands-on labs. Attendees do the work; NightAgents proves the point. Every lab has a learning objective, exact commands, what success looks like, and the takeaway.

**Setup per attendee group**: range owner runs `bash demo/scripts/lab_provision.sh` once. Attendees need: a terminal, the repo, and the operator Slack channel. Everything runs in an authorized local lab.

---

## Lab 1 - Catch the metronome (beacon fingerprinting)

**Learn**: how blue teams find C2 without any malware analysis - behavior, not signatures.

**Do**:
```bash
# Look at what the defender sees (redirector access logs in Elastic)
curl -s 'http://127.0.0.1:9200/logs-redirector-demo/_search?pretty&size=5' | head -40
# Count requests per client
curl -s 'http://127.0.0.1:9200/logs-redirector-demo/_search' -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {"clients": {"terms": {"field": "source.ip", "size": 5},
    "aggs": {"paths": {"terms": {"field": "url.path", "size": 5}}}}}'
```

**Observe**: one IP, one User-Agent, the same two URIs, every ~10 seconds. Then check what NightAgents did with it:
```bash
docker exec nightagents-postgres-1 psql -U nightagents -c \
  "select external_id, status, summary from incidents order by received_at desc limit 3;"
```

**Success**: you found the rhythm by hand before seeing the `auto-*` incident. **Takeaway**: humans don't browse like metronomes. The implant hides its address, not its rhythm.

---

## Lab 2 - Play the scanner (why redirection works)

**Learn**: the four redirection techniques by being the "blue team scanner" yourself.

**Do** (from the demo fleet - fetch each redirector as an outsider):
```bash
# 1. pass-through: everything forwarded (noisy, burns fast)
docker exec redirector-pass wget -qO- http://localhost:8080/ | head -3

# 2. filter: you are not the implant -> decoy
docker exec redirector-filter wget -qO- --server-response http://localhost:8080/ 2>&1 | grep -i '302\|location'
# ...now pretend to be the implant (right UA + URI):
docker exec redirector-filter wget -qO- --server-response \
  --header='User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) NightAgentsDemo' \
  http://localhost:8080/index 2>&1 | grep -i 'HTTP/'

# 3. vhost hiding: same redirector, two identities
docker exec redirector-front wget -qO- --header='Host: weather-front.demo' http://localhost:8080/ | head -2
docker exec redirector-front wget -qO- --header='Host: cdn-metrics.demo' http://localhost:8080/ | head -2

# 4. A/B paths: cover content vs C2 on one domain
docker exec redirector-ab wget -qO- http://localhost:8080/static/style.css | head -2
```

**Success**: the scanner in you got a weather page; the implant in you got the C2. **Takeaway**: the implant carries proof-of-shape (UA + URIs). Everyone else gets theater.

---

## Lab 3 - The world is watching (third-party fingerprinting)

**Learn**: blue teams don't need to touch your infrastructure - third parties fingerprint it for them.

**Do**: nothing but watch. The monitor polls the intel feed (lab-simulated VT/Shodan/CT/abuse) and opens incidents:
```bash
docker exec nightagents-postgres-1 psql -U nightagents -c \
  "select external_id, severity, summary from incidents where external_id like 'intel-%'
   order by received_at desc limit 5;"
```

**Observe**: four-plus `intel-*` incidents - VirusTotal flag, Shodan profile, CT log entry, abuse complaint - plus Slack alerts. Note each carries provenance (`thirdparty:<feed>#...`).

**Success**: you can name all five ways blue teams catch redirectors - rhythm plus these four. **Takeaway**: exposure happens out in the world's datasets. You can't hide from indexes; you can only watch them back and respond first.

---

## Lab 4 - Be the operator (approve and move real traffic)

**Learn**: the full response cycle and what human-in-the-loop actually means.

**Do**:
```bash
# 1. Trigger the loop fresh (or wait for the autonomous hunter)
docker exec nightagents-postgres-1 psql -U nightagents -c \
  "update incidents set status='resolved' where external_id like 'auto-%' and status in ('received','investigating');"
# 2. Wait for the Slack handoff message, then click Approve (or approve via the API)
# 3. Watch the beacons change redirector:
watch -n 2 'curl -s "http://127.0.0.1:9200/logs-redirector-demo/_search" -H "Content-Type: application/json" -d "{\"size\":0,\"aggs\":{\"r\":{\"terms\":{\"field\":\"host.name\",\"size\":5}}}}"'
# 4. Check the execution state
docker exec nightagents-postgres-1 psql -U nightagents -c \
  "select state, created_at::timestamp(0) from executions order by created_at desc limit 1;"
```

**Success**: the `host.name` counts shift from `redirector-pass` to `redirector-filter`, and the execution says `completed`. **Takeaway**: the AI investigated, the kernel moved the traffic, but the decision to act was yours. AI proposes, code decides, humans authorize.

---

## Lab 5 - When it says no (unknown is never success)

**Learn**: why the platform refuses to claim wins it can't prove - and why that's the feature.

**Do** (break the evidence chain, then run the response again):
```bash
# Stop the telemetry feed and try to execute - NightWatcher will refuse to judge
docker stop nightagents-demo-shipper-1
# Or let the Mythic token age out - execution goes failed_safe, never "success"
docker exec nightagents-postgres-1 psql -U nightagents -c \
  "select state, plan_json->>'reason' from executions order by created_at desc limit 3;"
# Restore when done:
docker start nightagents-demo-shipper-1
```

**Observe**: `request_evidence`, `abstain`, `failed_safe`, or `rolled_back` - never a false `completed`.

**Takeaway**: negative evidence counts only from healthy sources, and unknown continuity means rollback. A tool that lies to you is worse than no tool.

---

## Lab 6 - Break the detector (think like an adversary, learn like a defender)

**Learn**: detection limits, honestly. (Defensive purpose only - we do not build evasion tooling here.)

**Do**:
```bash
# Ask: what happens if the beacon gets slower? The detector needs >=4 requests in 15 min.
# Discuss: how would YOU tune the fingerprint to catch slower beacons?
# Look at the actual heuristic and propose one improvement:
grep -n 'threshold\|max_distinct_uris' src/nightagents/detection.py | head -5
```

**Success**: the group articulates one detection weakness and one defense against it (longer lookback, interval-variance analysis, JA4 on the redirector's TLS...).

**Takeaway**: every detector has a floor. Knowing where yours is - that's the job.

---

## The ops lesson (the point of the whole tactic)

Red teams rotate redirectors by hand today: someone gets an abuse ticket, logs into a VPS, edits DNS at 2 AM. NightAgents turns that into a **monitored, evidence-driven, human-approved, audited operation** - the same discipline blue teams have, pointed at red infrastructure. The implant keeps beaconed sessions alive; the platform keeps the operator honest.
