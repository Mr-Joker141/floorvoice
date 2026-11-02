// FloorVoice PWA: PIN -> hold-to-talk -> WS /audio -> transcript + agent result -> readback.

const TARGET_RATE = 24000;
const MIN_HOLD_MS = 300;

const $ = (id) => document.getElementById(id);
const el = {
  ptt: $("ptt"), pttLabel: $("ptt-label"), hint: $("hint"), transcript: $("transcript"),
  card: $("card"), cardStatus: $("card-status"), cardLinks: $("card-links"), cardText: $("card-text"), cardMeta: $("card-meta"), options: $("options"),
  photoRow: $("photo-row"), photoBtn: $("photo-btn"), photoLabel: $("photo-label"),
  photoThumb: $("photo-thumb"), photoInput: $("photo-input"),
  history: $("history"), typed: $("typed"), typedInput: $("typed-input"),
  workerBtn: $("worker-btn"), settings: $("settings"), settingsName: $("settings-name"),
  settingsLine: $("settings-line"), langOverride: $("lang-override"), switchWorker: $("switch-worker"),
  pinScreen: $("pin-screen"), pinDots: $("pin-dots"), pinMsg: $("pin-msg"), pinPad: $("pin-pad"),
  countRow: $("count-row"), countVideo: $("count-video"), countTimer: $("count-timer"),
  countBtn: $("count-btn"), countInput: $("count-input"), countNative: $("count-native"),
  reviewRow: $("review-row"),
  reviewImg: $("review-img"), reviewCanvas: $("review-canvas"), reviewNote: $("review-note"),
};

// ---- settings: access key (?k=...), worker PIN and language override, per device -------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private mode: settings just don't persist */ }
  return value;
}

function forget(key) {
  try { localStorage.removeItem(key); } catch { /* ignore */ }
}

const params = new URLSearchParams(location.search);
if (params.get("k")) {
  store("fv.key", params.get("k"));
  history.replaceState(null, "", location.pathname);
}
const accessKey = params.get("k") || store("fv.key") || "";

const PIN_UNKNOWN = "PIN not recognised";
let pin = store("fv.pin") || "";
let worker = null; // {id, name, home_warehouse, language} from /me

// The phone-level "My line" and speech language were replaced by the worker's home line and
// preferred language; the language select is now an optional override.
forget("fv.home");
forget("fv.speech");
el.langOverride.value = store("fv.lang") || "";
el.langOverride.addEventListener("change", () => store("fv.lang", el.langOverride.value));

