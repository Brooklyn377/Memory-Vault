# Memory agent

A documentation agent that runs on your own hardware. It reads exported chats, writes a markdown vault of
summaries, timelines and notes, and checks every fact against a quote from the chat before anything is written.
The models run locally through llama.cpp. Everything is in one Proxmox container with a web page.

## Requirements
- A Proxmox VE 8 host (x86_64) with internet access. CPUs without AVX work; llama.cpp is built for the CPU it runs on.
- Memory and disk for the models you pick. The installer's defaults, 24 GB of memory and 80 GB of disk, are an
  estimate for the original three models (a 9B sweeper, gpt-oss-20b as reviewer, a 24B curator that swaps in
  while the other two are idle). The test install (`--small`) defaults to 6 GB and 16 GB, mostly for building
  llama.cpp. Size the container for the models you choose.
- Models are not included; the installer downloads the ones you choose.

## Install
In the Proxmox host shell, as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/install.sh)"
```

It starts with a menu: **Default Install** takes every default (DHCP and the default models), **Advanced Install**
asks for the container ID, storage, cores, memory, disk, network and DNS server, and which models to download, one dialog box
each. Every question has a default; press Enter to take it. An answer that would not work (a fixed address without
its prefix length such as `/24`, a gateway outside that network, a container ID already in use, more cores than the
host has) is refused with the reason, and the box asks again. Before anything is created, a summary asks to
confirm. The default models are Qwen3.5-9B Q4_K_M (sweeper, 5.7 GB),
gpt-oss-20b MXFP4 (reviewer, 12.1 GB) and LFM2.5-24B-A2B Q4_K_M (curator, 14.5 GB). Type `-` to skip one and download it
later on the Settings page, or paste another `.gguf` link. Temperatures, token limits and chunk sizes start at the
defaults in the code (`settings.py`, `tuning.py`) and can be changed on the Settings and Tuning pages.
At the end it prints the web address and a generated login.

A quick test with a tiny model and no questions:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/install.sh)" _ --small --yes
```

Options go after the `_`. All of them are listed with `--help`; the main ones:

| Option | Meaning |
|---|---|
| `--ctid N`, `--hostname NAME` | container ID (default: next free) and name |
| `--storage NAME`, `--disk GB` | where the container disk goes and its size |
| `--cores N`, `--memory MB` | container size |
| `--bridge NAME`, `--ip CIDR`, `--gw ADDR` | network (default: `vmbr0`, DHCP) |
| `--dns ADDR` | DNS server, or `host` for the Proxmox host's (default: the gateway with a fixed address) |
| `--sweeper-url`, `--reviewer-url`, `--curator-url` | model download links |
| `--small` | one tiny test model for all three, small container |
| `--no-models` | install without models |
| `--yes` | take every default |
| `--plain` | ask in plain text lines instead of dialog boxes |
| `--dry-run` | print what would be done, change nothing |

## After installing
1. Open the web address and log in. Change the password on the Settings page.
2. Upload a chat export (`.zip` or `.json`). The agent reads it in chunks; a fact is written to the vault only
   after the reviewer model has checked it against a quote from the chat.
3. Models: Settings shows each model's server, memory use and limits, and can download other models. Each number
   has a usual range: a value outside it, or one that makes calls too big for a model's context, is saved after a
   warning instead of being refused. Only values that cannot work at all (a negative count, a probability above 1)
   are refused. Under Limits and guardrails, -1 turns a limit off; each limit's help line says what that means.

## Update
New versions come from this repository. On the Proxmox host:

```bash
pct exec CTID -- /usr/local/bin/memory-agent-update            # install the newest version
pct exec CTID -- /usr/local/bin/memory-agent-update --check    # only say whether there is one
pct exec CTID -- /usr/local/bin/memory-agent-update --rollback # go back to the code from before the last update
```

