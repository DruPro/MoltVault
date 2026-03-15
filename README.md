# ⚡ Voltex

**A content-addressed, decay-aware memory vault for LLMs.**

Voltex gives language models a persistent working memory they actively manage — storing goals, facts, and context as cryptographic nodes in a Merkle DAG, with vitality decay that makes old memories fade naturally unless pinned.

```
voltex_vault# INGEST
Enter data: build the Voltex integration layer
Root ID: a3f8c2e1d4b7f091...

voltex_vault# PIN
a3f8c2e1d4b7f091...
[PIN] Node is IMMORTAL.
```

---

## What it is

Most LLM memory systems are passive — they store embeddings and retrieve them when a query is close enough. Voltex is different: the LLM **actively manages** its own memory state. It decides what to remember, promotes it, names it, and releases it when done. Memory has a lifecycle.

Three properties make Voltex structurally distinct:

**Content addressing.** Every string has a deterministic 64-character root ID derived from a Merkle DAG of SHA-256 hashes. The same input always produces the same ID. You can verify any memory by re-ingesting it and comparing.

**Structural deduplication.** Two strings sharing a common substring share the actual graph nodes that encode it. Storage grows with *unique content*, not ingestion volume. 1000 JSON records with shared field names collapse to 538 unique 4-byte atom nodes.

**Vitality decay.** Every node carries a float vitality score. Unpinned nodes decay toward zero across DREAM cycles and are eventually purged. Pinned nodes are exempt — they survive indefinitely. The LLM controls what survives.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your LLM App                        │
│              (Python  ·  voltex_client.py)              │
└─────────────────────┬───────────────────────────────────┘
                      │  TCP · port 7474
                      │  newline-delimited JSON
┌─────────────────────▼───────────────────────────────────┐
│              voltex_api  (C++ server)                   │
│   vault.meta  ·  vault.blob  ·  registry.vtxr           │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│           voltex_proxy.py  (WS ↔ TCP bridge)            │
└─────────────────────┬───────────────────────────────────┘
                      │  WebSocket
┌─────────────────────▼───────────────────────────────────┐
│         voltex_dashboard.html  (browser UI)             │
└─────────────────────────────────────────────────────────┘
```

| Component | Language | Role |
|---|---|---|
| `voltex_paged.cpp` | C++ | Interactive REPL — development and exploration |
| `voltex_api.cpp` | C++ | TCP JSON server — production API |
| `voltex_client.py` | Python | Client library + LLM tool definitions |
| `voltex_proxy.py` | Python | WebSocket ↔ TCP bridge for the dashboard |
| `voltex_dashboard.html` | HTML/JS | Browser memory dashboard |
| `docker-compose.yml` | Docker | One-command full stack |

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/yourname/voltex.git
cd voltex
docker compose up --build
```

Open **http://localhost:8080** — the memory dashboard connects automatically.

> Vault data persists in a named Docker volume across restarts.  
> `docker compose down -v` to wipe all memory and start fresh.

### Manual

**Prerequisites:** C++17 compiler, OpenSSL, Python 3.9+

```bash
# Build the vault server
g++ -std=c++17 -O2 voltex_api.cpp -lssl -lcrypto -lpthread -o voltex_api

# Install the one Python dependency
pip install websockets

# Start everything (three terminals)
./voltex_api
python voltex_proxy.py
open voltex_dashboard.html
```

See [SETUP.md](SETUP.md) for platform-specific build instructions (Windows/Linux/macOS).

---

## How the LLM uses it

The model is given 8 tools. It calls them autonomously to manage its own memory state.

### The 8 tools

| Tool | What the LLM does with it |
|---|---|
| `voltex_ingest(text)` | Store something, receive a root hash |
| `voltex_pin(hash)` | Commit to remembering it — prevents decay |
| `voltex_unpin(hash)` | Release it — begins decaying |
| `voltex_unroll(hash)` | Recall exact text from a hash |
| `voltex_register(label, hash)` | Name a hash: `"goals/learn-transformers"` |
| `voltex_lookup(label)` | Retrieve by name → hash + text in one call |
| `voltex_rlist(namespace?)` | List all active memory, optionally filtered |
| `voltex_forget(label)` | Unpin + deregister — the memory fades away |

