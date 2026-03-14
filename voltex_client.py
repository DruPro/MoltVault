"""
voltex_client.py
────────────────────────────────────────────────────────────────────
Python client for the Voltex API server (voltex_api.cpp).

Exposes the 8 LLM-facing tools as clean Python functions plus
a VoltexClient class for connection management.

Usage — direct:
    from voltex_client import VoltexClient
    v = VoltexClient()          # connects to localhost:7474
    h = v.ingest("learn Voltex integration")
    v.pin(h)
    v.register("goals/learn-voltex", h)
    print(v.lookup("goals/learn-voltex"))

Usage — as OpenAI-compatible tool definitions (for LLM function calling):
    from voltex_client import TOOL_DEFINITIONS
    # Pass TOOL_DEFINITIONS to your LLM API call's `tools` parameter
    # Then route tool_call results through VoltexClient.call(name, args)

Protocol: newline-delimited JSON over TCP (same as voltex_api.cpp).
"""

import json
import socket
import threading
from typing import Optional


# ─── Connection ──────────────────────────────────────────────────────

class VoltexClient:
    """
    Thread-safe client for a running voltex_api server.
    One persistent TCP connection, protected by a lock.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7474):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._buf  = ""
        self._connect()

    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host, self.port))
        self._buf = ""

    def _send(self, payload: dict) -> dict:
        """Send one JSON command, return parsed response. Thread-safe."""
        with self._lock:
            line = json.dumps(payload) + "\n"
            self._sock.sendall(line.encode())
            # Read until newline
            while "\n" not in self._buf:
                chunk = self._sock.recv(4096).decode()
                if not chunk:
                    raise ConnectionError("Voltex server closed the connection")
                self._buf += chunk
            nl  = self._buf.index("\n")
            raw = self._buf[:nl]
            self._buf = self._buf[nl + 1:]
            return json.loads(raw)

    def _ok(self, resp: dict) -> dict:
        if not resp.get("ok"):
            raise RuntimeError(f"Voltex error: {resp.get('error', resp)}")
        return resp

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    # ── 8 LLM tools ──────────────────────────────────────────────────

    def ingest(self, text: str) -> str:
        """
        Store text in the vault.
        Returns the 64-char root hash.
        """
        r = self._ok(self._send({"cmd": "INGEST", "text": text}))
        return r["hash"]

    def pin(self, hash_: str) -> dict:
        """
        Grant immortality to a node — it will never decay.
        Pass the hash returned by ingest().
        Returns {"hash": ..., "status": "immortal"}.
        """
        return self._ok(self._send({"cmd": "PIN", "hash": hash_}))

    def unpin(self, hash_: str) -> dict:
        """
        Remove pin from a node — it will begin decaying.
        Returns {"hash": ..., "status": "decaying", "vitality": ...}.
        """
        return self._ok(self._send({"cmd": "UNPIN", "hash": hash_}))

    def unroll(self, hash_: str) -> str:
        """
        Reconstruct the original text from its hash.
        Returns the text string.
        """
        r = self._ok(self._send({"cmd": "UNROLL", "hash": hash_}))
        return r["text"]

    def register(self, label: str, hash_: str) -> dict:
        """
        Name a hash for easy retrieval — e.g. "goals/learn-voltex".
        Namespace is the prefix before '/'.  Saves to registry.vtxr.
        Returns {"label": ..., "hash": ...}.
        """
        return self._ok(self._send({"cmd": "REGISTER", "label": label, "hash": hash_}))

    def lookup(self, label: str) -> dict:
        """
        Resolve a label to its hash AND unrolled text in one call.
        Returns {"label", "hash", "text", "vitality", "pinned"}.
        Raises if node has decayed.
        """
        return self._ok(self._send({"cmd": "LOOKUP", "label": label}))

    def rlist(self, namespace: str = "") -> list:
        """
        List all registry entries, optionally filtered by namespace.
        e.g. rlist("goals") → only entries whose label starts with "goals/".
        Returns list of {"label", "hash", "namespace", "vitality", "pinned", "alive"}.
        """
        r = self._ok(self._send({"cmd": "RLIST", "namespace": namespace}))
        return r["entries"]

    def forget(self, label: str) -> dict:
        """
        Unpin the node AND remove the label from the registry.
        The node will then decay away over ~32 DREAM cycles.
        Returns {"label": ..., "status": "forgotten", "hash": ...}.
        """
        return self._ok(self._send({"cmd": "FORGET", "label": label}))

    # ── Utility tools (not the 8 primary LLM tools) ──────────────────

    def dream(self) -> dict:
        """Run one decay cycle. Returns {"purged": N, "remaining": N}."""
        return self._ok(self._send({"cmd": "DREAM"}))

    def save(self) -> dict:
        """Persist vault.meta + registry.vtxr to disk."""
        return self._ok(self._send({"cmd": "SAVE"}))

    def load(self) -> dict:
        """Reload vault.meta + registry.vtxr from disk."""
        return self._ok(self._send({"cmd": "LOAD"}))

    def status(self) -> dict:
        """
        Return live vault statistics.
        {"atoms", "chunks", "total", "max_depth", "blob_bytes",
         "hot_blobs", "cache_hit_rate", "registry_entries"}
        """
        return self._ok(self._send({"cmd": "STATUS"}))

    # ── Generic dispatcher (for LLM tool_call routing) ────────────────

    def call(self, tool_name: str, args: dict) -> dict | str:
        """
        Route an LLM tool_call by name.
        Returns the raw result (str for ingest/unroll, dict otherwise).
        """
        dispatch = {
            "voltex_ingest":   lambda a: self.ingest(a["text"]),
            "voltex_pin":      lambda a: self.pin(a["hash"]),
            "voltex_unpin":    lambda a: self.unpin(a["hash"]),
            "voltex_unroll":   lambda a: self.unroll(a["hash"]),
            "voltex_register": lambda a: self.register(a["label"], a["hash"]),
            "voltex_lookup":   lambda a: self.lookup(a["label"]),
            "voltex_rlist":    lambda a: self.rlist(a.get("namespace", "")),
            "voltex_forget":   lambda a: self.forget(a["label"]),
        }
        if tool_name not in dispatch:
            raise ValueError(f"Unknown Voltex tool: {tool_name}")
        return dispatch[tool_name](args)


# ─── OpenAI-compatible tool definitions ──────────────────────────────
# Pass TOOL_DEFINITIONS directly to the `tools` parameter of your LLM
# API call.  Works with OpenAI, Anthropic, and any OpenAI-compatible API.

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "voltex_ingest",
            "description": (
                "Store a piece of text in Voltex vault memory. "
                "Returns a stable hash (root ID) you can use to retrieve "
                "or pin this content later. Use this whenever you want to "
                "remember something across turns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to store."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_pin",
            "description": (
                "Grant immortality to a stored node — it will never be "
                "removed by decay cycles. Use immediately after ingest() "
                "for any memory you want to keep indefinitely (goals, "
                "facts, user preferences)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "64-character root hash returned by voltex_ingest."
                    }
                },
                "required": ["hash"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_unpin",
            "description": (
                "Remove the immortality pin from a node, allowing it to "
                "decay naturally over the next ~32 dream cycles. Use when "
                "a goal is completed or a memory is no longer needed. "
                "Prefer voltex_forget() if you also want to remove the "
                "registry label."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "64-character root hash to unpin."
                    }
                },
                "required": ["hash"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_unroll",
            "description": (
                "Reconstruct and return the original text stored at a "
                "given hash. Use this to recall a specific memory when "
                "you have the hash but not the label."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "64-character root hash to retrieve."
                    }
                },
                "required": ["hash"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_register",
            "description": (
                "Give a memorable name to a hash so you can find it by "
                "label instead of hash later. Use namespaced labels like "
                "'goals/build-api', 'facts/user-dark-mode', "
                "'context/current-task', 'people/user-name'. "
                "Always register after ingest+pin for any important memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Human-readable label. Use namespace/name format. "
                            "Recommended namespaces: goals/, facts/, context/, "
                            "people/, scratch/"
                        )
                    },
                    "hash": {
                        "type": "string",
                        "description": "64-character root hash to name."
                    }
                },
                "required": ["label", "hash"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_lookup",
            "description": (
                "Retrieve a memory by its label. Returns the label, hash, "
                "full reconstructed text, current vitality, and pin status "
                "in one call. Use this at the start of each turn to recall "
                "context, goals, or facts by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Label as registered with voltex_register."
                    }
                },
                "required": ["label"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_rlist",
            "description": (
                "List everything currently in the registry, optionally "
                "filtered by namespace. Call with no namespace at the "
                "start of a conversation to see all active memory. "
                "Call with namespace='goals' to see only active goals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional namespace filter. Leave empty for all. "
                            "Examples: 'goals', 'facts', 'context', 'people', 'scratch'"
                        )
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "voltex_forget",
            "description": (
                "Remove a label from the registry AND unpin the node, "
                "allowing it to decay away naturally. Use when a goal is "
                "complete, a fact is outdated, or a memory is no longer "
                "relevant. The text is not immediately deleted — it fades "
                "over ~32 dream cycles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "The label to forget (as used in voltex_register)."
                    }
                },
                "required": ["label"]
            }
        }
    }
]


# ─── Anthropic-compatible tool definitions ────────────────────────────
# For use with the Anthropic Python SDK's `tools` parameter.

ANTHROPIC_TOOL_DEFINITIONS = [
    {
        "name": "voltex_ingest",
        "description": (
            "Store text in Voltex vault memory. Returns a stable root hash "
            "you can use to retrieve or pin the content. Use whenever you "
            "want to remember something across turns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to store."}
            },
            "required": ["text"]
        }
    },
    {
        "name": "voltex_pin",
        "description": (
            "Grant immortality to a stored node — prevents decay. Use for "
            "goals, facts, and user preferences you want to keep forever."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string", "description": "64-char root hash from voltex_ingest."}
            },
            "required": ["hash"]
        }
    },
    {
        "name": "voltex_unpin",
        "description": "Remove immortality pin from a node — lets it decay naturally.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string", "description": "64-char root hash to unpin."}
            },
            "required": ["hash"]
        }
    },
    {
        "name": "voltex_unroll",
        "description": "Reconstruct original text from its hash.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string", "description": "64-char root hash to retrieve."}
            },
            "required": ["hash"]
        }
    },
    {
        "name": "voltex_register",
        "description": (
            "Name a hash with a label like 'goals/build-api' for easy lookup. "
            "Use namespaces: goals/, facts/, context/, people/, scratch/."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Namespaced label e.g. 'goals/learn-voltex'."},
                "hash":  {"type": "string", "description": "64-char root hash to name."}
            },
            "required": ["label", "hash"]
        }
    },
    {
        "name": "voltex_lookup",
        "description": (
            "Retrieve a memory by label — returns label, hash, full text, "
            "vitality, and pin status. Use at conversation start to recall context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label as registered."}
            },
            "required": ["label"]
        }
    },
    {
        "name": "voltex_rlist",
        "description": (
            "List registry entries, optionally filtered by namespace. "
            "Call at start of conversation to see all active memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Optional filter: 'goals', 'facts', 'context', 'people', 'scratch', or '' for all."
                }
            },
            "required": []
        }
    },
    {
        "name": "voltex_forget",
        "description": (
            "Remove a label from registry and unpin the node so it decays. "
            "Use when a goal completes or a memory becomes irrelevant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label to forget."}
            },
            "required": ["label"]
        }
    }
]


# ─── Example: minimal chatbot integration ────────────────────────────

EXAMPLE_SYSTEM_PROMPT = """
You have access to Voltex, a persistent memory vault. Use it actively.

