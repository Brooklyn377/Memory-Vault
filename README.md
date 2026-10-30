# Memory agent

> **Work in progress.** This project is under active development. Things may break, features may change or be removed
> between updates, and settings or saved data may need to be redone. Keep backups of anything you care about (your vault
> and the agent's state folder), and read what an update changes before you apply it. Use it at your own risk.

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
| `--gpu cpu\|DEVICE` | run the local models on the CPU (default) or on a host GPU through Vulkan |
| `--add-gpu CTID` | pass a GPU into an existing container and switch its models to it |
| `--sweeper-url`, `--reviewer-url`, `--curator-url` | model download links |
| `--small` | one tiny test model for all three, small container |
| `--no-models` | install without models |
| `--yes` | take every default |
| `--plain` | ask in plain text lines instead of dialog boxes |
| `--dry-run` | print what would be done, change nothing |

## After installing
1. Open the web address and log in. Change the password on the Settings page.
2. Upload a chat export (`.zip` or `.json`). The agent reads it in chunks; a fact is written to the vault only
   after the reviewer model has checked it against a quote from the chat. The Sweeper does not wait for the
   Reviewer: a chunk goes to the Reviewer's queue and the Sweeper reads the next conversation; when a review
   is done, that conversation is the next one the Sweeper works on (the Queue page shows both).
3. Models: Settings shows each model's server, memory use and limits, and can download other models. Each number
   has a usual range: a value outside it, or one that makes calls too big for a model's context, is saved after a
   warning instead of being refused. Only values that cannot work at all (a negative count, a probability above 1)
   are refused. Under Limits and guardrails, -1 turns a limit off; each limit's help line says what that means.

## Model groups and priority: which models work at the same time
By default **one model works at a time** and the others take turns: a call for a model that is not working waits, and the
model gets its turn when the one that has it is idle. The local model servers are therefore not started at boot; the turn
manager (in the web service) starts and stops them.

Under **Settings → Model groups**, a group is a list of models that are allowed to work at the same time, wherever they
run: **Add group** and tick them. A model can be in several groups. The models working at one time must all be in one
group, so models that no group lists together take turns, even on different machines.

Each piece of hardware (this server's CPU, this server's GPU, each external server on the Servers card) also has a
number: the most models that may work on it at once (default 1). Groups never raise it: two grouped models on a server
set to 1 still take turns there.

The same card has the **priority** list: drag the models into the order of the queue. When several wait to work, the
first in the list goes first. Priority never takes the turn from a model that is working.

- **Each model's card has two separate controls.** The **slider** (on or off) says whether the model takes part in the
  turns: off stops its server and it takes no turns, its calls wait until you turn it on; on lets the manager load it when it has
  work and its turn comes. **Hold to load / Hold to unload** starts or stops that server right now, ignoring every rule, and
  does not change the slider. With the slider on, the manager may undo a hand load or unload the next time it has a load or
  unload to do for a turn; with the slider off it never touches that model, so a model you turned off is yours to load and
  unload by hand for testing.
- **Memory.** Models that take turns on the same hardware are not all kept loaded: a model waiting for its turn is unloaded,
  and so is any server that does not belong with the ones working, whenever a turn changes hands. The one exception is two
  local models that run the same model file with the same settings: both stay loaded and swap turns, which costs nothing.
- **How a turn passes:** a model that still has work keeps its turn for at least the minutes set on that page
  (default 10), and always until its running call finishes; then the waiting model that is first in the priority list
  is loaded. A model that has had nothing to do for half a minute gives way at once. A model in the middle of an
  Assistant task (between its tool calls) is not given away.
- **The Curator** asks for its turn when the other local models have nothing to do, and gives it back when one wants
  to run.
- **Updating from an earlier version:** the servers that are running are kept as they are until another model needs
  its turn. After that, unless you made a group, the Sweeper and Reviewer no longer run together on one machine: add a
  group with both if your memory allows it.

## History: what happened behind the scenes
The **History** tab lists everything the models and you did, newest first, from the agent's logs. Filter by who did it
(Sweeper, Reviewer, Curator, Assistant, you, system) and by kind. Besides the vault work, it shows:
- **Assistant tools** and **Websites**: every look-up the Assistant made (search, open a file, Git repositories, web
  searches and the pages it opened), with what it asked for and a short start of what came back; its answers, with the
  full reply inside; the changes it drafted or proposed.
- **Git servers**: every pull and every commit.
- **Turns**: every time a model got its turn or gave it up, and why a model had to wait.
- **Settings**: model downloads (here and on a server), servers saved, groups saved, models loaded or stopped by you.
- **Model calls** (off until you tick it): every call to a model.

## Update
New versions come from this repository. The easiest way: **Settings → Updates** (at the bottom) shows the installed
and newest version, with **Check now** and **Update now**. The agent also checks by itself every 6 hours, and a **1**
appears next to Settings in the menu when an update is ready. Update now runs the same updater as below and shows
its progress; the page reloads when it is done. When an update is waiting, a list under the buttons shows what each release
you have not applied yet changed, newest first. It comes from the release notes each release is published with, so a
release made before notes were added only shows its name.

Or on the Proxmox host:

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
`/fix` corrections stay with the Reviewer. Your chat is also saved as a conversation, one file a day under `raw/conversations`
in the same format as an imported chat, so the Sweeper reads it and the notes can find it. While it is off or not answering, the Reviewer answers as before.

- **It looks things up before answering:** all vault notes and raw chats, the agent's own state and logs, and
  the Git servers it has a token for. Files named like keys, tokens, passwords or env files, and everything in
  `/etc/memory-agent`, are never readable, and every result is scrubbed, because its model may run on another
  machine.
- **It hands slow work to the Reviewer and Sweeper as jobs:** draft a new version of a file in a Git server, change
  a vault note, or sort a note. They run when those models are idle. Every result waits for you as an Ask card
  with the changes and Approve / Reject (a failed job asks you on the Failed list). Approving a note change files it to
  the inbox (the usual proposal path). A document draft is only made for a Git server set to read and write.
- **Changing files in a Git repository, under your supervision:** ask the Assistant to change a file and it makes
  the change itself (it is your quick helper for lookups, config changes and file edits; the Reviewer is much slower
  and is only for long work, or whole new documents). It replaces exact text it copied from the file, or adds to the
  end, or creates a file: one file per change. The Ask card shows exactly which repository (name, address, branch) and file, a summary, and
  the diff line by line. On a Git server marked **read and write**, **Approve and commit** commits exactly that
  change to that one file and pushes it (as the bot named in `config.yaml`), using the Assistant's token for that
  server (or another model's, if it has none). If the file changed in the repository after the draft was written,
  nothing is committed and you are asked to request a new draft. On a **read only** server nothing is queued or asked: the Assistant
  shows you the change in its answer, for you to apply, and says to set the server to read and write if you want it
  pushed. Under its answer, the notes it used show as one line ("Notes used: N notes") with **See more**. Every commit is an ordinary commit in the repository's history, so it can be
  reverted.
- **It keeps trying:** if its server drops the connection, cannot be reached or answers with a server error, the Assistant
  tries the same step again (after 5 seconds, then 10, 20, 40, and every minute) for as long as its switch is on, and
  the chat shows what is happening under your message. What it had found so far is kept. A request the server refuses
  (for example one that is too big) is not repeated; that fails with the server's reason.
- **The web (Settings → Web access, off by default):** when on, the Assistant can search the web (DuckDuckGo, or your
  own SearXNG) and read pages, but only after it has searched the vault and your Git servers and found nothing, and
  only a few look-ups per message (a limit on its card). Under its reply, one line says "Used the web: N searches,
  N pages" with **See more** for every query and page; the reply itself names its sources. This container makes the
  requests: pages on your own network, this machine and link-local addresses are refused (also after a redirect),
  no cookies or keys are sent, and page text is treated as untrusted. The Sweeper, Reviewer and Curator have no tools
  that could use the web.
- **Git servers** (Settings): add each repository with a name, what it is for, its HTTPS address, read only or
  read and write, and a token per model. A model without a token cannot read it. Tokens are stored root-only in
  `/etc/memory-agent/git/` and are never shown again or sent to a model. Nothing is pushed except a change you
  approved on an Ask card, and only to a server marked read and write.

## Optional: a GPU in the container
The local models run on the CPU by default. To run them on a GPU of the Proxmox host instead (through Vulkan: AMD,
Intel or NVIDIA), the GPU has to be passed into the container. That needs Proxmox VE 8.1 or newer.

- **New install:** the installer asks "Run the local models on" when the host has a GPU (or use `--gpu
  /dev/dri/renderD128`). CPU stays the default.
- **Existing container:** on the Proxmox host, run
  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/install.sh)" _ --add-gpu <container ID>
  ```
  It asks which GPU, passes it in, restarts the container, and switches its local models to the GPU.
- **Settings → Servers** shows this server's hardware: whether the GPU is set up and which GPU the container sees, with
  a button to set it up (or go back to CPU only). Setting it up rebuilds llama.cpp (this takes a while), then restarts the
  model servers; if the build fails, nothing changes. After that each model's **Where it runs** offers "Local model on
  this server CPU" and "Local model on this server GPU". On the GPU a model puts all its layers there unless GPU layers is
  set under its Advanced server flags, and the GPU's memory is what limits the context. A model set to the CPU runs with
  the GPU switched off, so CPU and GPU models can work at the same time.

## Optional: a model on another machine (model runner)
Any of the four models can run on another machine, for example a PC with a GPU. Add the machine once under
**Settings → Servers → Add server**, then pick it in **Where it runs** on the model's card.

On its own, the agent only sends that server its calls. llama-server cannot change its model, context or slots
while it runs, so the card cannot change them either. To set them from the card, run the **model runner** on that
machine: `model_runner.py` from this repository. It is one file and needs only Python 3.8 or newer (Linux, Windows
or macOS). It starts llama-server, and restarts it with new settings when you press Apply all on the card.

### Quick install for a GPU machine (one line)
For a new GPU machine, one script does what steps 1 to 5 below do by hand: it looks at the graphics cards, lets you pick
the software llama.cpp runs on, downloads the runner, sets llama.cpp up for it, starts the runner as a service, and prints
the **address**, the **API key** and the **runner token** to type into **Settings → Servers → Add server** (step 6).
Run it as root:

- **Proxmox** (creates a Debian container, passes one GPU of the host into it, builds llama.cpp inside; it asks for
  anything you do not give as an option, and always asks the disk, memory and cores):
  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/scripts/runner-proxmox.sh)"
  ```
- **Podman** (the llama.cpp server runs in a container the runner creates when you apply the first settings):
  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/scripts/runner-podman.sh)"
  ```
- **Docker**:
  ```bash
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/scripts/runner-docker.sh)"
  ```

**The software choice.** The script lists the cards it finds and offers **Vulkan** (works on AMD, Intel and NVIDIA; it is the
default, so pressing Enter takes it), **CUDA** (NVIDIA) and **ROCm** (AMD). It puts **(recommended)** next to the one it
expects to be fastest here: CUDA for an NVIDIA card whose driver and container support are in place, ROCm for an AMD card
ROCm supports (RX 6000 and newer, Instinct) when its compute device is there, otherwise Vulkan. Each line says what is
missing for it. `--backend vulkan|cuda|rocm` skips the question, and `--yes` takes Vulkan. With CUDA and Podman the script
writes the NVIDIA device list Podman needs (`/etc/cdi/nvidia.yaml`) when the toolkit is installed but the list is not;
with Docker the NVIDIA container toolkit has to be set up already. All layers go on the GPU (a model's card can change
GPU layers). On Proxmox, CUDA and ROCm are installed inside the container from NVIDIA's and AMD's repositories and need the
host's driver; they are best effort and not as well tried as Vulkan.

Options go after the command, for example `bash -c "$(curl ...)" _ --models /data/models --llama-port 8081`; add
`--help` to see them all and `--dry-run` to see what a script would do without changing anything. Models are kept in
`/opt/models` (Proxmox) or `/srv/llama-models` (Podman, Docker), and you download them from the Servers card (see
"Download and delete models on that machine" below). The runner keeps the key and token in `model_runner.json`, readable
by root only. To update the runner later, run the same script with `--update` (Proxmox: `--update <container ID>`); keys, models and settings stay. The runner starts llama-server again by itself if it dies (waiting longer after each quick death); stopping it from a model's card keeps it stopped.

Steps 1 to 5 below are the manual way, for a machine the scripts do not cover. They run on the other machine.
Steps 6 and 7 are on the Settings page.

### 1. Find out how llama-server runs there
```bash
podman ps        # or: docker ps
```
- **A llama.cpp container is listed** (image `ghcr.io/ggml-org/llama.cpp:...`): follow the **container** steps.
  Note its name (the last column, for example `llama`).
- **Nothing is listed:** llama-server runs as a program. Follow the **program** steps.

Do not paste the output of `podman inspect` or `ps` anywhere: it shows the whole command, including the
`--api-key` value.

### 2. (container) Check what starts it, and which devices it has
The runner has to be the only thing that creates the container. A systemd service or a Podman **Quadlet** that
runs it would replace the runner's container every time the runner changes it: the change seems to apply, and
moments later the old settings are back. You turn it off in step 4; for now, only check:
```bash
systemctl --user list-units --all | grep -i llama
ls ~/.config/containers/systemd/
```
If there is a `.container` file for it (a Quadlet), note the devices it gives the container, for step 3:
```bash
grep -i AddDevice ~/.config/containers/systemd/*.container        # for example: AddDevice=/dev/dri
```
Leave the container running: setup in step 3 reads it.

### 3. Set up the runner
```bash
mkdir -p ~/model-runner && cd ~/model-runner
curl -fsSLO https://raw.githubusercontent.com/Brooklyn377/Memory-Vault/main/model_runner.py
```
- **Container:**
  ```bash
  python3 model_runner.py --setup --adopt llama --device /dev/dri
  ```
  Use the container's name from step 1. Add `--engine docker` for Docker. Give every device the container needs:
  `--device /dev/dri` for a GPU through Vulkan (AMD, Intel, NVIDIA), `--device /dev/kfd --device /dev/dri` for ROCm.
  Podman does not report devices that a Quadlet added, so the runner cannot see them by itself. Check the
  **Devices it will get** line in what setup prints: if it says none, the model runs on the CPU.
  The runner reads the container's image, models folder, port, security options and command, and keeps
  everything in that command it does not manage (such as `--jinja` and `--api-key`).
- **Program:**
  ```bash
  python3 model_runner.py --setup --llama /path/to/llama-server --models /path/to/models
  ```
  On Windows: `python`, and the path to `llama-server.exe`. Stop any llama-server you started yourself; the
  runner starts its own.

Setup prints a **token** once. Keep it for step 6. Running setup again makes a new token, and the old one stops
working. Other options: `--port` (the runner's port, default 8090), `--listen` (default `0.0.0.0`), and for a
program `--llama-port` (llama-server's port, default 8080).

### 4. Turn off the old service, and run the runner as a service
**(container) If step 2 found a Quadlet or service, turn it off now.** A Quadlet: move its file out of the folder
(it is kept, so you can go back), then stop it. This also removes the container; the runner creates its own next.
```bash
mkdir -p ~/quadlet-backup
mv ~/.config/containers/systemd/llama.container ~/quadlet-backup/
systemctl --user daemon-reload
systemctl --user stop llama.service
```
A plain systemd service: `systemctl --user disable --now <its name>.service`. To go back later, stop the runner,
move the file back, run `systemctl --user daemon-reload`, and start the service.

Then run the runner as a systemd user service that starts at boot (Linux). When it starts and the container is not
running, it creates it with its saved settings:
```bash
mkdir -p ~/.config/systemd/user
printf '[Unit]\nDescription=Model runner\n\n[Service]\nExecStart=/usr/bin/python3 %%h/model-runner/model_runner.py\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n' > ~/.config/systemd/user/model-runner.service
systemctl --user daemon-reload
systemctl --user enable --now model-runner
loginctl enable-linger $USER
systemctl --user status model-runner      # should say: active (running)
```
Windows: a scheduled task "At log on" that runs `python C:\path\to\model_runner.py`.

Starting the runner by hand while the service runs fails with "Address already in use". That is expected: the
service already has the port.

### 5. Open the runner's port
The agent must reach the runner (port 8090) as well as llama-server (8080). On Fedora, Bazzite and other
firewalld systems:
```bash
sudo firewall-cmd --add-port=8090/tcp --permanent && sudo firewall-cmd --reload
```
Keep both ports on your local network.

### 6. Add the server and connect the card
Under **Settings → Servers → Add server**:
- **Name:** anything, for example Gaming PC.
- **Address:** `http://<that machine>:8080`, and its **API key** if llama-server has one.
- **Runner address:** `http://<that machine>:8090`. Type it in full: the grey text in an empty box is only an
  example.
- **Runner token:** the token from step 3.
- **Models that may work on it at the same time:** 1 unless that machine can serve more (see Model groups).

Press **Add**. Then, on the model's card under **Server and where it runs**, set **Where it runs** to that server and
press **Apply all**. A **Runner:** line appears under the token, and should say "llama-server ready". The note
above Context changes to "Managed through the runner on that machine".

### 7. Change settings and check them
- **Model on that server** is the model. Its list also shows the model files on that machine.
- Set **Context** and **Parallel slots**, and under **Advanced server flags** GPU layers, cache types and so on.
  (Threads is not shown for an external server; llama-server there picks its own.)
- Press **Apply all** and choose **Apply now**. The runner restarts llama-server there, which takes about a minute.

To check it worked: the server line at the top of the card no longer says "running with … tokens of context, not
the … you set", and the Thinking tab shows the new context. llama.cpp rounds the context up to a multiple of 256.

### Download and delete models on that machine
With a runner, the server's entry on **Settings → Servers** has **Models on that machine**: the model files there with
their sizes, a box to paste a `.gguf` link, and a delete button for each file. The download happens on that machine,
straight from the link (Hugging Face links start at once; any other host asks for your OK first). Deleting a model that
a card still uses warns you first. Download a model, then pick it under **Model on that server** on a card and press
Apply all.

### Use the same server for other models
Pick the same server in **Where it runs** on any model's card; the address, keys and runner are kept once, on the server.
Changing the server on the Servers card updates every model that uses it. A server's number says how many of those
models may work on it at the same time (1: they take turns). **Remove** deletes a server; models that used it keep
their settings.

Two models on the same runner share one llama-server. Give them the same model, context and slots, or each Apply
all restarts it with the other's settings; Apply all warns about this.

### If it does not work
| What you see | What it means |
|---|---|
| Runner line: "cannot reach the runner" | The runner is not running (step 4), or the firewall blocks 8090 (step 5). |
| Runner line: "the runner rejected the token" | Wrong token, or setup was run again since. Paste the newest one. |
| No Runner line, and the card still shows only Context | Runner address is empty. Type it (step 6). |
| Ask card "did not take the new settings", ending in a llama-server error | It did not start with them, for example the context does not fit in the GPU's memory. The old settings are back. Lower the context, or set Key/Value cache type to `q8_0`. |
| Ask card "did not take the new settings" with llama-server still loading, or "no container with name" | Something else replaced the container: a Quadlet or service is still running it (step 2). |
| The model is slow and the GPU's memory is not used | The container has no GPU device. Run setup again with `--device` (step 3). |

What the runner does and does not do: every request to it needs the token. It uses only model files in its models
folder, passes a flag only if that llama-server's own `--help` lists it, and runs only the configured program or
container engine, never through a shell. If llama-server does not come up with new settings, it puts the previous
ones back. Hold to stop / Hold to start on the card stops and starts that llama-server. Stopping the runner stops a
llama-server program it started; a container keeps running. Its log, `model_runner-llama.log` next to the script,
never contains the API key.

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
The Servers card on the Settings page can change the host's CPU power state through a small helper on the Proxmox host. The helper
is not part of this install yet. Without it the card says no helper is set up.