The full path is needed with `pct exec`, which does not search `/usr/local/bin`. (Or run `memory-agent-update` in the
container's own console.) An update backs up the current code, replaces the
code and Python packages, restarts the services and checks that the web page answers. If that check fails it
puts the old code back by itself. Your login, settings, models and vault are never touched. llama.cpp is not
rebuilt. The repository and branch it follows are in `/etc/memory-agent/source`.

## Uninstall
Everything lives in the container. Stop it and delete it in the Proxmox web interface, or on the host:
`pct stop CTID && pct destroy CTID`. This deletes the vault too; copy `/srv/memory-vault` out first if you want it.

## What it stores, and where (inside the container)
| Path | What |
|---|---|
| `/srv/memory-vault` | the markdown vault: summaries, timelines, entity notes, your uploaded chats under `raw/` |
| `/var/lib/memory-agent` | queues, logs, proposals, model settings, the hashed web login |
| `/etc/memory-agent` | `config.yaml`, model server settings, `web.env` with the first login, `source` (where updates come from) |
| `/opt/models` | model files |
| `/opt/memory-agent`, `/opt/llama.cpp` | the program and the model server |
| `/var/backups/memory-agent` | the code from the last three updates |

Nothing leaves the container unless you set up publishing (below), add Git servers, or point a model at an
external server on the Settings page.

## Optional: the Assistant
A fourth model you chat with. It is off until you turn it on: on its card on the Settings page, choose a model
(local) or an external server, load it, and tick "Use the Assistant for chat". Plain chat messages then go to it;
`/fix` corrections stay with the Reviewer. While it is off or not answering, the Reviewer answers as before.

- **It looks things up before answering:** all vault notes and raw chats, the agent's own state and logs, and
  the Git servers it has a token for. Files named like keys, tokens, passwords or env files, and everything in
  `/etc/memory-agent`, are never readable, and every result is scrubbed, because its model may run on another
  machine.
- **It hands slow work to the Reviewer and Sweeper as jobs:** draft a new version of a file in a Git server, change
  a vault note, or sort a note. They run when those models are idle. Every result waits for you as an Ask card,
  and the Queue page shows each job, its changes, and Approve / Reject / Cancel / Queue again. Approving a note
  change files it to the inbox (the usual proposal path); approving a draft pushes nothing, it is there for you
  to apply.
- **Git servers** (Settings): add each repository with a name, what it is for, its HTTPS address, read only or
  read and write, and a token per model. A model without a token cannot read it. Tokens are stored root-only in
  `/etc/memory-agent/git/` and are never shown again or sent to a model. Nothing is ever pushed.

## Optional: managing a model on another machine (model runner)
Any of the four models can run on another machine (a GPU box, a PC) by choosing **External server** on its card.
Without more, the agent only sends that server its calls: llama-server cannot change its model, context or slots
while running, so those stay as that machine started them, and the card only keeps the context for its own token
checks. To set them from the card, run the model runner next to llama-server on that machine. It is one Python file,
`model_runner.py` (in this repository), that needs only Python 3.8 or newer, on Linux, Windows or macOS.

1. Copy `model_runner.py` to that machine, next to where you keep it.
2. Set it up once. This writes `model_runner.json` next to the script and prints a token, once:
   ```bash
   python3 model_runner.py --setup --llama /path/to/llama-server --models /path/to/models
   ```
   Options: `--port` (the runner's port, default 8090), `--llama-port` (llama-server's port, default 8080),
   `--listen` (default `0.0.0.0`). On Windows, use `python` and the path to `llama-server.exe`.
3. Stop any llama-server you started yourself on that port: the runner starts its own.
4. Run it: `python3 model_runner.py`. To keep it running, make it a service (systemd on Linux, a scheduled task
   at log-on on Windows). Its llama-server output goes to `model_runner-llama.log` next to the script.
5. On the model's card: Where it runs = External server, Server address = `http://<that machine>:8080`, Runner
   address = `http://<that machine>:8090`, Runner token = the token from step 2. Press Apply all.

The card then shows that machine's model files, and Model file, Context, Threads, Parallel slots and the server
flags (including GPU layers) apply there: Apply all restarts its llama-server with them. If llama-server does not
come up, the runner puts the previous settings back, and an Ask card shows llama-server's last lines. Hold to stop /
Hold to start stops and starts that llama-server. Every request to the runner needs the token. Only model files in
its models folder can be used, a flag is passed only if that llama-server's own `--help` lists it, and nothing goes
through a shell. Keep the runner's port on your local network.

## Security notes
- The web page is plain HTTP on port 8000. Keep it on your local network, or put a reverse proxy with HTTPS
  in front of it. Do not expose it to the internet.
- The first password is generated at install time and printed once. It is in `/etc/memory-agent/web.env`
  (root only). Once you change it on the Settings page, the login uses a salted hash and the first password
  stops working.
- The container is unprivileged. Inside it the services run as root, because they start and stop the
  model servers.
- Uploaded chats are scrubbed for keys, tokens and passwords before they are stored, but treat the vault as
  private: it is your conversations.

## Optional: publishing notes to a Gitea repository
The agent can open pull requests with reviewed notes on a Gitea server. This is off after installing.
To turn it on, inside the container (`pct enter CTID`):
1. In `/etc/memory-agent/config.yaml`, set `gitea.url` and `gitea.vault_repo` (and optionally `bot_name`, `bot_email`).
2. Put a Gitea access token with write access to that repository in `/etc/memory-agent/publish.env`
   as `GITEA_WRITE_TOKEN=...`.
3. Make `/srv/memory-vault` a clone of that repository.
4. `systemctl enable --now memory-publish`.

## Optional: host CPU speed control
The Settings page can change the host's CPU power state through a small helper on the Proxmox host. The helper
is not part of this install yet. Without it the card says no helper is set up.
