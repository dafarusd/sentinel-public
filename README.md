# Sentinel

**A passive, multi-modal RF sensing and pattern-of-life analysis platform for your own network and property.**

Sentinel is a self-hosted research tool that observes the radio environment around a fixed location — primarily Wi-Fi management traffic — and turns raw, ephemeral signals into a structured, queryable picture of *what devices are present, when, and how that changes over time*. It is built for security research, home-network situational awareness, and counter-surveillance experimentation on infrastructure you own or are explicitly authorized to monitor.

It does **not** decode payloads, crack encryption, or intercept the contents of communications. It works entirely from metadata that devices broadcast in the clear.

---

## ⚠️ Read this first: scope, ethics, and legality

Sentinel is a surveillance-adjacent tool. That is the point of building it carefully, and the point of using it responsibly.

**Intended use.** Sentinel is for research and education on **networks and physical spaces you own or have written authorization to monitor** — your own home, your own lab, your own equipment. Pattern-of-life analysis of people who have not consented is invasive regardless of whether the underlying frames are unencrypted.

**What it observes.** Devices continuously broadcast unencrypted Wi-Fi management frames (e.g. probe requests and beacons) that include hardware identifiers and other metadata. Sentinel passively records this metadata. It is a *receiver*. It does not transmit, inject, deauthenticate, or interfere with any network or device.

**Legal reality varies by jurisdiction.** Passive reception of openly broadcast RF metadata, logging device identifiers, and building behavioral profiles are governed by different and sometimes conflicting laws depending on where you are — wiretap and interception statutes, computer-misuse law, and data-protection/privacy regimes (e.g. GDPR-style rules treat persistent device tracking as personal-data processing). **You are responsible for understanding and complying with the law that applies to you.** This document is not legal advice and the author is not a lawyer.

**Hard "don'ts.”** Do not deploy Sentinel to monitor third parties, neighbors, public spaces, or any network you do not control. Do not use it to identify, locate, or track specific individuals without their informed consent. Do not combine its output with other data to deanonymize people.

If you cannot satisfy the intended-use scope above, **do not run this software.**

---

## Table of contents

