# Engineering notes

Running log of decisions and of places where reality differed from the docs.

## 2026-10-30 (Day 1)

### ERPNext deployment
- Pulling `frappe/erpnext:v16` filled the host disk (the image needs ~10 GB with the
  containerd snapshotter). Instead we run on an **existing Frappe/ERPNext v15.122 bench**
  already on this host, as a separate site `floorvoice.local` with its own database.
  Nothing in the other site is touched.
- `infra/erp-gateway.compose.yml` adds one nginx container (same bench image) pinned to
  `floorvoice.local`, on `127.0.0.1:8090`. Bench image/network/volume are env-configurable,
  so it can point at a fresh frappe_docker install instead.
- Target is ERPNext **v15** (not v16).

### ERPNext REST quirks
- `Work Order` inserted over REST does not copy operations from the BOM (the desk UI does
  it client-side), so no Job Cards are created on submit. The seed passes `operations`
  explicitly.
- Setup wizard runs synchronously via
  `frappe.desk.page.setup_wizard.setup_wizard.setup_complete` with a session login.
- Session-cookie POSTs work without a CSRF token; the app itself uses token auth with a
  dedicated `floorvoice-bot@example.com` user.

### Meta Model API (from docs, 2026-10-30)
- Base `https://api.meta.ai/v1`, `Authorization: Bearer <key>`, OpenAI-SDK compatible
  (`/v1/chat/completions` and `/v1/responses`).
- Agent model pinned: `muse-spark-1.3`. ASR: `muse-voice-transcribe-1.0`.
- `tool_choice` only supports `"auto"`; `"required"`/named choice -> HTTP 400. The agent
  loop must therefore handle a plain-text reply (treat as an implicit `finish`/re-prompt).
- `strict` defaults to false; strict schemas need `additionalProperties: false` and every
  property in `required`. Arguments are still not guaranteed valid: validate in code.
- Realtime ASR: handshake ack is a frame with only `sessionId` (no `type`). Language bias
  uses English names (`"Tamil"`, `"English"`). Max count/length of `keywords` is not
  documented; we send ~170 entries and will note any rejection here.
- Docs: Muse Spark 1.3 audio understanding is degraded; irrelevant (we use Voice Transcribe).

### Test audio
- `samples/*.wav` are synthetic (espeak-ng, 24 kHz mono s16le) for pipeline smoke tests
  only; real phone recordings are needed to judge Tamil accuracy.

### First Voice Transcribe run (synthetic samples, keywords = 169 from masters)
- File and realtime endpoints both behave as documented; handshake + 169 keywords accepted.
- Latency: realtime final arrives ~1.3–1.6 s after the end of audio (incl. connect);
  file endpoint 1.8–3.4 s for 2–5 s clips.
- English is accurate, but spoken numbers come back as homophones:
  "two bearing" -> "to bearing", "job card four" -> "job card for". Number normalisation
  must treat "to"/"for" before an item or after "job card" as candidate quantities, and the
  agent must confirm against masters rather than trust them.
- Item codes are recognised well ("6205").
- Tamil from espeak-ng is unintelligible to the ASR ("400 bearing R400 mind stores line"),
  which says more about espeak than about the model. Real recordings needed.

## 2026-10-31 (Day 2) — agent, text mode

### Latency design
- One Muse Spark round trip measured 1.1–4.6 s (`reasoning_effort` low was fastest and is the
  default, `AGENT_REASONING`). A search -> write -> finish loop would be 3 calls and miss the
  8 s budget, so:
  1. **Pre-retrieval**: before the first call the server matches the utterance against the
     masters (`agent/resolve.candidates`) and sends likely items/warehouses/job cards as
     context. Ties are kept, so collisions ("bearings") stay visible to the agent. The
     search tools remain for anything not in the candidates.
  2. **Server readback**: after a successful write the server builds the one-line
     confirmation (EN/TA templates, `agent/readback.py`) from the posted document instead of
     waiting for a `finish` call. `finish` is still used for refusals and failures.
