# FloorVoice

Voice-first front end to ERPNext for shop-floor workers. Hold a button on a phone, speak in
Tamil, English or a mix of both, and a Muse Spark agent posts the ERPNext transaction and
reads back a one-line confirmation, typically 2–5 s after the button is released.

Built for the Meta Global AI Developer Hackathon (Oct 30 – Nov 4, 2026).

| Worker says | FloorVoice does in ERPNext |
|---|---|
| "இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து" | Submits a **Stock Entry** (Material Transfer): 2 × Ball Bearing 6205, Stores → Line 2. Confirms in Tamil. |
| "Job card five completed, ten pieces" | Logs 10 pieces on **Job Card** PO-JOB00005 and submits it once the planned quantity is reached. |
| "Send me bearings" → *"Which bearing, and how many?"* → "6205, ten pieces" | Asks one question (with tappable options), then submits a **Material Request** for 10 × 6205 to the worker's line. |

Also supported: material issue ("issue two kg grease from line two"), purchase requests
("order 20 hex nut M10"), and partial job-card progress ("20 of 50 done").

**Show me the part (photo disambiguation).** When FloorVoice asks which item was meant
(its two transcripts disagree on 6205 / 6305, or "bearings" matches four items), the
question card also shows a **Show me the part** camera button. The worker photographs the
part or its box label; the phone downscales it to 1280 px and sends it to
`POST /clarify-photo`. Muse Spark's image understanding reads the label and, through a
strict JSON schema, may only answer with one of the candidate item codes or null. The
server re-checks the code against the candidates; with high or medium confidence it resumes
the pending turn exactly as if that chip had been tapped, otherwise it says "Couldn't read
the label — tap one" and keeps the chips. The photo is stored next to the press's WAV, the
history row records `resolved_by: "photo"` (or `chip`, `voice`, `auto`), and the label text
goes into the document's Remarks. The photo step has its own 6 s budget and never posts on
its own. The button only appears when every option of the question is a distinct item.

## How it works

```
 Phone (PWA)                     FloorVoice server (FastAPI)                 Meta Model API
 ───────────                     ───────────────────────────                 ──────────────
 hold button ──PCM 24 kHz──▶ WS /audio ──relay──────────────────────────▶ Muse Voice Transcribe
 live text   ◀──partials────            ◀──partials / final transcript──    (realtime, keyword-
                                 │                                           biased from ERP masters)
                                 ▼
                         normalise numbers (Tamil/English words → digits)
                         pre-match items / warehouses / job cards
                                 │
                                 ▼
                         agent loop ──tools (strict JSON schema)──────────▶ Muse Spark 1.3
                                 │  search_* · create_stock_entry ·
                                 │  complete_job_card · create_material_request ·
                                 │  ask_user · finish
                                 ▼
                         validate against masters & stock ──REST──▶ ERPNext v15
                                 │                                  (insert draft → submit)
 card + speech ◀──result────────┘  readback built from the posted document (EN / TA / SI)
      │
      └─ question over an item? ── "Show me the part" ── photo ──▶ POST /clarify-photo
                                   Muse Spark image understanding, answer limited to the
                                   candidate codes ── high/medium ──▶ resume as a chip tap
                                                   ── low/null/6 s ──▶ back to the chips
```

Design points (details and measurements in [NOTES.md](NOTES.md)):

- **One round trip per press.** The phone streams audio over a single WebSocket; the server
  gets the final transcript and runs the agent on the same connection.
- **ASR biased by ERP data.** Item codes, names and Tamil/English spoken aliases (a custom
  field on Item and Warehouse) are sent to Voice Transcribe as keywords.
- **One Spark call for most requests.** Before calling the model the server matches the
  utterance against the masters and sends the likely candidates (keeping ties, so
  "bearings" stays ambiguous). After a successful write the server, not the model, writes
  the confirmation.
- **The model never writes unchecked.** Every write tool re-checks item, warehouse, job
  card, quantity and stock on the server before calling ERPNext; errors go back to the
  model as tool results. Each document's remarks record the exact transcript, and the audio
  is kept in `data/audio/`.
- **Misheard part numbers.** The audio is also transcribed a second time (file endpoint).
  Before posting an item whose number is one digit away from another item's (6205/6305),
  the two transcripts are compared; if they disagree the worker is asked "6205 or 6305?"
  instead. Every confirmation card has an **↺ Undo** button that cancels the document
  (or removes the job-card time log) in ERPNext.
- **Tail latency.** Spark requests are hedged (a duplicate is raced after 2.5 s) and each
  turn has a 6 s budget; over budget the worker is asked to repeat, quoting what was heard.

## Running it

