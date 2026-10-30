// FloorVoice PWA: hold-to-talk -> WS /audio -> transcript + agent result -> spoken readback.

const TARGET_RATE = 24000;
const MIN_HOLD_MS = 300;

const $ = (id) => document.getElementById(id);
const el = {
  ptt: $("ptt"), pttLabel: $("ptt-label"), hint: $("hint"), transcript: $("transcript"),
  card: $("card"), cardText: $("card-text"), cardMeta: $("card-meta"), options: $("options"),
  history: $("history"), home: $("home"), typed: $("typed"), typedInput: $("typed-input"),
};

// ---- settings: access key (?k=...) and home warehouse, remembered per device ----------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private mode: settings just don't persist */ }
  return value;
}

const params = new URLSearchParams(location.search);
if (params.get("k")) {
  store("fv.key", params.get("k"));
  history.replaceState(null, "", location.pathname);
}
const accessKey = params.get("k") || store("fv.key") || "";

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-FloorVoice-Key": accessKey },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

async function loadWarehouses() {
  try {
    const list = await api("/warehouses");
    const saved = store("fv.home");
    el.home.innerHTML = "";
    for (const w of list) {
      const opt = new Option(w.label, w.name);
      el.home.add(opt);
    }
    const fallback = list.find((w) => /line/i.test(w.label)) || list[0];
    el.home.value = list.some((w) => w.name === saved) ? saved : fallback?.name;
  } catch (e) {
    showCard("error", e.message === "Missing or wrong access key"
      ? "This link needs an access key. Open the full link with ?k=…" : `Server: ${e.message}`);
  }
}
el.home.addEventListener("change", () => store("fv.home", el.home.value));

// ---- UI state ---------------------------------------------------------------------------

let sessionId = newId();
let awaitingAnswer = false; // last result was a question: next utterance answers it
let busy = false;

function newId() {
  return crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "")
    : Math.random().toString(16).slice(2) + Date.now().toString(16);
}

function setButton(mode) {
  el.ptt.classList.toggle("recording", mode === "recording");
  el.ptt.classList.toggle("busy", mode === "busy");
  el.pttLabel.textContent = mode === "recording" ? "Release to send"
    : mode === "busy" ? "Working…" : awaitingAnswer ? "Hold to answer" : "Hold to talk";
}

function showTranscript(text, final) {
  el.hint.hidden = true;
  el.transcript.hidden = false;
  el.transcript.textContent = text || "…";
  el.transcript.classList.toggle("final", !!final);
}

function showCard(kind, text, { options = [], meta = "" } = {}) {
  el.card.hidden = false;
  el.card.className = `card ${kind}`;
  el.cardText.textContent = text;
  el.cardMeta.textContent = meta;
  el.options.innerHTML = "";
  for (const opt of options) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = opt;
    b.addEventListener("click", () => answerTyped(opt));
    el.options.append(b);
  }
}

function clearCard() {
  el.card.hidden = true;
}

function handleResult(r) {
  awaitingAnswer = r.kind === "ask";
  if (!awaitingAnswer) sessionId = newId();
  const timing = r.total_ms ? `${(r.total_ms / 1000).toFixed(1)} s from release`
    : `${(r.elapsed_ms / 1000).toFixed(1)} s`;
  showCard(r.kind, r.text, { options: r.options || [], meta: timing });
  speak(r.speech || r.text);
  setButton("idle");
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
  const tamil = /[஀-௿]/.test(text);
  const u = new SpeechSynthesisUtterance(text);
  const voice = tamil ? pickVoice("ta") : (pickVoice("en-in") || pickVoice("en"));
  if (voice) u.voice = voice;
  u.lang = voice ? voice.lang : tamil ? "ta-IN" : "en-IN";
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
  if (busy || current) return;
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
      home_warehouse: el.home.value || null,
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
  setButton("idle");
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

async function answerTyped(text) {
  if (busy || !text.trim()) return;
  unlockSpeech();
  busy = true;
  setButton("busy");
  showTranscript(text, true);
  try {
    const r = awaitingAnswer
      ? await api("/clarify", { session_id: sessionId, answer: text })
      : await api("/act", { session_id: sessionId, transcript: text,
                            home_warehouse: el.home.value || null });
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

// ---- history ---------------------------------------------------------------------------

async function refreshHistory() {
  try {
    const rows = await api("/history?limit=15");
    el.history.innerHTML = "";
    for (const h of rows) {
      const li = document.createElement("li");
      const said = document.createElement("div");
      said.className = "said";
      said.textContent = [h.transcript, ...h.answers].join(" → ");
      const did = document.createElement("div");
      did.className = `did ${h.kind}`;
      did.textContent = h.text;
      li.append(said, did);
      el.history.append(li);
    }
  } catch { /* history is best-effort */ }
}

if (!window.isSecureContext) {
  showCard("error", "Microphone needs HTTPS. Open the tunnel (https://) link.");
}
loadWarehouses();
refreshHistory();
setButton("idle");