- [What Sentinel does](#what-sentinel-does)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Capabilities](#capabilities)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Data model](#data-model)
- [Project layout](#project-layout)
- [Development & testing](#development--testing)
- [Known limitations](#known-limitations)
- [Roadmap](#roadmap)
- [Privacy & data handling](#privacy--data-handling)
- [License](#license)
- [Disclaimer](#disclaimer)

---

## What Sentinel does

At its core, Sentinel answers questions like:

- *Which devices are regularly present at this location, and on what schedule?*
- *Has a device that wasn't here before started appearing?*
- *Is a device that should be here suddenly absent — or present at an unusual time?*
- *Are there devices that appear only briefly, or only when something else is happening?*

It does this by continuously capturing RF observations, attaching them to a persistent identity model, and analyzing presence over time to surface **patterns of life** and **anomalies** — the building blocks of situational awareness and counter-surveillance for a fixed site.

---

## How it works

The pipeline runs in four conceptual stages:

1. **Capture.** A monitor-mode radio passively records Wi-Fi management frames in the environment. Each observation is a timestamped record of a device identifier, the frame type, signal strength, and other in-the-clear metadata. Capture is read-only with respect to the airwaves — nothing is transmitted.

2. **Normalize & enrich.** Raw observations are cleaned and enriched. Hardware identifiers are resolved to a manufacturer via an OUI (Organizationally Unique Identifier) vendor database, giving each device a coarse "what kind of thing is this" label without any payload inspection.

3. **Persist & identify.** Observations are written to a local SQLite database under a defined schema. An identity layer associates observations with logical devices and, where possible, groups related identifiers into a single entity — the foundation for tracking presence consistently over time even as individual signals come and go.

4. **Analyze.** On top of the stored history, Sentinel computes presence timelines, co-presence relationships, baseline behavior, and deviations from that baseline (anomaly detection). This is where "a list of MACs" becomes "pattern of life."

A key honest finding from development: naive statistical co-presence clustering (e.g. PMI-based) does **not** work well on sparse, intermittent capture data — observation gaps cause spurious convergence. Robust identity grouping requires multi-evidence fusion rather than a single statistical signal. See [Known limitations](#known-limitations).

---

## Architecture

```
        ┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
 RF ───▶│   Capture    │────▶│  Normalize /     │────▶│   SQLite store  │
 air    │ (monitor mode)│    │  enrich (OUI)    │     │  (schema.sql)   │
        └──────────────┘     └──────────────────┘     └────────┬────────┘
                                                                │
                                          ┌─────────────────────┴───────────┐
                                          │         Analysis layer          │
                                          │  presence · co-presence ·       │
                                          │  baselining · anomaly detection │
                                          └─────────────────────────────────┘
```

- **Language:** Python (packaged via `pyproject.toml`).
- **Storage:** SQLite, schema defined in `schema.sql`.
- **Orchestration:** a control script (`sentinel.sh`) plus `systemd` unit files for running capture and analysis as managed background services.
- **Reference data:** an OUI vendor table for manufacturer resolution.
- **Test data:** a synthetic-observation generator so the pipeline and analytics can be exercised without any real capture.

---

## Capabilities

- Passive, receive-only Wi-Fi management-frame capture
- Manufacturer resolution via OUI lookup
- Persistent, queryable observation store (SQLite)
- Device identity association across observations
- Presence and co-presence timelines
- Behavioral baselining and anomaly/deviation detection
- Proximity / appearance alerting concepts (see roadmap for maturity)
- Synthetic data generation for offline development and testing
- Runs unattended as `systemd` services on low-power hardware

> Maturity varies by capability. Capture, storage, enrichment, and presence analysis are the most developed; multi-evidence identity fusion and some alerting features are in progress. Always check `KNOWN_GAPS.md` and `CHANGELOG.md` for the current state.

---

## Requirements

**Hardware**
- A Linux host capable of running Python services continuously. Sentinel was developed to run on a single-board computer (e.g. a Raspberry Pi 5) as a fixed-site sensor.
- A Wi-Fi adapter that **supports monitor mode** for management-frame capture.

**Software**
- A recent Linux distribution
- Python (version per `pyproject.toml`)
- SQLite
- Standard wireless tooling for putting an interface into monitor mode

> Monitor mode should be configured in a way that does not disrupt your host's normal connectivity. The operator documentation describes the approach used during development.

---

## Installation

> The commands below describe the intended flow. Verify exact invocation against `install.sh`, `COMMANDS.md`, and `OPERATOR_MANUAL.md` in this repository, which are authoritative.

```bash
# 1. Clone
git clone https://github.com/dafarusd/sentinel-public.git
cd sentinel-public

# 2. Run the installer (sets up the environment and dependencies)
./install.sh

# 3. Initialize the database from the schema
sqlite3 sentinel.db < schema.sql   # confirm against OPERATOR_MANUAL.md

# 4. Create your config from the template
cp config.yaml.example config.yaml
# then edit config.yaml — see Configuration below
```

To run capture/analysis as background services, install the provided unit files from `systemd/` (review and adjust paths/user for your host before enabling).

---

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit it for your environment. The example file is the canonical reference for available keys; do not commit your real `config.yaml`.

Typical things you will set:

- **Capture interface** — the monitor-mode wireless interface name
- **Storage** — path to the SQLite database
- **Location / site** — a label and (optionally) reference coordinates for the sensor's fixed position
- **Identity model** — path to your local identity definitions, if you maintain a known-device list
- **Analysis thresholds** — windows and sensitivity for presence/anomaly logic

> `config.yaml` is intentionally git-ignored. Treat it as containing sensitive, site-specific data and keep it out of version control.

---

## Usage

> Reconcile these examples with `COMMANDS.md` / `sentinel.sh --help` — that file reflects the real subcommands.

```bash
# Start a capture session
./sentinel.sh start          # example — confirm subcommand names

# Check status
./sentinel.sh status

# Run / refresh analysis over stored observations
./sentinel.sh analyze

# Stop
./sentinel.sh stop
```

For long-running deployment, prefer the `systemd` services over foreground invocation so capture survives reboots and runs unattended.

---

## Data model

Sentinel stores observations and derived entities in SQLite. The full schema lives in `schema.sql` and is the source of truth. Broadly, it captures:

- **Observations** — individual timestamped RF detections (identifier, frame metadata, signal strength)
- **Devices / identities** — logical entities that observations are associated with, plus vendor/OUI enrichment
- **Derived analysis** — presence intervals, co-presence relationships, and baseline/anomaly records

Because everything lives in a single SQLite file, you can query it directly with any SQLite client for ad-hoc analysis.

---

## Project layout

```
sentinel-public/
├── sentinel/              # Core Python package (capture, enrichment, storage, analysis)
├── scripts/               # Helper and maintenance scripts
├── systemd/               # systemd service units for background operation
├── schema.sql             # SQLite database schema
├── config.yaml.example    # Configuration template (copy to config.yaml)
├── install.sh             # Installer / environment setup
├── sentinel.sh            # Primary control script
├── pyproject.toml         # Python project / dependency definition
├── COMMANDS.md            # Command reference (authoritative)
├── OPERATOR_MANUAL.md     # Full operator documentation
├── MANUAL_README.md       # Manual / setup notes
├── KNOWN_GAPS.md          # Current limitations and unimplemented work
├── ROADMAP_V2.md          # Planned direction
└── CHANGELOG.md           # Development history by stage
```

---

## Development & testing

Sentinel ships a **synthetic data generator** so you can develop and validate the storage and analysis layers without performing any real RF capture. This is the recommended way to explore the analytics, run the pipeline end-to-end, and contribute changes safely.

Working from synthetic data also keeps real, site-specific observations out of your development loop entirely.

---

## Known limitations

This project is honest about what isn't finished. See `KNOWN_GAPS.md` for the full list. Highlights:

- **Identity fusion is incomplete.** Reliable grouping of multiple identifiers into a single real-world entity requires multi-evidence fusion; it is a prerequisite for several downstream features and is not fully implemented.
- **Statistical co-presence clustering is unreliable on sparse data.** PMI-style approaches converge spuriously when capture is intermittent. Treat any automated grouping as a hypothesis, not ground truth.
- **MAC randomization.** Modern devices randomize identifiers, which limits naive long-term tracking by design — a feature for privacy, a constraint for this tool.
- **Capture completeness depends on hardware and environment.** Adapter, antenna, placement, and RF noise all materially affect what is and isn't seen.

---

## Roadmap

See `ROADMAP_V2.md` for the detailed plan. The general direction is from raw capture toward **intelligent passive awareness**: stronger identity fusion, richer pattern-of-life modeling, more robust anomaly detection, and site-aware alerting — all while staying strictly passive and metadata-only.

---

## Privacy & data handling

- Sentinel's database contains device identifiers and behavioral timelines. **Treat it as sensitive personal data**, even if it only concerns your own household.
- Keep `config.yaml`, your database, and any captured data out of version control (the repo is configured to ignore them).
- If you share results, screenshots, or exports, scrub identifiers first.
- Consider retention limits — indefinite logging of presence data expands both your risk and your responsibility.
- Delete data you no longer need.

---

## License

This project is released under the terms in [`LICENSE`](LICENSE).

> **No license file is included yet — add one before relying on others to reuse this code.** Without a license, default copyright applies and others have no legal right to use, modify, or distribute it. Common choices: **MIT** or **Apache-2.0** for permissive reuse, **GPL-3.0** if you want derivatives kept open. Apache-2.0 additionally provides an explicit patent grant, which some prefer for security tooling.

---

## Disclaimer

Sentinel is provided for lawful research and education on infrastructure you own or are authorized to monitor. It is offered **as-is, without warranty of any kind**. The author accepts no liability for misuse or for any consequences of using this software. By using Sentinel you accept full responsibility for operating it lawfully and ethically in your jurisdiction. **If in doubt, don't capture.**