Requirements: Python 3.11 with [uv](https://docs.astral.sh/uv/), Docker, a Meta Model API
key, and a Frappe/ERPNext **v15** bench (frappe_docker) to host a demo site.

```bash
cp .env.example .env              # set META_API_KEY, ERP_ADMIN_PASSWORD, FLOORVOICE_KEY
uv sync

# 1. ERPNext: create a site on your bench, then put an nginx in front of it on :8090
docker exec <bench-backend> bench new-site floorvoice.local --install-app erpnext \
    --mariadb-user-host-login-scope=% --admin-password <admin-pw> --db-root-password <root-pw>
docker compose -f infra/erp-gateway.compose.yml --env-file .env up -d
#    (ERP_BENCH_IMAGE / ERP_BENCH_NETWORK / ERP_SITES_VOLUME select your bench)

# 2. Demo data: company, 3 warehouses, 30 items with voice aliases, opening stock,
#    2 suppliers, BOMs, 5 work orders → job cards PO-JOB00001..5, bot user + API keys
uv run python -m seed.seed

# 3. Server + public HTTPS tunnel for the phone (prints the URL with ?k=<access key>)
scripts/dev.sh
```

Open the printed URL on the phone once (the key is remembered), allow the microphone,
choose **My line**, and hold the button while speaking.

Without a phone: `uv run python -m scripts.say "move 2 bearing 6205 from stores to line 2"`
runs the agent from text, and `uv run python -m scripts.ws_client samples/en_transfer.wav`
streams a WAV exactly like the PWA does.

### Watching the changes in ERPNext

`scripts/erp_tunnel.sh` opens a second quick tunnel to the ERPNext desk and prints its URL
(log in as Administrator). While it runs, every confirmation card in the app gets an
**Open … in ERPNext ↗** link to the posted document. Useful pages:

- `/app/stock-entry` and `/app/query-report/Stock Balance`: transfers and balances per warehouse
- `/app/job-card`: completed quantity and time logs per card
- `/app/material-request`: requests with status Pending
- In any document: the **Remarks** field shows the words the worker spoke

`scripts/erp_tunnel.sh stop` closes it. The tunnel is public; it is protected only by the
ERPNext login, so keep it up only while you need it.

## Demo runbook

1. `uv run python -m scripts.demo_reset`: cancels all test
   stock entries and material requests (stock returns to the opening figures), deletes
   work orders and job cards, resets their numbering and reseeds, so the job cards are
   PO-JOB00001–5 again. Restart the server afterwards.
2. `scripts/dev.sh` (and `scripts/erp_tunnel.sh` if showing ERPNext side by side).
3. On the phone: reload, set **My line = Line 1**, check the volume is up.
4. Speak the three lines in the table above, in that order. Hold the button until the end
   of the sentence; say the item number at a normal pace.
5. The job-card line works once per reset (the card is then completed).

## Tests

```bash
uv run pytest                          # 127 unit tests: normaliser, resolver, ERP client and
                                       # tools against mocked HTTP, agent loop with a scripted LLM,
                                       # photo parsing and the photo/chip resume path
FLOORVOICE_LIVE=1 uv run pytest tests/test_live.py   # the 3 demo flows + the label-photo flow
                                                     # against live Spark + ERPNext (posts documents)
uv run ruff check . && uv run ruff format --check .
```

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI app (`/audio`, `/act`, `/clarify`, `/clarify-photo`, `/undo`, `/history`), Voice Transcribe client |
| `agent/` | number normalisation, master matching, tool schemas and executor, agent loop, prompt, readback, label-photo matching (`vision.py`) |
| `erp/` | async ERPNext REST client, master loaders, ASR keyword builder |
| `pwa/` | the phone app: plain HTML/CSS/JS, AudioWorklet capture, browser speech synthesis |
| `seed/` | demo company data and the idempotent seeding script |
| `scripts/` | dev runner, tunnels, demo reset, text/WAV clients, ASR comparisons |
| `infra/` | nginx gateway for the ERPNext site |

## Limitations

- **Tamil accuracy is not measured yet.** Only a handful of real Tamil-English phone
  recordings were tested. Spoken item numbers are the weak point ("6205" heard as "2625Z"
  or as the real item "6305"). FloorVoice asks when the number matches nothing or when its
  two transcripts disagree, but if both mishear the same way it posts the wrong item:
  the worker must listen to the readback and use Undo.
- Clarifying questions follow the dominant language of the utterance: mostly-English
  code-switching gets an English question. Confirmations after a write are always in the
  utterance's script.
- Spoken readback uses the phone's built-in speech voices; without a Tamil or Sinhala voice
  (iPhones have no Sinhala voice) the confirmation is only shown on screen, not spoken.
- One shared access key, no per-worker login; history is kept in a local file.
- Single transaction, single item per utterance; three transaction types only.
- ERPNext v15 only (tested on 15.122). Partial job-card quantities are logged as time logs
  and the card is submitted only when the planned quantity is reached.
- Quick tunnels (`trycloudflare.com`) are for demos: the URL changes if the tunnel restarts.

## What was built during the hackathon

All code in this repository was written between Oct 30 and Nov 4, 2026, during the event.
ERPNext / Frappe is a pre-existing open-source dependency (GPLv3) and is not part of this
work; FloorVoice talks to it only over its REST API. Models used: Meta Muse Spark 1.3
(agent) and Muse Voice Transcribe 1.0 (speech recognition).