DREAM cycles run on a background timer. The LLM never calls it directly.

### Goal lifecycle

```
User: "I want to learn how transformers work."

  LLM calls voltex_ingest("learn how transformers work")  → hash_A
  LLM calls voltex_pin(hash_A)
  LLM calls voltex_register("goals/learn-transformers", hash_A)

     ... conversation continues across many turns ...
     ... voltex_rlist() at the start of each turn shows the goal ...

User: "I finished the transformer paper."

  LLM calls voltex_forget("goals/learn-transformers")
     └─ unpinned, removed from registry, will decay away

  LLM calls voltex_ingest("Completed: read the Attention Is All You Need paper")  → hash_B
  LLM calls voltex_pin(hash_B)
  LLM calls voltex_register("facts/transformers-studied", hash_B)
     └─ goal is gone, the fact that it happened persists
```

### Memory namespaces

```
goals/       active intentions the LLM is pursuing     (always pinned)
facts/       things the user told it that should last   (always pinned)
context/     what is being worked on right now          (always pinned)
people/      names, relationships, preferences          (always pinned)
scratch/     temporary working notes                    (no pin — auto-decays)
```

### Python integration

```python
from voltex_client import VoltexClient, ANTHROPIC_TOOL_DEFINITIONS, EXAMPLE_SYSTEM_PROMPT
import anthropic

vault  = VoltexClient()
client = anthropic.Anthropic()

conversation = []

def chat(user_message):
    conversation.append({"role": "user", "content": user_message})
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=EXAMPLE_SYSTEM_PROMPT,
            tools=ANTHROPIC_TOOL_DEFINITIONS,
            messages=conversation,
        )
        tool_results = []
        reply = ""
        for block in response.content:
            if block.type == "text":
                reply += block.text
            elif block.type == "tool_use":
                result = vault.call(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
        if tool_results:
            conversation.append({"role": "assistant", "content": response.content})
            conversation.append({"role": "user",      "content": tool_results})
            continue
        conversation.append({"role": "assistant", "content": reply})
        return reply
```

OpenAI-compatible tool definitions are also included — see `voltex_client.py`.

---

## The dashboard

Open `http://localhost:8080` (Docker) or `voltex_dashboard.html` (local).

- **Live memory view** — all registry entries grouped by namespace, with vitality bars
- **Node detail** — inspect any memory: content, hash, vitality, pin status
- **One-click actions** — PIN, UNPIN, FORGET, UNROLL directly from the UI
- **Command terminal** — send raw JSON commands and see live responses
- **Quick ingest** — store and register new memories without touching code
- **Goal lifecycle guide** — the full workflow visualised in the sidebar
- **Demo mode** — works offline with synthetic data if no server is running; auto-connects when the server comes up

---

## The vault internals

### Paged memory model

| Layer | File | Residency |
|---|---|---|
| NodeMeta | `vault.meta` | Always in RAM. Identity, child refs, vitality, pin flag, dendrite list. ~120 bytes/node. |
| NodeBlob | `vault.blob` | Append-only disk log. Raw lexeme text for ATOM nodes. LRU cache (default 4096 hot blobs). |

DREAM cycles run entirely on the NodeMeta layer — zero disk I/O. Only UNROLL and AUDIT touch blobs.

### Ingestion

1. **Leaf creation.** Strings ≤ 4 chars become ATOM nodes. ID = SHA-256 of the raw text. Existing atoms are revitalized.
2. **Hebbian split.** Longer strings are split at the point of weakest structural connection — where the vitality product of the two resulting substrings is lowest. Tends to split at word boundaries.
3. **Merkle combination.** A CHUNK node is created with ID = SHA-256(child_a ‖ child_b). The parent's identity is entirely determined by its children.
4. **Dendrite registration.** Both children record the new parent, enabling upward traversal for surgical repair.

### Decay math

```
vitality(n) = vitality(n-1) × 0.95    (each DREAM cycle)
purge when vitality < 0.2

cycles to purge ≈ log(0.2) / log(0.95) ≈ 32 cycles
```

