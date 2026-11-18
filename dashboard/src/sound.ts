// Distinct alert sounds generated with the Web Audio API (no audio files).
// Browsers only allow audio after a user gesture, so the header's sound toggle
// calls `enableSound()`.

let ctx: AudioContext | null = null;

export function enableSound(): boolean {
  try {
    if (!ctx) ctx = new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    return true;
  } catch {
    return false;
  }
}

export function soundReady(): boolean {
  return ctx !== null && ctx.state === "running";
}

type Note = [freq: number, start: number, dur: number, type?: OscillatorType, gain?: number];

const PATTERNS: Record<string, Note[]> = {
  provisional: [[880, 0, 0.09]],
  provisional_s2: [[660, 0, 0.08], [990, 0.1, 0.1]],
  confirmed: [[523, 0, 0.12], [784, 0.14, 0.16]],
  armed: [[440, 0, 0.18, "triangle", 0.18]],
  entry: [[659, 0, 0.12], [880, 0.13, 0.12], [1175, 0.26, 0.22]],
  target: [[784, 0, 0.1], [1047, 0.12, 0.18]],
  exit: [[392, 0, 0.16, "triangle"], [262, 0.18, 0.22, "triangle"]],
  warning: [[220, 0, 0.15, "sawtooth", 0.12], [220, 0.2, 0.15, "sawtooth", 0.12]],
  system: [[300, 0, 0.25, "square", 0.08]],
};

export function play(kind: string | null) {
  if (!kind || !ctx || ctx.state !== "running") return;
  const notes = PATTERNS[kind];
  if (!notes) return;
  const t0 = ctx.currentTime + 0.01;
  for (const [freq, start, dur, type = "sine", gain = 0.22] of notes) {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    g.gain.setValueAtTime(0, t0 + start);
    g.gain.linearRampToValueAtTime(gain, t0 + start + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + start + dur);
    osc.connect(g).connect(ctx.destination);
    osc.start(t0 + start);
    osc.stop(t0 + start + dur + 0.02);
  }
}