- Result: typical transaction = 1 Spark call, 1.5–2.7 s end to end in text mode.

### Muse Spark behaviour observed
- Strict tool schemas with nullable types (`"type": ["string","null"]`) are accepted.
- It issues parallel tool calls (searches) in one turn when allowed.
- It usually returns a short `content` preamble alongside tool calls; we ignore it.
- After a failed write it sometimes answers in plain text instead of calling `finish`; the
  loop treats plain text as finish (or as a question if it ends with "?").

### ERPNext behaviour
- Inserting with `docstatus=1` in one request leaves Material Request `status = "Draft"`
  (status hooks skipped). The client now inserts a draft, then submits with a PUT.
  MAT-MR-2026-00001/2 were created before the fix and show "Draft"; demo reset clears them.
- Job Card completion: v15 refuses to submit unless total completed == for_quantity, so a
  partial quantity appends a time log and keeps the card open ("20 of 50 done"); the
  quantity that reaches the planned total submits it (status Completed).
- Workstation `production_capacity` raised to 50 so time logs of concurrent cards on one
  workstation don't fail the overlap check.

### Normalisation
- Spoken digit runs are part numbers: "six two zero five" -> 6205 (not 13); Tamil
  "ஆறு இரண்டு பூஜ்யம் ஐந்து" -> 6205.
- Python `\w` excludes Tamil vowel signs/virama; tokeniser includes the Tamil block
  explicitly. Tamil case suffixes glued to nouns ("ஸ்டோர்ஸ்லிருந்து") match by prefix.
- "to"/"for" homophones: resolved deterministically only right after "card"; elsewhere the
  prompt tells Spark to read them as 2/4 only in a quantity slot.

## 2026-10-30/11-01 (Day 3) — voice path, PWA, tunnel

### Architecture of the voice path
- One WebSocket per button press (`WS /audio`). The browser AudioWorklet resamples the mic
  (usually 48 kHz) to 24 kHz s16le and streams 80 ms frames; the server opens the Voice
  Transcribe session in parallel and buffers frames until the handshake ack. On release
  the server sends `endStream`, gets the final transcript and runs the agent in the same
  connection, so the phone needs no second round trip.
- Measured: final transcript 0.35–1.1 s after release; full release-to-confirmation
  2.0–4.9 s over the Cloudflare quick tunnel.
- The quick tunnel (`trycloudflare.com`) needs no account; the URL changes only when the
  cloudflared container is recreated. The API requires `FLOORVOICE_KEY` (header or `?k=`)
  because the tunnel is public.

### Muse Spark: deviations from the docs
- **Tool call leaked into `content`.** Once, with no `tool_calls`, the response content was
  `<atem:function_calls><atem:invoke name="default.finish"><atem:parameter name="summary">…`
  (raw markup). This happened right after the prompt asked it not to write text alongside
  tool calls (instruction since removed). `SparkCompleter` now logs the raw content and
  parses such markup back into tool calls, coercing values with our tool schemas.
- **Tail latency.** Same request, `reasoning_effort=low`: median ~1.7 s but occasional
  6–8 s calls (and up to 1,200 completion tokens on confusing input). `minimal` is not
  faster in practice (spikes to 6–7 s). Mitigations:
  - hedged requests: a second identical request is raced if the first hasn't returned in
    2.5 s (10-run ask benchmark: max 4.4 s instead of 6+ s);
  - a 6 s per-turn Spark budget; over budget the worker is asked to repeat (never cuts an
    ERP write; anything already posted is read back).

### Verified without a phone
- Headless Chromium (Playwright image) with a fake microphone playing `samples/*.wav`:
  hold -> partial/final transcript -> Stock Entry posted -> card shown (4.5 s from release);
  typed request -> question with option chips -> tap option -> Material Request posted.