Pinned nodes are exempt. Their vitality is frozen at 1.0.

### Registry

The registry (`registry.vtxr`) is a flat tab-separated file mapping human-readable labels to hashes:

```
goals/learn-transformers    a3f8c2e1...    goals    1
facts/user-dark-mode        00ff1234...    facts    1
scratch/looked-up-sha256    b7c3d4e5...    scratch  0
```

It is auto-saved on every `REGISTER` and `FORGET`. The LLM never has to manage hashes directly — it works entirely through labels.

---

## API protocol

The server speaks newline-delimited JSON over TCP. One command per line, one response per line. Connections are persistent and can multiplex multiple commands.

```bash
# Test with netcat
echo '{"cmd":"STATUS"}' | nc localhost 7474
# → {"ok":true,"atoms":538,"chunks":86647,"total":87185,...}

echo '{"cmd":"INGEST","text":"hello world"}' | nc localhost 7474
# → {"ok":true,"hash":"2cf24dba...","vitality":1.0}
```

### All 12 commands

| Command | Fields | Returns |
|---|---|---|
| `INGEST` | `text` | `hash`, `vitality` |
| `UNROLL` | `hash` | `text` |
| `PIN` | `hash` | `status: "immortal"` |
| `UNPIN` | `hash` | `status: "decaying"`, `vitality` |
| `DREAM` | — | `purged`, `remaining` |
| `SAVE` | — | `nodes`, `registry_entries` |
| `LOAD` | — | `nodes`, `registry_entries` |
| `STATUS` | — | full vault stats |
| `REGISTER` | `label`, `hash` | `label`, `hash` |
| `LOOKUP` | `label` | `label`, `hash`, `text`, `vitality`, `pinned` |
| `RLIST` | `namespace` (opt.) | `entries[]`, `count` |
| `FORGET` | `label` | `label`, `status: "forgotten"` |

---

## File reference

```
voltex/
├── voltex_paged.cpp          REPL vault (reference / dev)
├── vtx_export.cpp            Export utility (.vtxe portable snapshots)
├── vtx_test.cpp              Test harness
├── voltex_api.cpp            TCP JSON server (production)
├── voltex_client.py          Python client + tool definitions
├── voltex_proxy.py           WebSocket ↔ TCP bridge
├── voltex_dashboard.html     Browser memory dashboard
├── docker-compose.yml        One-command full stack
├── .env.example              Port configuration template
├── .dockerignore
├── docker/
│   ├── Dockerfile.api        Two-stage C++ build
│   ├── Dockerfile.proxy      Alpine Python image
│   ├── Dockerfile.frontend   nginx static server
│   └── nginx.conf            WebSocket proxy config
├── SETUP.md                  Full setup guide
└── VOLTEX_API_GUIDE.md       API reference
```

**Generated at runtime (not in repo)**

```
vault.meta        NodeMeta index  (written by SAVE)
vault.blob        Atom text store (written continuously)
registry.vtxr     Label registry  (written automatically)
```

---

## Configuration

Key constants in `voltex_api.cpp`:

| Constant | Default | Effect |
|---|---|---|
| `DECAY_MULTIPLIER` | `0.95` | Vitality multiplier per DREAM cycle. Lower = faster forgetting. |
| `DECAY_THRESHOLD` | `0.2` | Vitality floor before purge. |
| `MAX_HOT_BLOBS` | `4096` | LRU blob cache size. Increase for heavy UNROLL workloads. |
| `MAX_BUF` | `1024` | Max reconstructed string length. Raise for long documents. |
| `ATOM_LEAF_SIZE` | `4` | Max chars per ATOM leaf. Smaller = deeper trees, finer dedup. |

Port defaults (override via environment or `.env`):

```bash
VOLTEX_PORT=7474      # vault TCP server
WS_PORT=8765          # WebSocket proxy
FRONTEND_PORT=8080    # dashboard (Docker only)
```

---

## Building from source

| Platform | Command |
|---|---|
| Linux | `g++ -std=c++17 -O2 voltex_api.cpp -lssl -lcrypto -lpthread -o voltex_api` |
| macOS | `g++ -std=c++17 -O2 voltex_api.cpp -I$(brew --prefix openssl)/include -L$(brew --prefix openssl)/lib -lssl -lcrypto -lpthread -o voltex_api` |
| Windows | See [SETUP.md](SETUP.md) — requires vcpkg + OpenSSL |

