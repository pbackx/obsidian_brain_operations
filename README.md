# Antigravity Nightly Obsidian Operator

An autonomous overnight task operator powered by **[Google Antigravity](https://antigravity.google)** and **[Obsidian](https://obsidian.md)**.

The system wakes up nightly during off-peak hours, synchronizes your vault via official Headless Obsidian Sync, discovers actionable tasks tagged with `#ai`, executes concrete steps toward your goals (scoping, technical research, or implementation), and maintains a single persistent **Living Dossier** per work item as durable long-term memory.

---

## Getting Started

### 1. Prerequisites
- **Linux machine** (Debian, Ubuntu, Arch, etc.)
- **Python 3.8+**
- **Google Antigravity CLI (`agy`)** installed and authenticated.
- **Node.js & npm** (managed via `nvm` or system package manager).
- **Official Obsidian Headless CLI:**
  ```bash
  npm install -g obsidian-headless
  ```

### 2. Installation & Configuration
Clone this repository:
```bash
git clone <your-repo-url> ~/brain_operations
cd ~/brain_operations
```

Copy the example configuration file:
```bash
cp config/settings.env.example config/settings.env
```

Edit `config/settings.env` with your preferred settings:
```bash
# Path to your local Obsidian vault directory
VAULT_LOCAL="$HOME/vault_sync/ObsidianVault"

# Synchronization method: "obsidian-headless"
SYNC_METHOD="obsidian-headless"

# Batch limits and model effort
MAX_TASKS=3
EFFORT="high"
TIMEOUT_SECONDS=1800
```

### 3. Connect Your Vault
Log in to your Obsidian account and connect your local directory:
```bash
# Authenticate
ob login

# List remote vaults
ob sync-list-remote

# Connect local directory to your remote vault
mkdir -p "$HOME/vault_sync/ObsidianVault"
cd "$HOME/vault_sync/ObsidianVault"
ob sync-setup --vault "Your Remote Vault Name"

# Initial pull
ob sync
```

### 4. Test Run
Mark any task in your vault with `#ai`:
```markdown
- [ ] Evaluate vector databases for local LLMs #ai
  - **User Note**: Focus on low-memory embedded options like SQLite-vec.
```

Test task discovery:
```bash
python3 scripts/vault_tool.py scan --vault "$HOME/vault_sync/ObsidianVault"
```

Execute a manual run:
```bash
./scripts/run_nightly.sh
```

### 5. Automate Nightly Runs (Systemd)
1. Copy or create the user service `~/.config/systemd/user/antigravity-nightly.service`:
   ```ini
   [Unit]
   Description=Antigravity Nightly Obsidian Agent Run
   After=network-online.target

   [Service]
   Type=oneshot
   WorkingDirectory=%h/brain_operations
   ExecStart=%h/brain_operations/scripts/run_nightly.sh
   StandardOutput=journal
   StandardError=journal
   ```

2. Create the user timer `~/.config/systemd/user/antigravity-nightly.timer`:
   ```ini
   [Unit]
   Description=Trigger Antigravity Nightly Run at 02:30 AM

   [Timer]
   OnCalendar=*-*-* 02:30:00
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

3. Enable and start the timer:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now antigravity-nightly.timer
   ```

4. Enable 24/7 lingering so the timer runs while you are logged out:
   ```bash
   sudo loginctl enable-linger $USER
   ```

---

## Architecture & Data Flow

```
 +-----------------------------------------------------------------------------------+
 |                                   USER SURFACE                                    |
 |                                                                                   |
 |     +-----------------------+                    +--------------------------+     |
 |     |  Smartphone (Obsidian)|                    |   Laptop (Obsidian)      |     |
 |     +-----------+-----------+                    +------------+-------------+     |
 |                 |                                             |                   |
 |                 +---------------------+-----------------------+                   |
 |                                       | (Daytime remarks & task additions)        |
 |                                       v                                           |
 |                         +---------------------------+                             |
 |                         |   Obsidian Sync (Cloud)   |                             |
 |                         +-------------+-------------+                             |
 +---------------------------------------|-------------------------------------------+
                                         |
                            (Pre-turn: `ob sync` pull)
                            (Post-turn: `ob sync` push)
                                         |
                                         v
 +-----------------------------------------------------------------------------------+
 |                       LINUX HOST (brain_operations)                               |
 |                                                                                   |
 |   +---------------------------------------------------------------------------+   |
 |   | 1. Cloud Sync Layer (`obsidian-headless` / `ob sync`)                     |   |
 |   |    - Native E2EE synchronization directly via Obsidian Sync               |   |
 |   |    - Automatic merge resolution and version history                       |   |
 |   +-------------------------------------+-------------------------------------+   |
 |                                         |                                         |
 |                                         v                                         |
 |   +---------------------------------------------------------------------------+   |
 |   | 2. Orchestration Layer (Systemd Timer + `scripts/run_nightly.sh`)         |   |
 |   |    - Wakes up nightly at 02:30 AM                                         |   |
 |   |    - Bootstraps NVM/Node environment for non-interactive shells           |   |
 |   |    - Sequential outer loop: dispatches up to $MAX_TASKS isolated sessions  |   |
 |   |    - Eliminates cross-task context bleed and avoids line number drift     |   |
 |   +-------------------------------------+-------------------------------------+   |
 |                                         |                                         |
 |                                         v                                         |
 |   +---------------------------------------------------------------------------+   |
 |   | 3. Antigravity Agent Core (`agy -p` per task)                             |   |
 |   |    - Custom Skill: `obsidian-nightly-operator`                            |   |
 |   |    - Clean Context per Task: Reads Tier 4 `MEMORY.md` & task dossier       |   |
 |   |    - Fast Scanner: `scripts/vault_tool.py` (locates next task)            |   |
 |   |    - Phase A: Task Load & Triage (Reads targeted task and user notes)     |   |
 |   |    - Phase B: Durable Memory Recall (Reads existing living dossier)        |   |
 |   |    - Phase C: Execution Cycle (Research, blueprints, synthesis)            |   |
 |   |    - Phase D: Vault Updates & Linking                                     |   |
 |   |        * Updates/creates single living dossier in `Brain_Operations/`     |   |
 |   |        * Resolves task to `#ai/done` or `#ai/question`                     |   |
 |   |        * Appends run summary to `Brain_Operations/00_Nightly_Log.md`      |   |
 |   +-------------------------------------+-------------------------------------+   |
 |                                         |                                         |
 |                                         v                                         |
 |   +---------------------------------------------------------------------------+   |
 |   | 4. Sync-Back Layer (`ob sync`)                                            |   |
 |   |    - Automatically pushes changes back to Obsidian Sync                   |   |
 |   |    - Updates ready on phone & laptop when you wake up                     |   |
 |   +-------------------------------------+-------------------------------------+   |
 +-----------------------------------------------------------------------------------+
```

---

## Vault Conventions & Human-in-the-Loop Interaction

### 1. Folder Structure
The agent respects your vault layout and restricts all new generated files to a dedicated folder:

```
My Obsidian Vault/
├── Daily Notes/            <-- Your normal notes (untouched except for task checkboxes)
├── Projects/               <-- Your project notes
└── Brain_Operations/       <-- Managed by Antigravity
    ├── 00_Nightly_Log.md   <-- Chronological ledger of all nightly runs
    ├── Dossier_Dashboard.md <-- Dynamic dashboard powered by Dataview
    └── Dossiers/           <-- Living Dossiers (one persistent file per task)
        ├── VictoriaMetrics Migration.md
        └── SQLite Vector Search Evaluation.md
```

### 2. Task Lifecycle & State Machine

Tasks are controlled using standard markdown checkboxes and `#ai` tags:

```
                  +----------------------------------------------+
                  |              New / Backlog                   |
                  |     `- [ ] Task #ai` or `#ai/focus`          |
                  +----------------------+-----------------------+
                                         |
                                (Nightly Agent Triage)
                                         |
                                         v
                         (Autonomous Overnight Work)
                                         |
                      +------------------+------------------+
                      |                                     |
             (Goal accomplished)                   (Needs user decision)
                      |                                     |
                      v                                     v
        +---------------------------+         +-------------------------------+
        |         Completed         |         |       Blocked on Question     |
        | `- [ ] Task #ai/done [[📄]]` |       | `- [ ] Task #ai/question [[📄]]`|
        +---------------------------+         +---------------+---------------+
                                                              |
                                                    (User answers question &
                                                     explicitly retags to `#ai`)
                                                              |
                                                              v
                                                    (Re-enters nightly queue)
```

| Tag / State | Who Sets It | Scanner & Nightly Job Behavior |
| :--- | :--- | :--- |
| **`- [ ] ... #ai`** (or `#ai/todo`) | **You** | Queued backlog item. Selected in prioritized batch. |
| **`- [ ] ... #ai/focus`** | **You** | High Priority. Selected first on the next nightly run. |
| **`- [ ] ... #ai/question [[Dossier\|📄]]`** | **Agent** | Waiting on user feedback. **Ignored** by scanner until you explicitly retag to `#ai` (or `#ai/focus`). |
| **`- [ ] ... #ai/done [[Dossier\|📄]]`** (or `- [x]`) | **Agent / You** | Work completed. **Completely ignored** by the scanner. |
| **`- [ ] ... #ai/paused`** | **You** | On hold. **Completely ignored** by the scanner until unpaused. |
| **No `#ai` tag** | **You** | Normal manual todo item. **Completely ignored**. |

### 3. Inline Task Linking & Sub-Bullets

When the agent acts on a task, it updates the original note using clean inline links:

```markdown
- [ ] Migrate InfluxDB to VictoriaMetrics #ai/question [[Brain_Operations/Dossiers/VictoriaMetrics Migration.md|📄]]
  - **Latest Update (2026-10-11)**: Complete implementation playbook with Proxmox NFS bind-mount.
  - **Question for the user**: Dedicated LXC container for Grafana or shared?
```

- **1-Click Navigation:** Tapping the `📄` icon opens the living dossier immediately on phone or laptop.
- **No Bullet Clutter:** Only a single consolidated `Latest Update` line is kept beneath the task. Historical activity is stored inside the dossier's internal activity log.
- **Replying to Questions:** Add an indented reply bullet beneath the question, and retag the line back to `#ai` (or `#ai/focus`). The scanner will then pick it up again on the next nightly run.

---

## Living Dossiers (Durable Working Memory)

Rather than generating disconnected daily notes, each task has **exactly one persistent living dossier** in `Brain_Operations/Dossiers/<Clean Task Title>.md` (without date prefixes in the filename).

### Dossier Structure:
```markdown
---
task: "<Original Task Title>"
created: YYYY-MM-DD
updated: YYYY-MM-DD
status: completed | needs-clarification
source_note: "[[<Path to Note>]]"
---

# <Task Title>

> **Task:** `- [ ] <Original Task Title>`  
> **Source:** [[<Path to Note>]]  
> **Status:** In Progress

## Objective
<1-2 sentences on what this task is trying to achieve>

## Current Architecture / Plan
<Comprehensive, current state of research, implementation, and code deliverables>

## Open Questions for User (If Any)
<Active questions blocking next steps>

---

## Activity Log
### 2026-10-10 - Initial Scoping
- Evaluated database alternatives.
- Formulated migration strategy.

### 2026-10-11 - Architecture Refinement
- Incorporated user feedback regarding storage mounts.
- Added Grafana setup.
```

On subsequent nights, the agent reads this file first to load full context, updates the architecture in-place, and appends the latest milestones to the `## Activity Log`.

### Dynamic Dossier Dashboard (Dataview)

To easily visualize and track living dossiers without manual navigation, a dynamic dashboard template is provided in [`templates/Dossier_Dashboard.md`](templates/Dossier_Dashboard.md).

Copy it into your vault:
```bash
cp templates/Dossier_Dashboard.md "$VAULT_LOCAL/Brain_Operations/Dossier_Dashboard.md"
```

When opened in Obsidian with the **Dataview** community plugin enabled, it automatically renders:
- **Recently Updated:** Living dossiers modified within the last 48 hours (`dur(2 days)`).
- **Awaiting User Decision:** Living dossiers with status `needs-clarification` (corresponding to `#ai/question`).
- **All Active Dossiers:** All living dossiers sorted by recency.
- **Archived Dossiers:** Historical or completed dossiers.

---

## Freewrite Smart Typewriter Integration (`scripts/freewrite_ingest.py`)

If you use an **Astrohaus Freewrite** device connected to **Postbox** with cloud mirroring (e.g., Google Drive or NAS Cloud Sync), `scripts/freewrite_ingest.py` automatically imports your drafts into Obsidian (`Freewrite/<YYYY-MM-DD> - <Title>.md`) and converts shorthand task markers into actionable `#ai` tasks right before the nightly scan:

- **Zero External Dependencies:** Natively parses `.txt`, `.md`, and `.docx` drafts (extracting paragraphs from Postbox's default `.docx` exports via Python's standard `zipfile` + `xml.etree.ElementTree`).
- **Ergonomic Typewriter Shorthand:**
  - `- [ ] Task #ai` or `- Task #ai` $\rightarrow$ `- [ ] Task #ai`
  - `AI: Research topic` or `TODO: Research topic #ai` $\rightarrow$ `- [ ] Research topic #ai`
  - Whole-draft `#ai` marker (or drafts placed in a designated folder via `FREEWRITE_AI_FOLDERS="C"`) $\rightarrow$ automatically adds a top-level `- [ ] Freewrite brain dump: <Title> #ai` task header so the nightly operator analyzes the entire draft and creates a Living Dossier.
- **Idempotent Sync Safety:** Tracks SHA-256 hashes in `Brain_Operations/.freewrite_state.json` so source files in Google Drive (`Postbox/A`, `Postbox/B`, `Postbox/C`) remain untouched, avoiding one-way Postbox sync conflicts.

---

## Quota-Aware Model Routing & Automatic Fallback (`scripts/check_quota.py`)

On **Google AI Pro**, Antigravity separates model usage into three independent quota pools (`Gemini 4 Argon`, `Gemini Models`, and `Claude and GPT models`). Because `agy`'s built-in `--effort high` resolver does not check remaining quota before selecting a model, `scripts/check_quota.py` queries `agy -p "/usage" --output-format json` (which consumes **0 tokens**) before each task in `scripts/run_nightly.sh`:

- **Live Quota Inspection:** Displays remaining percentage and local reset times for all weekly and 5-hour buckets (`./scripts/check_quota.py` or `./scripts/check_quota.py --markdown`).
- **Daytime Reserve Protection (`ARGON_RESERVE_PCT=20`):** Preserves a configurable percentage of your weekly `Gemini 4 Argon` quota for interactive daytime sessions. When Argon drops below `ARGON_RESERVE_PCT` (or optionally steps down to `gemini-4-argon-medium` below `ARGON_MEDIUM_BELOW_PCT`), `run_nightly.sh` automatically switches to `FALLBACK_MODELS` (`claude-opus-5-5-high`, `gemini-3.8-flash-high`, `claude-sonnet-5-5-high`, `gemini-3.8-flash-medium`).
- **Mid-Run Exhaustion Retry:** If a task fails mid-execution due to bucket exhaustion, `run_nightly.sh` automatically excludes the exhausted model and retries the task on the next available fallback bucket.

---

## Repository Structure

```
brain_operations/
├── .agents/
│   └── skills/
│       └── obsidian-nightly-operator/
│           └── SKILL.md             # Custom Antigravity skill definition
├── config/
│   ├── settings.env.example         # Template configuration file
│   └── settings.env                 # (Ignored) Your local environment settings
├── scripts/
│   ├── check_models.sh              # Antigravity CLI model entitlement monitor
│   ├── check_quota.py               # Zero-token quota inspector & automatic model router
│   ├── freewrite_ingest.py          # Freewrite Postbox (.txt/.md/.docx) -> Vault ingester
│   ├── run_nightly.sh               # Master execution & sync runner
│   └── vault_tool.py                # High-speed Python vault scanner & updater
├── templates/
│   └── Dossier_Dashboard.md         # Dynamic Dataview dashboard for your vault
├── logs/                            # (Ignored) Timestamped execution logs
├── README.md                        # Documentation
└── .gitignore                       # Git ignore rules
```

---

## License
MIT License.
