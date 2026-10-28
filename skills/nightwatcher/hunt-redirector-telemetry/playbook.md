# Redirector Telemetry Hunting Playbook

Guidance knowledge for the NightWatcher hunting agent. These are the patterns
worth hunting in redirector access telemetry, distilled from field detection
work. The agent decides what to query and how to correlate; this playbook only
informs that judgment. Every finding must cite elastic evidence ids - a pattern
you cannot point at in the data is not a finding.

## Path probing

- **Invalid path probes**: requests for paths that were never part of the
  redirector's served set. Outsiders guess; beacons do not.
- **C2 path probes**: requests matching common C2 framework URIs (known
  profile paths, default stager endpoints) that our profile does not use.
- **Error page probes**: requests deliberately aimed at missing resources to
  compare 404/403 responses against known redirector software defaults.
- **Favicon pulls**: standalone /favicon.ico requests - fingerprinting tools
  hash favicons to match infrastructure across domains.

## Scanner fingerprints

- **Suspicious user agents**: nikto, masscan, nmap, dirbuster, gobuster,
  sqlmap, w3af, wpscan, acunetix, zap, and similar tooling strings.
- **Scanner services**: censys, binaryedge, rapid7, shodan-style fetch
  patterns - regular, shallow, no referrer, no follow-up resources.
- **TLS fingerprint scans**: connection patterns consistent with JARM/JA3/JA4
  collection - handshake-only or single-request visits from research networks.
- **HEAD-only scans**: repeated HEAD requests where real clients use GET.
- **Suspicious headers**: proxy header injection attempts (X-Forwarded-For
  games, unexpected Via), header order that does not match a real browser.

## Volume and rhythm anomalies

- **Scan volume spikes**: request rates from a single source far above the
  redirector's baseline.
- **Subnet sweeps**: sequential or clustered source IPs from one prefix
  touching the same paths.
- **Low-and-slow scans**: sparse requests spread over hours, designed to stay
  under rate thresholds - correlate over long windows.
- **Repeated 404s / 403s**: enumeration behavior - an outsider walking the
  path space and collecting denials.

## Infrastructure and provider signals

- **Cloud provider probes**: requests sourced from cloud/VPS ranges where the
  expected client population is not cloud-hosted.
- **WAF block spikes**: the edge provider blocking at unusual rates - someone
  else's tooling is bouncing off.
- **TLS anomalies**: handshake failures, version downgrades, cipher mismatch
  patterns consistent with probing rather than clients.
- **Geo anomalies**: requests from regions outside the expected audience.
- **Unexpected methods**: OPTIONS/TRACE/PUT where only GET/POST are served.
- **DNS blocking signals**: resolver-level denials observed upstream - the
  domain may be getting categorized.
- **Health check failures**: the redirector failing its own checks - could be
  an attack in progress, could be a takedown already starting.
- **Callback drops**: established callbacks going quiet while the redirector
  stays up - the path between beacon and C2 may be getting cut.

## Intelligence-driven patterns

- **Reputation hits**: the redirector appearing on blocklists, VirusTotal
  flags, or URLhaus-style feeds.
- **Threat intel submissions**: the domain or IP appearing in sandbox
  submissions or scanning queues - someone submitted us.
- **Provider abuse investigations**: abuse desk tickets or provider
  notifications about the redirector.
- **Operator reports**: a human flagged something - treat as a lead, verify
  against telemetry before acting.

## Correlation

- **Infrastructure correlation**: the same source touching multiple
  redirectors of the fleet - mapping behavior, the strongest pre-burn signal.
- **Fingerprint matches**: observed artifacts (JARM, cert, favicon hash,
  header order) matching previously registered malicious fingerprints.

## Judgment rules

- One odd request is noise; rhythm, repetition, and correlation across
  redirectors are signal.
- Absence of telemetry from a healthy source is a valid quiet result, not a
  finding. Absence from an unhealthy source is unknown - say so, never act.
- Prefer one well-evidenced finding over five speculative ones.
