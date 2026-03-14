# Voltex — Complete Setup Guide

A step-by-step guide to getting the full Voltex stack running: the vault server, the WebSocket proxy, the web dashboard, and the Python LLM integration.

---

## What you are building

```
┌─────────────────────────────────────────────────────────┐
│                     Your LLM App                        │
│              (Python  ·  voltex_client.py)              │
└─────────────────────┬───────────────────────────────────┘
                      │  TCP  ·  port 7474
                      │  newline-delimited JSON
┌─────────────────────▼───────────────────────────────────┐
│              voltex_api  (C++ server)                   │
│   vault.meta  ·  vault.blob  ·  registry.vtxr           │
└─────────────────────┬───────────────────────────────────┘
                      │  TCP  ·  port 7474
┌─────────────────────▼───────────────────────────────────┐
│           voltex_proxy.py  (WS ↔ TCP bridge)            │
│                      port 8765                          │
└─────────────────────┬───────────────────────────────────┘
                      │  WebSocket  ·  port 8765
┌─────────────────────▼───────────────────────────────────┐
│         voltex_dashboard.html  (browser UI)             │
└─────────────────────────────────────────────────────────┘
```

**File inventory**

| File | Role |
|---|---|
| `voltex_paged.cpp` | Original interactive REPL (reference / development) |
| `voltex_api.cpp` | TCP JSON server — this is what you run in production |
| `voltex_client.py` | Python client + LLM tool definitions |
| `voltex_proxy.py` | WebSocket ↔ TCP bridge for the dashboard |
| `voltex_dashboard.html` | Browser memory dashboard |
| `vault.meta` | Generated — NodeMeta index (created on first SAVE) |
| `vault.blob` | Generated — append-only atom text store |
| `registry.vtxr` | Generated — label → hash registry |

---

## Quick start — Docker (recommended)

If you have Docker and Docker Compose installed, the entire stack runs with one command. No compiler, no manual dependency installs.

**Step 1 — Project layout**

```
voltex/
├── docker-compose.yml
├── .env.example
├── .dockerignore
├── voltex_api.cpp
├── voltex_proxy.py
├── voltex_dashboard.html
└── docker/
    ├── Dockerfile.api
    ├── Dockerfile.proxy
    ├── Dockerfile.frontend
    └── nginx.conf
```

**Step 2 — Start everything**

```bash
cp .env.example .env      # optional — edit ports if 8080 / 7474 are taken
docker compose up --build
```

Docker compiles the C++ server, wires all three services on an internal network, and creates a named volume `vault_data` so memories survive restarts.

**Step 3 — Open the dashboard**

```
http://localhost:8080
```

The status bar shows `localhost ✓` when live. Demo data loads automatically if the server is still starting.

**Useful commands**

```bash
docker compose up --build -d   # start in background
docker compose logs -f          # watch all logs
docker compose logs -f api      # watch one service
docker compose down             # stop — data preserved
docker compose down -v          # stop and wipe all vault data
docker compose up --build       # rebuild after a code change
```

**Python client against a Dockerised vault**

The vault TCP port is exposed on your host:

```python
from voltex_client import VoltexClient
v = VoltexClient(host="localhost", port=7474)
```

**Change ports** — edit `.env`:

```bash
FRONTEND_PORT=3000
API_PORT=9000
```

---

## Part 1 — Build the C++ server (without Docker)

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| C++ compiler | C++17 or later | MSVC (VS 2022), GCC 9+, or Clang 10+ |
| OpenSSL | any recent | Required for SHA-256 |
| CMake | 3.15+ | Recommended |
| vcpkg | any | Easiest way to get OpenSSL on Windows |

---

### Windows

**Step 1 — Install vcpkg** (skip if you already have it)

```powershell
git clone https://github.com/microsoft/vcpkg.git C:\vcpkg
C:\vcpkg\bootstrap-vcpkg.bat
C:\vcpkg\vcpkg integrate install
```

