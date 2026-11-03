# FloorVoice

Voice-first front end to ERPNext for shop-floor workers. Hold a button on a phone, say what
happened in Tamil or English (or a mix of both), with Sinhala experimental, and a Muse Spark
agent posts the ERPNext transaction and reads back a one-line confirmation: 2–5 s typical
after the button is released, 6 s budget. Stock can also be counted by filming the shelf
(SAM 3.1), with the number confirmed by the worker before ERPNext is updated.

Built for the Meta Global AI Developer Hackathon (Oct 30 – Nov 4, 2026).

| Worker says | FloorVoice does in ERPNext |
|---|---|
| "இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து" | Submits a **Stock Entry** (Material Transfer): 2 × Ball Bearing 6205, Stores → Line 2. Confirms in Tamil. |
| "Job card five completed, ten pieces" | Logs 10 pieces on **Job Card** PO-JOB00005 and submits it once the planned quantity is reached. |
| "Send me bearings" → *"Which bearing, and how many?"* → "6205, ten pieces" | Asks one question (with tappable options), then submits a **Material Request** for 10 × 6205 to the worker's line. |
| "Count bearing 6305 in stores" → pans the phone across the shelf → taps **Post 12** | SAM 3.1 tracks each box across the clip: "I count 12. ERPNext has 15 in Stores." After the tap, submits a **Stock Reconciliation** setting 6305 in Stores to 12. |

**Experimental: Sinhala.** Sinhala is not in Voice Transcribe's supported language list;
these work on simple requests via keyword biasing and number normalisation, and are not
reliable for part numbers.

| Worker says | FloorVoice does in ERPNext |
|---|---|
| "මට බෝල්ට් දහයක් එවන්න" | Submits a **Material Request** for 10 × Hex Bolt M10 to the worker's line. Confirms in Sinhala. |
| "mata bearing 10k ewanna" (typed Sinhala in English letters) | Read as Sinhala ("10k" = 10); asks which bearing, in Sinhala. |

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

**Worker PIN on every transaction.** The app opens on a 4-digit PIN pad (again after
**Switch worker** in settings). The PIN goes with every press (`pin` in the WebSocket start
message, `X-FloorVoice-Pin` on `/act`, `/clarify`, `/clarify-photo`, `/undo`, `/history`,
`/me`), in addition to the shared access key, and the server maps it to an ERPNext
Employee; an unknown PIN gets "PIN not recognised". The worker's home line is the default
destination and requesting warehouse, and their preferred language sets the speech bias and
readback (a manual language override stays in settings; the old phone-level "My line" and
language settings are gone). Every document is stamped: `floorvoice_worker` on Stock Entry
and Material Request, `employee` on the job-card time log, and the worker's name at the
start of Remarks. History rows carry `worker_id` / `worker_name`; `/history` shows the
current worker's rows (`?all=1` for everyone's). Only the worker who posted a document can
undo it, once.

**Stock count by video (SAM 3.1).** "Count X in Y" (or "how many X in Y") goes through the
same resolver and clarification path as the other transactions; the agent's `count_stock`
tool only reads ERPNext's quantity and posts nothing. The phone then shows a camera card:
the worker pans slowly across the shelf, left to right, once (5–15 s). The server turns
the clip into a 5 fps H.264 MP4 (at most 15 s and 75 frames) and sends it to SAM 3.1 with
the item's `floorvoice_count_prompt`, a short noun phrase for what the camera sees ("box").