**Dependencies:** OpenSSL (SHA-256 only), C++17 stdlib, POSIX sockets (Linux/macOS) or Winsock2 (Windows).

---

## Use cases

**LLM working memory.** The primary design target. An LLM maintains goals, facts, and context across sessions without a vector database. Memory has intentionality — the model decides what matters, not a retrieval score.

**Deduplication-aware text storage.** Any corpus where documents share common substrings — config files, log lines, API responses from the same schema — stores proportionally to unique content, not volume.

**Content fingerprinting.** Every string has a deterministic root ID. Two strings differing by one character produce entirely different IDs due to Merkle hash propagation. The root ID is the fingerprint.

**Decaying cache.** The vitality/dream cycle models a memory system where infrequently accessed data expires smoothly rather than hard-expiring. Maps naturally to session data, TTL caches, and relevance-weighted storage.

**Portable vault snapshots.** The companion `vtx_export.cpp` utility converts the vault to a compact `.vtxe` format (positional addressing replaces 32-byte hash references with 4-byte row indices — ~61% size reduction) that can be archived, transmitted, or loaded into a fresh instance.
---

## Local LLM integration — BitNet 1.58b

Run the full stack **entirely offline** using BitNet's 1-bit quantised Llama3-8B. No cloud API, no GPU required (CPU works, just slower).

### What it does

`voltex_bitnet.py` runs a local LLM that actively manages its own Voltex memory — calling `rlist` at the start of each turn, storing new goals with `ingest → pin → register`, and releasing completed goals with `forget`. The model uses tool calls embedded directly in its output text, which the runner parses and dispatches to the Voltex API.

### Setup

```bash
# 1. Install the BitNet-compatible transformers fork
pip install git+https://github.com/huggingface/transformers.git@refs/pull/33410/head
pip install torch accelerate websockets

# 2. Start Voltex (one command)
docker compose up --build

# 3. Run the BitNet chat (downloads ~8GB weights on first run)
python voltex_bitnet.py
```

**No GPU?** Use `--cpu` flag (slower but works):
```bash
python voltex_bitnet.py --cpu
```

**Test the Voltex integration without a model:**
```bash
python voltex_bitnet.py --demo
```
Demo mode runs the full tool-call loop with synthetic model responses — useful for verifying your Voltex setup before the model downloads.

### How tool calls work

The model writes structured tags directly in its output:

```
<tool>{"action":"rlist","namespace":""}</tool>
→ <tool_result>{"ok":true,"entries":[...],"count":3}</tool_result>

<tool>{"action":"ingest","text":"learn transformer architecture"}</tool>
→ <tool_result>{"ok":true,"hash":"a3f8c2e1...","tip":"call pin+register to keep it"}</tool_result>

<tool>{"action":"pin","hash":"a3f8c2e1..."}</tool>
<tool>{"action":"register","label":"goals/learn-transformers","hash":"a3f8c2e1..."}</tool>
```

The runner processes all tool calls, injects results, and loops until the model stops calling tools — up to 6 rounds per turn.

### Built-in chat commands

```
memory   — print the full registry (what the model currently remembers)
dream    — manually trigger one decay cycle
save     — persist vault state to disk
status   — print vault statistics
quit     — save and exit
```

### Model details

| Property | Value |
|---|---|
| Base model | Llama-3-8B-Instruct |
| Architecture | BitNet 1.58b (1-bit quantisation) |
| Training | 100B tokens on FineWeb-edu |
| Weights | `HF1BitLLM/Llama3-8B-1.58-100B-tokens` |
| Tokenizer | `meta-llama/Meta-Llama-3-8B-Instruct` |
| Memory footprint | ~2GB (vs ~16GB for full bfloat16) |

> The BitNet 1.58b architecture represents each weight as one of `{-1, 0, +1}`, reducing the model to roughly 1/8th the memory of a standard Llama3-8B while retaining competitive performance on reasoning tasks.


---

## License

MIT