**Step 2 — Install OpenSSL**

```powershell
C:\vcpkg\vcpkg install openssl:x64-windows
```

**Step 3 — Compile**

```powershell
# With MSVC (Developer Command Prompt)
cl /std:c++17 /O2 voltex_api.cpp ^
   /I C:\vcpkg\installed\x64-windows\include ^
   /link ws2_32.lib ^
   C:\vcpkg\installed\x64-windows\lib\libssl.lib ^
   C:\vcpkg\installed\x64-windows\lib\libcrypto.lib ^
   /out:voltex_api.exe

# Or with CMake
cmake -B build -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake
cmake --build build --config Release
```

**Step 4 — Copy runtime DLLs** (if voltex_api.exe fails to start)

```powershell
copy C:\vcpkg\installed\x64-windows\bin\libssl-3-x64.dll   .
copy C:\vcpkg\installed\x64-windows\bin\libcrypto-3-x64.dll .
```

---

### Linux

**Step 1 — Install OpenSSL**

```bash
# Ubuntu / Debian
sudo apt install libssl-dev

# Fedora / RHEL
sudo dnf install openssl-devel

# Arch
sudo pacman -S openssl
```

**Step 2 — Compile**

```bash
g++ -std=c++17 -O2 voltex_api.cpp -lssl -lcrypto -lpthread -o voltex_api
```

---

### macOS

**Step 1 — Install OpenSSL via Homebrew**

```bash
brew install openssl
```

**Step 2 — Compile**

```bash
g++ -std=c++17 -O2 voltex_api.cpp \
    -I$(brew --prefix openssl)/include \
    -L$(brew --prefix openssl)/lib \
    -lssl -lcrypto -lpthread \
    -o voltex_api
```

---

### Verify the build

```bash
./voltex_api
```

Expected output:

```
  ╔══════════════════════════════════════╗
  ║   VOLTEX API SERVER  —  port 7474   ║
  ║   vault loaded:      0 nodes        ║
  ║   registry:          0 labels       ║
  ╠══════════════════════════════════════╣
  ║  Protocol: newline-delimited JSON   ║
  ...
```

To use a different port:

```bash
VOLTEX_PORT=9000 ./voltex_api
# or
./voltex_api 9000
```

---

## Part 2 — Set up the Python environment

### Prerequisites

Python 3.9 or later.

**Install the one dependency**

```bash
pip install websockets
```

That is the only external package required. `voltex_client.py` uses only the standard library for everything else.

---

### Verify the client

With `voltex_api` already running, run the built-in smoke test:

```bash
python voltex_client.py
```

Expected output:

```
Connecting to Voltex API...
Status: {'ok': True, 'atoms': 0, 'chunks': 0, ...}
Ingested: a3f8c2e1...
Registry (2 entries):
  [goals   ] goals/build-integration          pinned=True  alive=True
  [facts   ] facts/user-concise               pinned=True  alive=True
Lookup: Build a Voltex LLM integration layer...
After completing goal:
  facts/integration-complete
  facts/user-concise
Saved. Done.
```

---

## Part 3 — Start the WebSocket proxy

The dashboard runs in a browser and can only speak WebSocket. The proxy bridges that to the raw TCP connection the vault server speaks.

```bash
python voltex_proxy.py
```

Expected output:

```
Voltex WS proxy  ws://localhost:8765  →  tcp://localhost:7474
```

The proxy auto-reconnects to the vault server if it restarts. Leave it running in a terminal alongside `voltex_api`.

> **Custom ports:** If you changed the vault port, edit the two constants at the top of `voltex_proxy.py`:
> ```python
> VOLTEX_HOST, VOLTEX_PORT = "127.0.0.1", 9000   # match voltex_api port
> WS_PORT = 8765                                  # what the browser connects to
> ```

---

## Part 4 — Open the dashboard

Open `voltex_dashboard.html` directly in any modern browser (Chrome, Firefox, Safari, Edge).