*How counting works.* SAM 3.1 returns, per frame, every matching object with an id that
stays the same while the object is in view. The count is the number of distinct ids that
persist for at least 3 frames with a median box area of at least 0.2% of the frame (ids
boxing the same object in all their shared frames are merged); per-frame counts are never
summed, since a pan shows each box in many frames. The review is mandatory: the frame with
the most objects in view is shown with the counted boxes numbered, next to "I count 12.
ERPNext has 15 in Stores." and **Post 12 / Adjust / Retake / Cancel**. Only the Post tap
writes the Stock Reconciliation (valuation rate from Bin); no tool the model can call does.
Adjust takes a spoken or typed number. Zero objects, more than 500, an error or more than
20 s ends in "Retake or enter the number", never a post; an item without a count prompt is
counted by entering the number. Remarks record the worker, the transcript, "counted by
video: 12 (system 15)" and the counting parameters; the clip and review frame stay in
`data/audio/`; Undo cancels the reconciliation (same worker, once). Measured: a 12-box pan
counted 12 in 9 of 9 runs; on real iPhone clips, 6 of 6 mugs, 3 of 3 sweets, and 3 for 2
sweets plus an empty wrapper. About 7–10 s of SAM time and 50–60 billed frames (~$0.01) per count.
Enabled with `FEATURE_VIDEO_COUNT=true`.

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
                                 │  count_stock · ask_user · finish
                                 ▼
                         validate against masters & stock ──REST──▶ ERPNext v15
                                 │                                  (insert draft → submit)
 card + speech ◀──result────────┘  readback built from the posted document (EN / TA / SI)
      │
      └─ question over an item? ── "Show me the part" ── photo ──▶ POST /clarify-photo
                                   Muse Spark image understanding, answer limited to the
                                   candidate codes ── high/medium ──▶ resume as a chip tap
                                                   ── low/null/6 s ──▶ back to the chips
      │
      └─ count_stock? ── pan the shelf ── clip ──▶ POST /count-video ── ffmpeg 5 fps ──▶ SAM 3.1
                         review: "I count N. ERPNext has M" ◀── distinct tracked identities
                         Post N (tap) ──▶ POST /count-action ──▶ Stock Reconciliation

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

## Agentic behaviour

- Resolves item, warehouse and job card against ERP masters before deciding which transaction to post.
- Chooses Material Transfer vs Material Issue from the utterance, and checks current stock before either.
- Re-validates every field and checks stock before writing; refuses to post when the two transcripts disagree on a part number.
- Asks exactly one question when a mandatory field is ambiguous, and accepts a spoken answer, a tap, or a photo of the label resolved by Muse Spark vision.
- Builds the confirmation from the posted ERPNext document, never from model text.
- Offers a one-time, same-worker Undo with ERPNext-correct cancel and amend semantics.
- Counts stock from a shelf video by tracking distinct objects, and posts a Stock Reconciliation only after the worker confirms the number.

Models: Muse Spark 1.3 (tool calling, structured output, image understanding), Muse Voice Transcribe 1.0 (realtime and file endpoints, keyword and language biasing), SAM 3.1 (video segmentation with object tracking, for stock counts).

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
#    2 suppliers, BOMs, 5 work orders → job cards PO-JOB00001..5, 6 workers (Employee),
#    bot user + API keys; generates FLOORVOICE_PIN_SALT in .env if it is empty
uv run python -m seed.seed

