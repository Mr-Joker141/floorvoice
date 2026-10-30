// AudioWorklet: microphone float32 at the context rate -> s16le mono at targetRate,
// posted to the main thread in ~80 ms chunks.
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = options.processorOptions.targetRate;
    this.ratio = sampleRate / this.target;
    this.chunk = new Int16Array(Math.round(this.target * 0.08));
    this.n = 0;
    this.t = 0; // fractional read position relative to the current block (may be -1..0)
    this.prev = 0; // last sample of the previous block
    this.active = false;
    this.port.onmessage = (e) => {
      if (e.data === "start") {
        this.active = true;
        this.n = 0;
        this.t = 0;
      } else if (e.data === "stop") {
        this.active = false;
        this.flush();
        this.port.postMessage("flushed");
      }
    };
  }

  flush() {
    if (this.n > 0) {
      const out = this.chunk.slice(0, this.n);
      this.port.postMessage(out.buffer, [out.buffer]);
      this.n = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !this.active) return true;
    const len = ch.length;
    let t = this.t;
    // Linear interpolation between samples i0 and i0+1 (i0 = -1 means previous block).
    while (t < len - 1) {
      const i0 = Math.floor(t);
      const frac = t - i0;
      const s0 = i0 < 0 ? this.prev : ch[i0];
      const s1 = ch[i0 + 1];
      let s = s0 + (s1 - s0) * frac;
      s = Math.max(-1, Math.min(1, s));
      this.chunk[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === this.chunk.length) this.flush();
      t += this.ratio;
    }
    this.t = t - len;
    this.prev = ch[len - 1];
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
