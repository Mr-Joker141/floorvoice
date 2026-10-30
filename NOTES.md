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