```bash
# macOS
open voltex_dashboard.html

# Linux
xdg-open voltex_dashboard.html

# Windows
start voltex_dashboard.html
```

On load the dashboard tries to connect to `ws://localhost:8765`. If the proxy is running you will see:

```
[WS] Connected to Voltex API server
```

in the terminal panel and the status bar will show `localhost:7474 ✓`. The memory list will populate with whatever is currently in the registry.

If the proxy is not running, the dashboard enters **demo mode** automatically — all UI features work against synthetic data. It retries the WebSocket connection every 3 seconds and switches to live mode the moment the proxy comes up.

---

## Part 5 — Full startup sequence

Run these three commands in separate terminals, in order:

```bash
# Terminal 1 — vault server
./voltex_api

# Terminal 2 — WebSocket proxy
python voltex_proxy.py

# Terminal 3 — (optional) smoke test
python voltex_client.py
```

Then open `voltex_dashboard.html` in your browser.

---

## Part 6 — LLM integration

### With the Anthropic SDK

```bash
pip install anthropic
```

```python
import anthropic
from voltex_client import VoltexClient, ANTHROPIC_TOOL_DEFINITIONS, EXAMPLE_SYSTEM_PROMPT

vault  = VoltexClient()          # connects to localhost:7474
client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env

conversation = []

def chat(user_message: str) -> str:
    conversation.append({"role": "user", "content": user_message})

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=EXAMPLE_SYSTEM_PROMPT,
            tools=ANTHROPIC_TOOL_DEFINITIONS,
            messages=conversation,
        )

        # Collect tool calls and text blocks
        tool_results = []
        reply_text   = ""

        for block in response.content:
            if block.type == "text":
                reply_text += block.text
            elif block.type == "tool_use":
                result = vault.call(block.name, block.input)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     str(result),
                })

        # If there were tool calls, feed results back and loop
        if tool_results:
            conversation.append({"role": "assistant", "content": response.content})
            conversation.append({"role": "user",      "content": tool_results})
            continue

        # No tool calls — we have a final response
        conversation.append({"role": "assistant", "content": reply_text})
        return reply_text

# Example
print(chat("I want to learn how transformers work. Set that as a goal."))
print(chat("What are my current goals?"))
print(chat("I finished learning about transformers. Mark it done."))
```

### With the OpenAI SDK

```bash
pip install openai
```

```python
from openai import OpenAI
from voltex_client import VoltexClient, TOOL_DEFINITIONS, EXAMPLE_SYSTEM_PROMPT
import json

vault  = VoltexClient()
client = OpenAI()   # reads OPENAI_API_KEY from env

conversation = [{"role": "system", "content": EXAMPLE_SYSTEM_PROMPT}]

def chat(user_message: str) -> str:
    conversation.append({"role": "user", "content": user_message})

    while True:
        response = client.chat.completions.create(
            model="gpt-4o",
            tools=TOOL_DEFINITIONS,
            messages=conversation,
        )
        msg = response.choices[0].message
        conversation.append(msg)

        if not msg.tool_calls:
            return msg.content

        # Route tool calls to Voltex
        for tc in msg.tool_calls:
            args   = json.loads(tc.function.arguments)
            result = vault.call(tc.function.name, args)
            conversation.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      str(result),
            })
```

---

## Part 7 — Data files

Voltex creates three files in the directory where `voltex_api` runs:

| File | Written by | Purpose | Notes |
|---|---|---|---|
| `vault.blob` | `voltex_api` continuously | Raw atom text — append-only log | Never needs manual save |
| `vault.meta` | `SAVE` command | NodeMeta index — all graph structure | Must save to persist across restarts |
| `registry.vtxr` | `REGISTER` / `FORGET` automatically | Label → hash mapping | Auto-saved on every mutation |

> **Important:** `vault.blob` is written continuously but `vault.meta` is only written when you call `SAVE`. If you restart the server without saving, you lose any nodes created since the last save. The registry is always current on disk.

**Recommended: save on shutdown**

