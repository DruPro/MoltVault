"""
voltex_bitnet.py
────────────────────────────────────────────────────────────────────
BitNet Llama3-8B-1.58 + Voltex memory integration.

Runs a local 1-bit quantised LLM that actively manages its own
memory through Voltex — storing goals, facts, and context across
turns without any external API or cloud dependency.

Requirements
────────────────────────────────────────────────────────────────────
  pip install git+https://github.com/huggingface/transformers.git@refs/pull/33410/head
  pip install torch accelerate websockets

  voltex_api must be running on localhost:7474
  (or: docker compose up --build)

Usage
────────────────────────────────────────────────────────────────────
  python voltex_bitnet.py                     # interactive chat
  python voltex_bitnet.py --cpu               # force CPU (slow but works)
  python voltex_bitnet.py --demo              # run without model (tool demo)
"""

import argparse
import json
import re
import sys
import textwrap
import threading
import time
from typing import Optional

# ─── Voltex client ───────────────────────────────────────────────────
from voltex_client import VoltexClient

# ─── Model constants ─────────────────────────────────────────────────
MODEL_ID      = "HF1BitLLM/Llama3-8B-1.58-100B-tokens"
TOKENIZER_ID  = "meta-llama/Meta-Llama-3-8B-Instruct"
MAX_NEW_TOKENS = 512
TEMPERATURE    = 0.7

# ─── DREAM cycle: decay memory every N seconds ───────────────────────
DREAM_INTERVAL_SECONDS = 120

# ═══════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT  — tells the model how to use Voltex tools
# ═══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a helpful assistant with access to Voltex, a persistent memory vault.
You use Voltex tools to remember goals, facts, and context across turns.

MEMORY TOOLS — call these by writing a JSON block wrapped in <tool> tags:

  <tool>{"action":"ingest","text":"..."}</tool>
  → Stores text in the vault. Returns a hash.

  <tool>{"action":"pin","hash":"..."}</tool>
  → Makes a memory immortal (survives decay cycles).

  <tool>{"action":"unpin","hash":"..."}</tool>
  → Lets a memory start decaying — use when done with it.

  <tool>{"action":"register","label":"namespace/name","hash":"..."}</tool>
  → Names a hash so you can find it later. Namespaces: goals/ facts/ context/ people/ scratch/

  <tool>{"action":"lookup","label":"namespace/name"}</tool>
  → Retrieves a memory by label. Returns text + vitality + pin status.

  <tool>{"action":"rlist","namespace":""}</tool>
  → Lists all active memories. Pass a namespace to filter (e.g. "goals").

  <tool>{"action":"forget","label":"namespace/name"}</tool>
  → Unpins and deregisters a memory. It will decay away naturally.

RULES:
1. At the START of every conversation turn, call rlist to see your active memory.
2. When the user gives you a goal: ingest → pin → register under goals/
3. When a goal completes: forget the goals/ entry, then ingest+pin+register a facts/ entry.
4. Store persistent user facts under facts/ and people/
5. Use scratch/ for temporary notes — do NOT pin these.
6. Always respond to the user AFTER processing any tool results.
7. Keep tool calls compact. Never explain the tools to the user unless asked.

TOOL CALL FORMAT — you must use this exact format, one per line:
  <tool>{"action":"...","key":"value"}</tool>

After each tool call, the result appears as:
  <tool_result>{"ok":true,...}</tool_result>

