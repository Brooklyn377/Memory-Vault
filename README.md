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

It asks for the container ID, storage, cores, memory, disk and network, and which models to download. Every
question has a default; press Enter to take it. The default models are Qwen3.5-9B Q4_K_M (sweeper, 5.7 GB),
gpt-oss-20b MXFP4 (reviewer) and LFM2.5-24B-A2B Q4_K_M (curator, 14.5 GB). Type `-` to skip one and download it
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
| `--sweeper-url`, `--reviewer-url`, `--curator-url` | model download links |
| `--small` | one tiny test model for all three, small container |
| `--no-models` | install without models |
| `--yes` | take every default |
| `--dry-run` | print what would be done, change nothing |

## After installing
1. Open the web address and log in. Change the password on the Settings page.
2. Upload a chat export (`.zip` or `.json`). The agent reads it in chunks; a fact is written to the vault only
   after the reviewer model has checked it against a quote from the chat.
3. Models: Settings shows each model's server, memory use and limits, and can download other models.

## Update
New versions come from this repository. On the Proxmox host:

```bash
pct exec CTID -- memory-agent-update            # install the newest version
pct exec CTID -- memory-agent-update --check    # only say whether there is one
pct exec CTID -- memory-agent-update --rollback # go back to the code from before the last update
```

(Or run `memory-agent-update` in the container's own console.) An update backs up the current code, replaces the
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

Nothing leaves the container unless you set up publishing (below) or point a model at an external server
on the Settings page.

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