AT THE START OF EVERY CONVERSATION:
  Call voltex_rlist() with no namespace to see everything you currently remember.
  This is your working memory — read it before responding.

WHEN THE USER GIVES YOU A GOAL OR TASK:
  1. voltex_ingest(text)     — store the goal text, get back a hash
  2. voltex_pin(hash)        — make it immortal
  3. voltex_register(label, hash)  — name it "goals/<short-name>"
  Now you'll see it in every future rlist() call.

WHEN A GOAL IS COMPLETE:
  1. voltex_forget("goals/<name>")  — unpin + deregister
  2. voltex_ingest("Completed: <what was accomplished>")  — store the fact
  3. voltex_pin(hash)
  4. voltex_register("facts/<name>", hash)

FOR PERSISTENT USER FACTS (preferences, name, context):
  voltex_ingest → voltex_pin → voltex_register("facts/<name>", hash)

FOR TEMPORARY WORKING NOTES:
  voltex_ingest only (no pin, no register) — they decay away automatically.

Namespaces:
  goals/     active intentions you are pursuing
  facts/     things the user told you that should persist
  context/   what you are working on right now
  people/    names, preferences, relationships
  scratch/   temporary notes (no pin — auto-decays)
""".strip()


if __name__ == "__main__":
    # Quick smoke test — requires voltex_api server running on port 7474
    import sys
    print("Connecting to Voltex API...")
    try:
        v = VoltexClient()
    except ConnectionRefusedError:
        print("ERROR: No Voltex API server found on localhost:7474")
        print("Build and run voltex_api.cpp first.")
        sys.exit(1)

    print("Status:", v.status())

    # Store a goal
    h = v.ingest("Build a Voltex LLM integration layer")
    print(f"Ingested: {h[:16]}...")
    v.pin(h)
    v.register("goals/build-integration", h)

    # Store a fact
    hf = v.ingest("User prefers concise responses")
    v.pin(hf)
    v.register("facts/user-concise", hf)

    # List everything
    entries = v.rlist()
    print(f"\nRegistry ({len(entries)} entries):")
    for e in entries:
        print(f"  [{e['namespace']:8s}] {e['label']:35s} pinned={e['pinned']} alive={e['alive']}")

    # Lookup
    result = v.lookup("goals/build-integration")
    print(f"\nLookup: {result['text'][:50]}...")

    # Complete the goal
    v.forget("goals/build-integration")
    hc = v.ingest("Completed: Voltex LLM integration layer built")
    v.pin(hc)
    v.register("facts/integration-complete", hc)

    print("\nAfter completing goal:")
    for e in v.rlist():
        print(f"  {e['label']}")

    v.save()
    print("\nSaved. Done.")