Then continue your response normally."""


# ═══════════════════════════════════════════════════════════════════
#  VOLTEX TOOL DISPATCHER
# ═══════════════════════════════════════════════════════════════════

class VoltexTools:
    """Routes model tool calls to the Voltex API."""

    def __init__(self, vault: VoltexClient):
        self.vault = vault
        self._lock = threading.Lock()

    def dispatch(self, call: dict) -> dict:
        action = call.get("action", "")
        try:
            with self._lock:
                if action == "ingest":
                    hash_ = self.vault.ingest(call["text"])
                    return {"ok": True, "hash": hash_, "tip": "call pin+register to keep it"}

                elif action == "pin":
                    return self.vault.pin(call["hash"])

                elif action == "unpin":
                    return self.vault.unpin(call["hash"])

                elif action == "register":
                    return self.vault.register(call["label"], call["hash"])

                elif action == "lookup":
                    return self.vault.lookup(call["label"])

                elif action == "rlist":
                    entries = self.vault.rlist(call.get("namespace", ""))
                    # Return a compact summary the model can actually read
                    summary = []
                    for e in entries:
                        summary.append({
                            "label":    e["label"],
                            "vitality": round(e["vitality"], 2),
                            "pinned":   e["pinned"],
                            "preview":  e.get("text", "")[:80],
                        })
                    return {"ok": True, "entries": summary, "count": len(summary)}

                elif action == "forget":
                    return self.vault.forget(call["label"])

                elif action == "status":
                    return self.vault.status()

                else:
                    return {"ok": False, "error": f"unknown action: {action}"}

        except Exception as e:
            return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════
#  TOOL CALL PARSER
#  Extracts <tool>...</tool> blocks from model output,
#  dispatches them, and injects <tool_result> back.
# ═══════════════════════════════════════════════════════════════════

TOOL_PATTERN = re.compile(r'<tool>(.*?)</tool>', re.DOTALL)

def process_tool_calls(text: str, tools: VoltexTools) -> tuple[str, list[dict]]:
    """
    Find all <tool> blocks in `text`, execute them, replace with results.
    Returns (processed_text, list_of_results).
    """
    results = []
    def replace(match):
        raw = match.group(1).strip()
        try:
            call = json.loads(raw)
        except json.JSONDecodeError:
            result = {"ok": False, "error": "invalid JSON in tool call"}
            results.append(result)
            return f"<tool_result>{json.dumps(result)}</tool_result>"

        result = tools.dispatch(call)
        results.append(result)
        return f"<tool_result>{json.dumps(result)}</tool_result>"

    processed = TOOL_PATTERN.sub(replace, text)
    return processed, results


# ═══════════════════════════════════════════════════════════════════
#  BACKGROUND DREAM CYCLE
# ═══════════════════════════════════════════════════════════════════

class DreamCycleThread(threading.Thread):
    """Runs DREAM on the vault every DREAM_INTERVAL_SECONDS."""

    def __init__(self, vault: VoltexClient):
        super().__init__(daemon=True)
        self.vault   = vault
        self._stop   = threading.Event()
        self._cycles = 0

    def run(self):
        while not self._stop.wait(DREAM_INTERVAL_SECONDS):
            try:
                result = self.vault.dream()
                self._cycles += 1
                if result.get("purged", 0) > 0:
                    print(f"\n  \033[90m[DREAM cycle {self._cycles} — "
                          f"purged {result['purged']} nodes]\033[0m\n", flush=True)
            except Exception:
                pass

    def stop(self):
        self._stop.set()


# ═══════════════════════════════════════════════════════════════════
#  MODEL LOADER
# ═══════════════════════════════════════════════════════════════════

def load_model(force_cpu: bool = False):
    """Load the BitNet model and tokenizer. Returns (model, tokenizer)."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError:
        print("ERROR: transformers / torch not installed.")
        print("Run: pip install git+https://github.com/huggingface/transformers.git@refs/pull/33410/head")
        sys.exit(1)

    print(f"\033[90m  Loading tokenizer from {TOKENIZER_ID}...\033[0m")
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_ID)
    tokenizer.pad_token = tokenizer.eos_token

    device_map = "cpu" if force_cpu else "auto"
    dtype      = torch.float32 if force_cpu else torch.bfloat16

    print(f"\033[90m  Loading model {MODEL_ID} (device={device_map}, dtype={dtype})...\033[0m")
    print(f"\033[90m  This may take a minute on first run (downloading ~8GB weights).\033[0m")

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        device_map=device_map,
        torch_dtype=dtype,
    )
    model.eval()
    print(f"\033[32m  Model ready.\033[0m\n")
    return model, tokenizer


