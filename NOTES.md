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

## 2026-10-30 (Day 4) — Tamil reliability, failure handling, demo reset

### Voice Transcribe settings on real Tamil-English phone audio
`scripts/asr_compare.py`, file endpoint, 3 runs per setting, two phone clips of
"இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து":
- Good clip: default (languageBias Tamil+English, 169 keywords) gave 6205 3/3.
  Without keywords: "six to zero five" 3/3. Tamil-only / no bias / English-first
  sometimes gave **6305**, a real item. Keep the defaults.
- Failed clip: truncated after the number in all 18 runs. Splitting it showed the audio
  after 3 s contains only "two six two five Z": the rest of the sentence was never
  recorded (released early or a pause). Digits spoken one by one were misheard.
- Small sample (2 real Tamil clips). Not an accuracy figure.

### Handling a misheard number
- `search_items`: if a spoken number matches no item ("bearing 2625Z"), return the item
  family without the number, flagged `note: spoken number matched no item`.
- Prompt: in that case, or when a transfer lacks a warehouse, ask at once (no extra
  search). Replay of "இரண்டு bearing 2625Z": one Spark call, question with the four
  bearings as options in 2.3–5.2 s (was: 6 s timeout and a generic apology).
- Timeouts/errors now quote what was heard.
- Reply language: Spark follows the dominant language of the utterance. Fully Tamil input
  gets a Tamil question; mostly-English code-switching gets English even with an explicit
  "Reply language: Tamil" line. Readbacks after a write are server templates, so they are
  always Tamil for Tamil-script input.

### Demo reset (`scripts/demo_reset.py`)
- ERPNext v15 refuses to delete a cancelled Stock Entry ("linked with Stock Ledger
  Entry"), so stock entries and material requests are cancelled only (stock returns to
  the opening figures; their numbering continues). Job cards and work orders are deleted
  and their `tabSeries` counters reset, so the seed recreates PO-JOB00001..5 and "job card
  five" in the script stays valid. Verified: all balances equal opening stock afterwards.

### PWA
- Outcome is shown on the button (green "Posted ✓" / red "Not posted", vibration) and as a
  heading on the card; presses with no audio say so ("No sound from the microphone").
- "My line" lists only floor warehouses (those with voice aliases).

## 2026-10-31 — misheard part numbers: second transcript + Undo

- Phone test: "இரண்டு bearing 6205 …" was transcribed as **6305** (a real item) and posted.
  Re-sending that recording: 6205 once, 6305 twice. The audio is genuinely borderline.
- **Second transcript.** On release the server also sends the audio to the file endpoint.
  Just before a stock entry / material request whose item number has a one-digit
  neighbour in the item list (6205 ↔ 6201, 6204, 6305), the agent compares: if the
  second transcript lacks the number and contains a neighbour, nothing is written and the
  server asks "6205 or 6305?" (as a synthetic `ask_user`, so the answer resumes the same
  conversation). If they agree, or the second transcript is late (> 3 s) or unreadable,
  it posts. Skipped after a clarification (the worker chose explicitly).
  Replaying the problem recording 4×: asked twice, posted 6305 twice (both transcripts
  agreed). It catches disagreement, not a shared mistake.
- **Undo.** `POST /undo` reverses only documents in FloorVoice's own history, once.
  Stock Entry / Material Request: cancel. Job Card with a partial log: remove our time
  log. Completed Job Card: cancel, then insert an amended draft (`amended_from`), as
  ERPNext's Amend does; the reopened card is PO-JOB00002-1 and "job card two" still
  finds it. All four paths verified against ERPNext 15.122.

## 2026-10-31 — Sinhala

- Sinhala number words incl. glued endings (දෙකක්, දෙකට, විස්සක්), one-word compounds
  (විසිපහ = 25) and digit-by-digit part numbers; bare "එක" is left alone (also "the").
- Script detection `si`; Sinhala readbacks, questions, errors and undo text; Spark writes
  its questions in Sinhala when told (verified in text mode).
- Sinhala spoken aliases for all items and warehouses (222 ASR keywords). Aliases built on
  "එක" ("ලයින් එකෙන්") clashed with "ස්ටෝර්ස් එකෙන්" (from the stores) and were dropped.
- Warehouse candidates now need the name's words consecutively in the utterance, so a
  quantity "2" plus "line" no longer suggests Line 2.
- The PWA has a speech-language setting (தமிழ் / සිංහල / English) that selects
  languageBias. Voice Transcribe accepts any language name silently (even "Klingon"), so
  Sinhala support is unverified until tested on real Sinhala audio.