function headers(json = true) {
  const h = { "X-FloorVoice-Key": accessKey, "X-FloorVoice-Pin": pin };
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function checked(res) {
  if (res.ok) return res.json();
  const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
  if (res.status === 403 && detail === PIN_UNKNOWN) askPin(PIN_UNKNOWN);
  throw new Error(detail);
}

async function api(path, body) {
  return checked(await fetch(path, {
    method: body ? "POST" : "GET",
    headers: headers(),
    body: body ? JSON.stringify(body) : undefined,
  }));
}

// ---- worker: PIN screen, settings panel ---------------------------------------------------

let entered = "";

function askPin(message = "") {
  worker = null;
  pin = "";
  forget("fv.pin");
  entered = "";
  drawPin();
  el.pinMsg.textContent = message;
  el.pinScreen.hidden = false;
  el.settings.hidden = true;
  el.workerBtn.textContent = "…";
}

function drawPin() {
  [...el.pinDots.children].forEach((d, i) => d.classList.toggle("on", i < entered.length));
}

async function submitPin() {
  pin = entered;
  try {
    worker = await checked(await fetch("/me", { headers: headers(false) }));
  } catch (e) {
    if (e.message !== PIN_UNKNOWN) askPin(e.message === "Missing or wrong access key"
      ? "This link needs an access key. Open the full link with ?k=…" : e.message);
    el.pinDots.classList.remove("bad");
    void el.pinDots.offsetWidth; // restart the shake
    el.pinDots.classList.add("bad");
    navigator.vibrate?.([80, 60, 80]);
    return;
  }
  store("fv.pin", pin);
  el.pinScreen.hidden = true;
  showWorker();
  sessionId = newId();
  awaitingAnswer = false;
  clearCard();
  refreshHistory();
}

function showWorker() {
  const line = (worker.home_warehouse || "").replace(/ - [^-]+$/, "");
  el.workerBtn.textContent = worker.name;
  el.settingsName.textContent = worker.name;
  el.settingsLine.textContent = line ? `Home line: ${line}` : "";
}

el.pinPad.addEventListener("click", (e) => {
  const k = e.target.closest("button")?.dataset.k;
  if (!k) return;
  if (k === "back") entered = entered.slice(0, -1);
  else if (k === "clear") entered = "";
  else if (entered.length < 4) entered += k;
  el.pinMsg.textContent = "";
  drawPin();
  if (entered.length === 4) submitPin();
});

el.workerBtn.addEventListener("click", () => {
  if (!worker) return;
  el.settings.hidden = !el.settings.hidden;
  el.workerBtn.setAttribute("aria-expanded", String(!el.settings.hidden));
});
el.switchWorker.addEventListener("click", () => askPin());

async function startup() {
  if (!pin) return askPin();
  try {
    worker = await checked(await fetch("/me", { headers: headers(false) }));
    showWorker();
    refreshHistory();
  } catch (e) {
    if (e.message !== PIN_UNKNOWN) showCard("error", e.message === "Missing or wrong access key"
      ? "This link needs an access key. Open the full link with ?k=…" : `Server: ${e.message}`);
  }
}

// ---- UI state ---------------------------------------------------------------------------

let sessionId = newId();
let awaitingAnswer = false; // last result was a question: next utterance answers it
let busy = false;

function newId() {
  return crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "")
    : Math.random().toString(16).slice(2) + Date.now().toString(16);
}

let flashTimer = 0;

function setButton(mode) {
  clearTimeout(flashTimer);
  el.ptt.classList.remove("recording", "busy", "flash-done", "flash-fail");
  if (mode === "recording" || mode === "busy") el.ptt.classList.add(mode);
  el.pttLabel.textContent = mode === "recording" ? "Release to send"
    : mode === "busy" ? "Posting…" : awaitingAnswer ? "Hold to answer" : "Hold to talk";
}

// Show the outcome on the button itself for a moment: it is always on screen.
function flashButton(ok, label) {
  setButton("idle");
  el.ptt.classList.add(ok ? "flash-done" : "flash-fail");
  el.pttLabel.textContent = label;
  navigator.vibrate?.(ok ? 60 : [80, 60, 80]);
  flashTimer = setTimeout(() => setButton("idle"), 2500);
}

const STATUS = {
  done: "✓ Posted to ERPNext",
  ask: "? Question — answer by holding the button",
  finish: "✗ Not posted",
  error: "✗ Not posted",
  repeat: "✗ Not posted — say it again",
  undone: "↺ Cancelled in ERPNext",
  count: "▶ Stock count — film the shelf",
  review: "? Check the count — nothing is posted until you tap",
};

function showTranscript(text, final) {
  el.hint.hidden = true;
  el.transcript.hidden = false;
  el.transcript.textContent = text || "…";
  el.transcript.classList.toggle("final", !!final);
}

function erpLinks(docs) {
  const frag = document.createDocumentFragment();
  for (const d of docs) {
    if (!d.url) continue;
    const a = document.createElement("a");
    a.href = d.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.className = "erp-link";
    a.textContent = `Open ${d.name} in ERPNext ↗`;
    frag.append(a);
  }
  return frag;
}