```python
import atexit
vault = VoltexClient()
atexit.register(lambda: vault.save())
```

**Or save periodically**

```python
import threading

def autosave():
    vault.save()
    threading.Timer(300, autosave).start()   # every 5 minutes

autosave()
```

---

## Part 8 — Namespace conventions

The registry uses `/` as a namespace separator. These are the recommended namespaces:

| Namespace | Purpose | Lifespan |
|---|---|---|
| `goals/` | Active intentions the LLM is pursuing | PIN — until `FORGET` when done |
| `facts/` | Persistent user facts and preferences | PIN — until explicitly forgotten |
| `context/` | What is being worked on right now | PIN — update each session |
| `people/` | Names, relationships, preferences | PIN — long-lived |
| `scratch/` | Temporary working notes | No PIN — auto-decays in ~32 DREAM cycles |

**Naming convention:** use lowercase kebab-case after the namespace.

```
goals/build-voltex-api
facts/user-prefers-dark-mode
context/current-project
people/alice-role
scratch/looked-up-sha256-size
```

---

## Part 9 — Decay and maintenance

Voltex memory decays unless pinned. Each `DREAM` cycle multiplies unpinned node vitality by `0.95`. A node crosses the purge threshold after approximately 32 cycles.

**Recommended: run DREAM on a background timer**

```python
import threading

def dream_loop(interval_seconds=60):
    vault.dream()
    threading.Timer(interval_seconds, dream_loop, args=[interval_seconds]).start()

dream_loop(60)   # decay every minute
```

**What to pin:**
- Everything in `goals/`, `facts/`, `context/`, `people/`
- Any root hash you store manually

**What not to pin:**
- `scratch/` entries — let them decay
- Temporary computation results

---

## Part 10 — Troubleshooting

**`voltex_api` fails to start with a missing DLL (Windows)**

Copy the OpenSSL DLLs next to the executable:
```powershell
copy C:\vcpkg\installed\x64-windows\bin\libssl-3-x64.dll   .
copy C:\vcpkg\installed\x64-windows\bin\libcrypto-3-x64.dll .
```

**`python voltex_client.py` → `ConnectionRefusedError`**

`voltex_api` is not running, or is on a different port. Check that the server started successfully and that `VoltexClient(port=...)` matches.

**Dashboard shows `localhost:7474 ✗` and stays in demo mode**

`voltex_proxy.py` is not running. Start it in a separate terminal. The dashboard retries every 3 seconds automatically.

**`LOOKUP` returns `node has decayed`**

The node was not pinned and has been purged by DREAM cycles. Re-ingest the content and pin it. If you need it to survive restarts, always call `PIN` after `INGEST`.

**Registry is empty after restart**

`registry.vtxr` is written automatically on every `REGISTER`/`FORGET`. If it is missing, the vault directory may be wrong. Make sure `voltex_api` runs from the same working directory each time.

**`vault.meta` is empty after restart**

You did not call `SAVE` before shutting down. `vault.blob` is always current but the meta index must be saved explicitly. Add the autosave pattern from Part 7.

**Strings longer than 1024 characters are truncated**

The default `MAX_BUF` in `voltex_api.cpp` is 1024. Increase it and recompile:
```cpp
static constexpr int MAX_BUF = 8192;
```

---

## Quick reference card

```bash
# Build
g++ -std=c++17 -O2 voltex_api.cpp -lssl -lcrypto -lpthread -o voltex_api

# Run stack
./voltex_api &
python voltex_proxy.py &
open voltex_dashboard.html

# Python usage
from voltex_client import VoltexClient
v = VoltexClient()
h = v.ingest("my goal text")
v.pin(h)
v.register("goals/my-goal", h)
print(v.rlist())
print(v.lookup("goals/my-goal"))
v.forget("goals/my-goal")
v.save()
```

---

*Voltex · voltex_api.cpp · voltex_client.py · voltex_proxy.py · voltex_dashboard.html*