# 3. Server + public HTTPS tunnel for the phone (prints the URL with ?k=<access key>)
scripts/dev.sh
```

Open the printed URL on the phone (the `?k=` access key stays in the address bar; if it is
ever missing, the PIN screen asks for it), enter a worker PIN, allow the microphone (and the
camera, for photos and counts), and hold the button while speaking.

The seed adds these ERPNext custom fields:

| Doctype | Field | Purpose |
|---|---|---|
| Item, Warehouse, Employee | `voice_aliases` | spoken names, used for ASR keywords and matching |
| Employee | `floorvoice_pin` | HMAC-SHA256 of the PIN with `FLOORVOICE_PIN_SALT` (never the PIN) |
| Employee | `floorvoice_home_warehouse` | home line: default destination / requesting warehouse |
| Employee | `floorvoice_language` | `ta`, `si` or `en`: speech bias and readback |
| Stock Entry, Material Request | `floorvoice_worker` | Employee who spoke |
| Material Request | `floorvoice_remarks` | transcript (and label text), as Material Request has no Remarks |
| Item | `floorvoice_count_prompt` | what SAM 3.1 looks for when the item is counted by video ("box") |
| Stock Reconciliation | `floorvoice_worker`, `floorvoice_remarks` | who counted, transcript, video vs system count and counting parameters |

For the count demo the seed sets ERPNext's Stores quantity of Ball Bearing 6305 to 15 and
Jaw Coupling L100 to 10, a few above the demo shelf (12 and 8 boxes). If the demo shelf
holds something else, set `DEMO_COUNT_PROMPTS` in `.env` (e.g. `BRG-6305=mug`, for mugs
standing in for bearing boxes); the seed and `demo_reset` keep it.

Demo workers (PIN → name, home line, language): 1111 Kumar Selvaraj, Line 1, Tamil ·
2222 Priya Nadarajah, Line 2, Tamil · 3333 Nimal Perera, Line 1, Sinhala · 4444 Dilani
Fernando, Line 2, Sinhala · 5555 Arjun Mahendran, Stores, English · 6666 Sarah Thomas,
Line 1, English. The bot user gets the HR User role so it can read Employee.

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

1. `uv run python -m scripts.demo_reset`: cancels all test stock reconciliations,
   stock entries and material requests (stock returns to the seeded figures), deletes
   work orders and job cards, resets their numbering and reseeds, so the job cards are
   PO-JOB00001–5 again and the six workers have their PINs, lines and languages back.
   History (every worker's) is archived. Restart the server afterwards.
2. `scripts/dev.sh` (and `scripts/erp_tunnel.sh` if showing ERPNext side by side).
3. On the phone: reload, enter PIN 1111 (Kumar, Line 1, Tamil), check the volume is up.
4. Speak the three lines in the table above, in that order. Hold the button until the end
   of the sentence; say the item number at a normal pace.
5. The job-card line works once per reset (the card is then completed).
6. Stock count: put 12 boxes (or the `DEMO_COUNT_PROMPTS` stand-in, e.g. 6 mugs) in a single
   row, say "count bearing 6305 in stores", pan once left to right, check the numbered
   objects and tap **Post** (ERPNext had 15).

## Tests

```bash
uv run pytest                          # 204 unit tests: normaliser, resolver, ERP client and
                                       # tools against mocked HTTP, agent loop with a scripted LLM,
                                       # photo parsing and the photo/chip resume path, worker PINs,
                                       # identity counting on recorded SAM 3.1 responses, the
                                       # count review gate and the Stock Reconciliation writer
FLOORVOICE_LIVE=1 uv run pytest tests/test_live.py   # the 3 demo flows, the label-photo flow and
                                                     # a 12-box video count, against live Spark,
                                                     # SAM 3.1 + ERPNext (posts documents)
uv run ruff check . && uv run ruff format --check .
```

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI app (`/audio`, `/me`, `/act`, `/clarify`, `/clarify-photo`, `/count-video`, `/count-action`, `/count-frame`, `/undo`, `/history`, `/client-log` for phone-side camera errors), Voice Transcribe client |
| `agent/` | number normalisation, master matching, tool schemas and executor, agent loop, prompt, readback, label-photo matching (`vision.py`), video counting with SAM 3.1 (`count.py`) |
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
- **Sinhala is experimental.** It is not in Voice Transcribe's supported language list;
  recognition varies run to run, and spoken part numbers are not reliable.
- Clarifying questions follow the dominant language of the utterance: mostly-English
  code-switching gets an English question. Confirmations after a write are always in the
  utterance's script.
- Spoken readback uses the phone's built-in speech voices; without a Tamil or Sinhala voice
  (iPhones have no Sinhala voice) the confirmation is only shown on screen, not spoken.
- Worker identity: 4-digit PIN, no per-worker login. The PIN rides alongside one shared
  access key; anyone who knows a PIN can act as that worker, and `?all=1` history is open to
  any worker. History is kept in a local file.
- Single transaction, single item per utterance; four transaction types only.
- **Video counts single-layer, visible items.** Stacked or occluded stock undercounts;
  anything that looks like the item counts, including empty packaging (an empty sweet
  wrapper was counted as a sweet); panning back over the shelf counts items again; the
  count prompt has to describe what the camera sees ("bearing box" found nothing where
  "box" found every box). Always reviewed before posting. Tested on one synthetic shelf
  clip and three real phone clips only (exact on two, +1 on the one with the empty wrapper).
- ERPNext v15 only (tested on 15.122). Partial job-card quantities are logged as time logs
  and the card is submitted only when the planned quantity is reached.
- Quick tunnels (`trycloudflare.com`) are for demos: the URL changes if the tunnel restarts.

## Built during the hackathon

All FloorVoice code (server, agent, erp client, pwa, seed, scripts, tests) was written
between Oct 30 and Nov 3, 2026. ERPNext/Frappe is a pre-existing open-source dependency
used only via its REST API. No code from any other project was reused.