function undoButtons(docs) {
  const frag = document.createDocumentFragment();
  for (const d of docs) {
    if (d.undone) continue;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "undo";
    b.textContent = docs.length > 1 ? `↺ Undo ${d.name}` : "↺ Undo";
    b.addEventListener("click", () => undoDoc(d, b));
    frag.append(b);
  }
  return frag;
}

async function undoDoc(doc, button) {
  if (busy) return;
  unlockSpeech();
  busy = true;
  button.disabled = true;
  button.textContent = "Cancelling…";
  try {
    const r = await api("/undo", { doctype: doc.doctype, name: doc.name });
    busy = false;
    showCard("undone", r.text, { docs: r.url ? [{ name: r.name, url: r.url, undone: true }] : [] });
    speak(r.speech);
    flashButton(false, "Cancelled");
    refreshHistory();
  } catch (e) {
    busy = false;
    button.disabled = false;
    button.textContent = "↺ Undo";
    showCard("error", `Undo failed: ${e.message}`);
    speak("Undo failed.");
  }
}

const PHOTO_LABEL = { ta: "பாகத்தைக் காட்டு", si: "කොටස පෙන්වන්න", en: "Show me the part" };

function showCard(kind, text, { options = [], meta = "", docs = [], photo = false, lang = "",
                                actions = [], review = null } = {}) {
  el.card.hidden = false;
  el.card.className = `card ${kind}`;
  el.cardStatus.textContent = STATUS[kind] || "";
  el.cardText.textContent = text;
  el.cardMeta.textContent = meta;
  el.cardMeta.className = "card-meta";
  el.cardLinks.replaceChildren(erpLinks(docs), undoButtons(docs));
  el.options.innerHTML = "";
  options.forEach((opt, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = opt;
    // Count review chips are verbs for /count-action; other chips answer the question.
    b.addEventListener("click", () => (actions[i] ? countAction(actions[i]) : answerTyped(opt, "chip")));
    el.options.append(b);
  });
  if (kind !== "count") stopCamera();
  el.countRow.hidden = kind !== "count";
  el.countRow.classList.remove("filming");
  el.countBtn.disabled = false;
  el.countBtn.textContent = "● Start filming";
  el.countTimer.hidden = true;
  showReview(kind === "review" ? review : null);
  if (kind === "count") startPreview();
  // Only for a question over an item; the server decides (r.photo).
  el.photoRow.hidden = !(kind === "ask" && photo);
  el.photoBtn.disabled = false;
  el.photoThumb.hidden = true;
  el.photoThumb.classList.remove("reading");
  el.photoLabel.textContent = PHOTO_LABEL[lang] || PHOTO_LABEL.en;
}

function clearCard() {
  el.card.hidden = true;
}

function handleResult(r) {
  awaitingAnswer = r.kind === "ask";
  // A count keeps its session through filming, review and adjusting.
  if (!["ask", "count", "review"].includes(r.kind)) sessionId = newId();
  const timing = r.total_ms ? `${(r.total_ms / 1000).toFixed(1)} s from release`
    : `${(r.elapsed_ms / 1000).toFixed(1)} s`;
  showCard(r.kind, r.text, { options: r.options || [], meta: timing, docs: r.documents || [],
                            photo: r.photo, lang: r.language, actions: r.actions || [],
                            review: r.review });
  el.card.scrollIntoView({ behavior: "smooth", block: "center" });
  speak(r.speech || r.text);
  if (r.kind === "done") flashButton(true, "Posted ✓");
  else if (["ask", "count", "review"].includes(r.kind)) setButton("idle");
  else flashButton(false, "Not posted");
  refreshHistory();
}

// ---- speech readback (browser speechSynthesis; no third-party TTS) ------------------------

let speechUnlocked = false;