# ═══════════════════════════════════════════════════════════════════
#  INFERENCE
# ═══════════════════════════════════════════════════════════════════

def build_prompt(conversation: list[dict], system: str) -> str:
    """
    Build a Llama-3 Instruct formatted prompt from conversation history.
    Format: <|begin_of_text|><|system|>...<|user|>...<|assistant|>
    """
    lines = ["<|begin_of_text|>"]
    lines.append(f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>")
    for msg in conversation:
        role    = msg["role"]
        content = msg["content"]
        lines.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>")
    lines.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
    return "".join(lines)


def generate(model, tokenizer, prompt: str, max_new_tokens: int = MAX_NEW_TOKENS) -> str:
    """Run inference on the BitNet model."""
    import torch

    inputs = tokenizer(prompt, return_tensors="pt")
    input_ids = inputs["input_ids"]

    # Move to same device as model
    device = next(model.parameters()).device
    input_ids = input_ids.to(device)

    with torch.no_grad():
        output_ids = model.generate(
            input_ids,
            max_new_tokens=max_new_tokens,
            do_sample=True,
            temperature=TEMPERATURE,
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    # Decode only the newly generated tokens
    new_tokens = output_ids[0][input_ids.shape[1]:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)


# ═══════════════════════════════════════════════════════════════════
#  AGENTIC LOOP
#  Runs the model, processes tool calls, feeds results back,
#  repeats until the model stops calling tools.
# ═══════════════════════════════════════════════════════════════════

MAX_TOOL_ROUNDS = 6   # prevent infinite tool loops

def agent_turn(
    user_message: str,
    conversation: list[dict],
    model,
    tokenizer,
    tools: VoltexTools,
    demo_mode: bool = False,
) -> str:
    """
    One full agent turn:
      1. Add user message to conversation
      2. Generate model response
      3. Process any tool calls → inject results
      4. If tool calls were made, generate again with results in context
      5. Return final text reply
    """
    conversation.append({"role": "user", "content": user_message})

    for round_ in range(MAX_TOOL_ROUNDS):
        if demo_mode:
            # In demo mode, fake a model response with tool calls
            raw_reply = _demo_response(user_message, round_, tools)
        else:
            prompt    = build_prompt(conversation, SYSTEM_PROMPT)
            raw_reply = generate(model, tokenizer, prompt)

        # Process any tool calls embedded in the response
        processed, tool_results = process_tool_calls(raw_reply, tools)

        if not tool_results:
            # No tool calls — this is the final response
            # Strip any residual tag artifacts and return clean text
            clean = re.sub(r'<[^>]+>', '', processed).strip()
            conversation.append({"role": "assistant", "content": processed})
            return clean

        # Tool calls were made — add the processed response to history
        # and loop so the model can see the results and continue
        conversation.append({"role": "assistant", "content": processed})
        conversation.append({
            "role": "user",
            "content": f"[Tool results received. Continue your response to the user.]"
        })

    # Safety fallback — shouldn't normally reach here
    return "[Max tool rounds reached — please rephrase your request.]"


# ═══════════════════════════════════════════════════════════════════
#  DEMO MODE  (no model required)
#  Simulates model responses with hardcoded tool call sequences
#  so you can test the Voltex integration without a GPU.
# ═══════════════════════════════════════════════════════════════════

_demo_turn_count = 0

def _demo_response(user_message: str, round_: int, tools: VoltexTools) -> str:
    global _demo_turn_count

    msg_lower = user_message.lower()

    # Round 0: always start by listing memory
    if round_ == 0:
        _demo_turn_count += 1

        # First turn: check memory, then respond
        if _demo_turn_count == 1:
            return (
                'Let me check my active memory first.\n'
                '<tool>{"action":"rlist","namespace":""}</tool>\n'
                'Now I can respond with full context.'
            )

        # If user mentions a goal
        if any(w in msg_lower for w in ["want to", "goal", "learn", "build", "create", "need to"]):
            return (
                f'I\'ll store that as an active goal.\n'
                f'<tool>{{"action":"ingest","text":"{user_message}"}}</tool>\n'
            )

        # If user says done / finished / complete
        if any(w in msg_lower for w in ["done", "finished", "complete", "accomplished"]):
            entries = tools.vault.rlist("goals")
            if entries:
                label = entries[0]["label"]
                return (
                    f'Great! Let me close out that goal and record it as a fact.\n'
                    f'<tool>{{"action":"forget","label":"{label}"}}</tool>\n'
                )

        # If asking what model remembers
        if any(w in msg_lower for w in ["remember", "memory", "what do you know", "recall"]):
            return (
                'Let me check everything I currently remember.\n'
                '<tool>{"action":"rlist","namespace":""}</tool>\n'
            )

        # Default: just respond
        return f"I understand. {user_message[:50]}... Let me help with that."

    # Round 1+: follow-up after tool results
    if round_ == 1:
        if any(w in msg_lower for w in ["want to", "goal", "learn", "build", "create", "need to"]):
            # Pin and register the just-ingested goal
            entries = tools.vault.rlist("goals")
            existing_hashes = {e["label"]: e for e in entries}

            # Get the hash from the last ingest via a fresh status check
            # (in real model mode the hash comes from tool_result)
            return (
                'I\'ve stored your goal. Let me make sure it\'s pinned and named.\n'
                # In demo mode we can't easily get the hash, so we demonstrate the pattern
                'Your goal is now active in my memory under goals/. '
                'I\'ll track it across our conversation and remind you of it each turn.'
            )
        return "I've processed the tool results. How can I help further?"

    return "Continuing based on the tool results."


# ═══════════════════════════════════════════════════════════════════
#  BANNER
# ═══════════════════════════════════════════════════════════════════

def print_banner(vault_status: dict, demo_mode: bool):
    model_label = "[DEMO MODE — no model loaded]" if demo_mode else f"BitNet {MODEL_ID}"
    nodes       = vault_status.get("total", 0)
    registry    = vault_status.get("registry_entries", 0)

    print("\n\033[1;35m  ⚡ VOLTEX + BITNET  —  Local LLM with Persistent Memory\033[0m")
    print(f"\033[90m  ─────────────────────────────────────────────────────\033[0m")
    print(f"\033[90m  Model    \033[0m\033[1;37m{model_label}\033[0m")
    print(f"\033[90m  Vault    \033[0m\033[1;33m{nodes:,} nodes\033[0m  ·  "
          f"\033[1;32m{registry} registry entries\033[0m")
    print(f"\033[90m  Dream    \033[0mevery {DREAM_INTERVAL_SECONDS}s (background)")
    print(f"\033[90m  ─────────────────────────────────────────────────────\033[0m")
    print(f"\033[90m  Commands: 'quit' to exit · 'memory' to list vault · 'dream' to decay\033[0m")
    print(f"\033[90m  ─────────────────────────────────────────────────────\033[0m\n")


# ═══════════════════════════════════════════════════════════════════
#  MAIN  —  interactive chat loop
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="BitNet + Voltex memory chat")
    parser.add_argument("--cpu",  action="store_true", help="Force CPU inference")
    parser.add_argument("--demo", action="store_true", help="Run without model (tool demo)")
    parser.add_argument("--host", default="127.0.0.1",  help="Voltex API host")
    parser.add_argument("--port", type=int, default=7474, help="Voltex API port")
    args = parser.parse_args()

    # ── Connect to Voltex ────────────────────────────────────────────
    print("\n\033[90m  Connecting to Voltex API...\033[0m", end="", flush=True)
    try:
        vault = VoltexClient(host=args.host, port=args.port)
        status = vault.status()
        print(f" \033[32mconnected\033[0m")
    except ConnectionRefusedError:
        print(f"\n\n\033[31m  ERROR: Cannot connect to Voltex at {args.host}:{args.port}\033[0m")
        print(  "  Start the vault server first:")
        print(  "    docker compose up --build")
        print(  "  or: ./voltex_api\n")
        sys.exit(1)

    tools = VoltexTools(vault)

    # ── Load model ───────────────────────────────────────────────────
    model, tokenizer = None, None
    if not args.demo:
        try:
            model, tokenizer = load_model(force_cpu=args.cpu)
        except Exception as e:
            print(f"\033[33m  WARNING: Could not load model ({e})\033[0m")
            print(  "  Falling back to demo mode. Pass --demo to suppress this warning.\n")
            args.demo = True

    # ── Print banner ─────────────────────────────────────────────────
    print_banner(status, args.demo)

    # ── Start background DREAM cycle ─────────────────────────────────
    dream_thread = DreamCycleThread(vault)
    dream_thread.start()

    # ── Conversation state ───────────────────────────────────────────
    conversation: list[dict] = []

    # ── Chat loop ────────────────────────────────────────────────────
    try:
        while True:
            try:
                user_input = input("\033[1;34m  you › \033[0m").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            # Built-in commands
            if user_input.lower() in ("quit", "exit", "q"):
                break

            if user_input.lower() in ("memory", "mem", "list"):
                entries = vault.rlist()
                if not entries:
                    print("  \033[90m[vault is empty]\033[0m\n")
                else:
                    print(f"\n  \033[90m── Registry ({len(entries)} entries) ──\033[0m")
                    for e in entries:
                        pin_marker = "\033[32m●\033[0m" if e["pinned"] else "\033[33m○\033[0m"
                        vit = f"{e['vitality']*100:.0f}%"
                        print(f"  {pin_marker} \033[1;37m{e['label']:<35}\033[0m "
                              f"\033[90m{vit:>5}  {e.get('text','')[:50]}\033[0m")
                    print()
                continue

            if user_input.lower() == "dream":
                result = vault.dream()
                print(f"  \033[90m[DREAM] purged {result.get('purged',0)} nodes  "
                      f"remaining {result.get('remaining',0)}\033[0m\n")
                continue

            if user_input.lower() == "save":
                vault.save()
                print("  \033[90m[SAVE] vault persisted\033[0m\n")
                continue

            if user_input.lower() == "status":
                s = vault.status()
                print(f"  \033[90matoms={s.get('atoms',0)}  chunks={s.get('chunks',0)}  "
                      f"total={s.get('total',0)}  registry={s.get('registry_entries',0)}  "
                      f"hit_rate={s.get('cache_hit_rate',0):.1f}%\033[0m\n")
                continue

            # ── Agent turn ────────────────────────────────────────────
            print(f"\n\033[90m  thinking...\033[0m", end="", flush=True)
            t0 = time.time()

            reply = agent_turn(
                user_input,
                conversation,
                model,
                tokenizer,
                tools,
                demo_mode=args.demo,
            )

            elapsed = time.time() - t0
            print(f"\r\033[90m  ({elapsed:.1f}s)\033[0m\n")

            # Pretty-print the reply
            wrapped = textwrap.fill(reply, width=72, initial_indent="  ", subsequent_indent="  ")
            print(f"\033[1;37m{wrapped}\033[0m\n")

    finally:
        # Clean shutdown
        dream_thread.stop()
        print("\n\033[90m  Saving vault state...\033[0m", end="")
        try:
            vault.save()
            print(" done.\033[0m")
        except Exception:
            print(" failed (not connected).\033[0m")
        print("\033[1;35m  Goodbye.\033[0m\n")


if __name__ == "__main__":
    main()