function unlockSpeech() {
  // iOS only lets speechSynthesis talk after it was used inside a user gesture.
  if (speechUnlocked || !("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(" ");
  u.volume = 0;
  speechSynthesis.speak(u);
  speechUnlocked = true;
}

function pickVoice(lang) {
  const voices = speechSynthesis.getVoices();
  return voices.find((v) => v.lang.replace("_", "-").toLowerCase().startsWith(lang))
    || null;
}

function speak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel();
  const lang = /[\u0D80-\u0DFF]/.test(text) ? "si" : /[\u0B80-\u0BFF]/.test(text) ? "ta" : "en";
  const u = new SpeechSynthesisUtterance(text);
  const voice = lang === "en" ? (pickVoice("en-in") || pickVoice("en")) : pickVoice(lang);
  // No voice for this script (Sinhala on iPhone): an English voice would mangle it, so stay
  // quiet and let the card on screen carry the confirmation.
  if (!voice && lang !== "en") return;
  if (voice) u.voice = voice;
  u.lang = voice ? voice.lang : { si: "si-LK", ta: "ta-IN", en: "en-IN" }[lang];
  u.rate = 1.0;
  speechSynthesis.speak(u);
}
if ("speechSynthesis" in window) speechSynthesis.getVoices(); // warm the voice list

// ---- microphone capture ------------------------------------------------------------------

let audioCtx = null;
let worklet = null;

async function ensureMic() {
  if (worklet) {
    if (audioCtx.state === "suspended") await audioCtx.resume();
    return;
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.audioWorklet.addModule("capture-worklet.js");
  const source = audioCtx.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(audioCtx, "pcm-capture", {
    processorOptions: { targetRate: TARGET_RATE },
  });
  source.connect(worklet);
  // Some browsers only pull audio through nodes that reach the destination.
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  worklet.connect(mute).connect(audioCtx.destination);
  if (audioCtx.state === "suspended") await audioCtx.resume();
}

// ---- one utterance = one WebSocket --------------------------------------------------------

let current = null;

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/audio?k=${encodeURIComponent(accessKey)}`;
}

async function pressStart() {
  if (busy || current || !worker) return;
  unlockSpeech();
  speechSynthesis?.cancel();
  try {
    await ensureMic();
  } catch (e) {
    showCard("error", `Microphone unavailable: ${e.message}`);
    return;
  }
  const utt = {
    started: performance.now(),
    ws: new WebSocket(wsUrl()),
    buffered: [],
    stopped: false,
    finished: false,
  };
  current = utt;
  utt.ws.binaryType = "arraybuffer";
  utt.ws.onopen = () => {
    utt.ws.send(JSON.stringify({
      type: "start",
      session_id: sessionId,
      mode: awaitingAnswer ? "clarify" : "act",
      sample_rate: TARGET_RATE,
      pin,
      speech: el.langOverride.value,
    }));
    for (const b of utt.buffered) utt.ws.send(b);
    utt.buffered = [];
    if (utt.stopped) utt.ws.send(JSON.stringify({ type: "stop" }));
  };
  utt.ws.onmessage = (ev) => onServerMessage(utt, JSON.parse(ev.data));
  utt.ws.onerror = () => fail(utt, "Connection problem. Try again.");
  utt.ws.onclose = (ev) => {
    if (ev.code === 4401) fail(utt, "Access key missing or wrong.");
    else if (!utt.finished && utt.stopped) fail(utt, "Connection closed. Try again.");
  };

  worklet.port.onmessage = (ev) => {
    if (ev.data === "flushed") {
      utt.stopped = true;
      if (utt.ws.readyState === WebSocket.OPEN) utt.ws.send(JSON.stringify({ type: "stop" }));
      return;
    }
    if (utt.ws.readyState === WebSocket.OPEN) utt.ws.send(ev.data);
    else utt.buffered.push(ev.data);
  };
  worklet.port.postMessage("start");
  if (!awaitingAnswer) clearCard();
  showTranscript("", false);
  setButton("recording");
  navigator.vibrate?.(20);
}

function pressEnd() {
  const utt = current;
  if (!utt || utt.releasing) return;
  utt.releasing = true;
  worklet.port.postMessage("stop");
  if (performance.now() - utt.started < MIN_HOLD_MS) {
    // Accidental tap: drop it.
    worklet.port.onmessage = null;
    utt.finished = true;
    if (utt.ws.readyState === WebSocket.OPEN) utt.ws.send(JSON.stringify({ type: "cancel" }));
    utt.ws.close();
    current = null;
    el.transcript.hidden = true;
    el.hint.hidden = false;
    setButton("idle");
    return;
  }
  busy = true;
  setButton("busy");
}

function onServerMessage(utt, msg) {
  if (msg.type === "error" && msg.code === "pin") {
    finish(utt);
    setButton("idle");
    askPin(msg.message);
    return;
  }
  if (msg.type === "partial") showTranscript(msg.text, false);
  else if (msg.type === "final") showTranscript(msg.text, true);
  else if (msg.type === "result") {
    finish(utt);
    handleResult(msg);
  } else if (msg.type === "error") fail(utt, msg.message);
}

function finish(utt) {
  utt.finished = true;
  if (current === utt) current = null;
  busy = false;
  utt.ws.close();
}

function fail(utt, message) {
  if (utt.finished) return;
  finish(utt);
  showCard("error", message);
  speak(message);
  flashButton(false, "Not posted");
}

// Hold-to-talk on touch, mouse and pen; space bar on desktop.
el.ptt.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  el.ptt.setPointerCapture?.(e.pointerId);
  pressStart();
});
for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) {
  el.ptt.addEventListener(ev, pressEnd);
}
el.ptt.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && document.activeElement !== el.typedInput) {
    e.preventDefault();
    pressStart();
  }
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && document.activeElement !== el.typedInput) pressEnd();
});

// ---- typed fallback and option taps --------------------------------------------------------

async function answerTyped(text, via = "voice") {
  if (busy || !text.trim() || !worker) return;
  unlockSpeech();
  busy = true;
  setButton("busy");
  showTranscript(text, true);
  try {
    const r = awaitingAnswer
      ? await api("/clarify", { session_id: sessionId, answer: text, via })
      : await api("/act", { session_id: sessionId, transcript: text,
                            speech: el.langOverride.value });
    busy = false;
    handleResult(r);
  } catch (e) {
    busy = false;
    awaitingAnswer = false;
    sessionId = newId();
    showCard("error", e.message);
    setButton("idle");
  }
}

el.typed.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = el.typedInput.value;
  el.typedInput.value = "";
  el.typedInput.blur();
  answerTyped(text);
});

// ---- "Show me the part": photo answers an item question ---------------------------------

const PHOTO_MAX_EDGE = 1280;

async function downscale(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not encode photo"))),
      "image/jpeg", 0.8));
}

el.photoBtn.addEventListener("click", () => {
  if (busy || !awaitingAnswer) return;
  unlockSpeech();
  el.photoInput.value = "";
  el.photoInput.click();
});

el.photoInput.addEventListener("change", async () => {
  const file = el.photoInput.files?.[0];
  if (!file || busy) return;
  busy = true;
  setButton("busy");
  el.photoBtn.disabled = true;
  let thumbUrl = "";
  try {
    const jpeg = await downscale(file);
    thumbUrl = URL.createObjectURL(jpeg);
    el.photoThumb.src = thumbUrl;
    el.photoThumb.hidden = false;
    el.photoThumb.classList.add("reading");
    const form = new FormData();
    form.append("session_id", sessionId);
    form.append("image", jpeg, "part.jpg");
    const r = await checked(await fetch("/clarify-photo", {
      method: "POST", headers: headers(false), body: form,
    }));
    busy = false;
    handleResult(r);
  } catch (e) {
    busy = false;
    el.photoBtn.disabled = false;
    el.photoThumb.classList.remove("reading");
    setButton("idle");
    el.cardMeta.textContent = `Photo failed: ${e.message}`;
  } finally {
    if (thumbUrl) setTimeout(() => URL.revokeObjectURL(thumbUrl), 30000);
  }
});

// ---- stock count by video: film the shelf, review, confirm --------------------------------

const MIN_CLIP_MS = 5000;
const MAX_CLIP_MS = 15000;
let camStream = null;
let recorder = null;
let filmTimer = 0;

function stopCamera() {
  clearInterval(filmTimer);
  if (recorder && recorder.state !== "inactive") {
    recorder.onstop = null;
    recorder.stop();
  }
  recorder = null;
  camStream?.getTracks().forEach((t) => t.stop());
  camStream = null;
  el.countVideo.srcObject = null;
}

// Camera problems on phones are invisible from the server: report them to its log.
function clientLog(event, detail) {
  fetch("/client-log", { method: "POST", headers: headers(),
    body: JSON.stringify({ event, detail: String(detail).slice(0, 500), ua: navigator.userAgent }),
  }).catch(() => {});
}

function cameraError(e) {
  const msg = `${e.name || "Error"}: ${e.message || e}`;
  clientLog("camera", msg);
  el.cardMeta.textContent = `Camera didn't start (${msg}). Tap "Use the camera app instead".`;
  el.cardMeta.className = "card-meta count-error";
}

async function startPreview() {
  if (camStream) return true;
  if (!navigator.mediaDevices?.getUserMedia) {
    cameraError(new Error("this browser has no camera access"));
    return false;
  }
  try {
    camStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
  } catch (e) {
    cameraError(e);
    return false;
  }
  el.countVideo.srcObject = camStream;
  el.countVideo.muted = true;
  el.countVideo.setAttribute("playsinline", "");
  try { await el.countVideo.play(); } catch (e) { clientLog("preview-play", `${e.name}: ${e.message}`); }
  return true;
}

function clipType() {
  if (!window.MediaRecorder) return null;
  const types = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp8", "video/webm"];
  return types.find((t) => MediaRecorder.isTypeSupported?.(t)) ?? "";
}

el.countBtn.addEventListener("click", async () => {
  if (busy) return;
  unlockSpeech();
  if (recorder && recorder.state === "recording") {
    recorder.stop(); // onstop uploads
    return;
  }
  const type = clipType();
  if (type === null) { // no MediaRecorder: the phone's own camera app
    clientLog("recorder", "MediaRecorder not available");
    el.countInput.value = "";
    el.countInput.click();
    return;
  }
  if (!(await startPreview())) return;
  const chunks = [];
  try {
    recorder = new MediaRecorder(camStream, type ? { mimeType: type } : undefined);
  } catch (e) {
    clientLog("recorder", `${e.name}: ${e.message} (type ${type})`);
    cameraError(e);
    return;
  }
  recorder.ondataavailable = (ev) => ev.data.size && chunks.push(ev.data);
  const started = performance.now();
  recorder.onstop = () => {
    clearInterval(filmTimer);
    const mime = (recorder?.mimeType || type || "video/mp4").split(";")[0];
    stopCamera();
    uploadClip(new Blob(chunks, { type: mime }), mime);
  };
  recorder.start(500);
  el.countRow.classList.add("filming");
  el.countTimer.hidden = false;
  el.countBtn.disabled = true;
  el.countBtn.textContent = "■ Stop";
  filmTimer = setInterval(() => {
    const ms = performance.now() - started;
    el.countTimer.textContent = `${Math.floor(ms / 1000)} s`;
    if (ms >= MIN_CLIP_MS) el.countBtn.disabled = false;
    if (ms >= MAX_CLIP_MS && recorder?.state === "recording") recorder.stop();
  }, 200);
  navigator.vibrate?.(20);
});

el.countNative.addEventListener("click", () => {
  if (busy) return;
  stopCamera();
  el.countInput.value = "";
  el.countInput.click();
});

el.countInput.addEventListener("change", () => {
  const file = el.countInput.files?.[0];
  if (file) uploadClip(file, (file.type || "video/mp4").split(";")[0]);
});

async function uploadClip(blob, mime) {
  if (busy) return;
  busy = true;
  setButton("busy");
  el.countRow.classList.remove("filming");
  el.countBtn.disabled = true;
  el.countBtn.textContent = "Counting…";
  el.cardMeta.textContent = "";
  const ext = mime.includes("webm") ? "webm" : mime.includes("quicktime") ? "mov" : "mp4";
  try {
    const form = new FormData();
    form.append("session_id", sessionId);
    form.append("video", blob, `shelf.${ext}`);
    const r = await checked(await fetch("/count-video", {
      method: "POST", headers: headers(false), body: form,
    }));
    busy = false;
    handleResult(r);
  } catch (e) {
    busy = false;
    setButton("idle");
    el.countBtn.disabled = false;
    el.countBtn.textContent = "● Film again";
    el.cardMeta.textContent = `Count failed: ${e.message}`;
  }
}

function showReview(review) {
  el.reviewRow.hidden = !review;
  el.reviewNote.textContent = "";
  const ctx = el.reviewCanvas.getContext("2d");
  ctx.clearRect(0, 0, el.reviewCanvas.width, el.reviewCanvas.height);
  el.reviewImg.closest(".review-pic").hidden = !review?.frame_url;
  if (!review?.frame_url) return;
  el.reviewImg.onload = () => {
    const w = review.width || el.reviewImg.naturalWidth;
    const h = review.height || el.reviewImg.naturalHeight;
    el.reviewCanvas.width = w;
    el.reviewCanvas.height = h;
    ctx.lineWidth = Math.max(3, w / 300);
    ctx.font = `700 ${Math.round(w / 28)}px system-ui`;
    for (const b of review.boxes || []) {
      ctx.strokeStyle = "#f5b301";
      ctx.strokeRect(b.x1, b.y1, b.x2 - b.x1, b.y2 - b.y1);
      const label = String(b.n);
      const pad = w / 160;
      const tw = ctx.measureText(label).width + pad * 2;
      const th = w / 24;
      ctx.fillStyle = "#f5b301";
      ctx.fillRect(b.x1, b.y1, tw, th);
      ctx.fillStyle = "#111";
      ctx.fillText(label, b.x1 + pad, b.y1 + th * 0.8);
    }
  };
  el.reviewImg.src = `${review.frame_url}?k=${encodeURIComponent(accessKey)}`;
  const inView = (review.boxes || []).length;
  if (review.counted != null && inView < review.counted) {
    el.reviewNote.textContent = `This frame shows ${inView}; all ${review.counted} were tracked across the pan.`;
  }
}

async function countAction(action) {
  if (busy) return;
  unlockSpeech();
  busy = true;
  setButton("busy");
  try {
    const r = await api("/count-action", { session_id: sessionId, action });
    busy = false;
    handleResult(r);
  } catch (e) {
    busy = false;
    setButton("idle");
    el.cardMeta.textContent = e.message;
  }
}

// ---- history ---------------------------------------------------------------------------

async function refreshHistory() {
  if (!worker) return;
  try {
    const rows = await api("/history?limit=15");
    el.history.innerHTML = "";
    for (const h of rows) {
      const li = document.createElement("li");
      const said = document.createElement("div");
      said.className = "said";
      said.textContent = [h.transcript, ...h.answers].join(" → ");
      const did = document.createElement("div");
      const undone = (h.documents || []).some((d) => d.undone);
      did.className = `did ${undone ? "undone" : h.kind}`;
      did.textContent = undone ? `${h.text} (undone)` : h.text;
      li.append(said, did, erpLinks(h.documents || []));
      el.history.append(li);
    }
  } catch { /* history is best-effort */ }
}

if (!window.isSecureContext) {
  showCard("error", "Microphone needs HTTPS. Open the tunnel (https://) link.");
}
startup();
setButton("idle");
