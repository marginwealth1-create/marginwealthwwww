/**
 * Trade-side audio cues — BUY plays a short ascending two-note chirp, SELL
 * plays a descending one. Synthesised on the fly via the Web Audio API so we
 * don't ship any audio assets and can't trigger 404s.
 *
 * Browsers block AudioContext until a user gesture; the first click on the
 * BUY/SELL pill IS the gesture, so this works without any pre-priming.
 */

let _ctx: AudioContext | null = null;

function ctx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (_ctx && _ctx.state !== "closed") return _ctx;
  const Ctor: typeof AudioContext | undefined =
    (window as any).AudioContext || (window as any).webkitAudioContext;
  if (!Ctor) return null;
  try {
    _ctx = new Ctor();
  } catch {
    return null;
  }
  return _ctx;
}

function chirp(notes: { freq: number; ms: number }[], gain = 0.18) {
  const ac = ctx();
  if (!ac) return;
  // Some browsers leave the context suspended until a user gesture; resume.
  if (ac.state === "suspended") {
    ac.resume().catch(() => undefined);
  }
  let t = ac.currentTime;
  for (const n of notes) {
    const osc = ac.createOscillator();
    const g = ac.createGain();
    osc.type = "sine";
    osc.frequency.value = n.freq;

    // Quick attack/decay envelope so the chirp doesn't click.
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + n.ms / 1000);

    osc.connect(g).connect(ac.destination);
    osc.start(t);
    osc.stop(t + n.ms / 1000 + 0.02);

    t += n.ms / 1000;
  }
}

/** BUY → bright two-note ascending chirp (think mt4-style ka-ching). */
export function playBuyTone(): void {
  chirp([
    { freq: 880, ms: 70 },   // A5
    { freq: 1318.5, ms: 110 }, // E6
  ]);
}

/** SELL → low two-note descending chirp. */
export function playSellTone(): void {
  chirp([
    { freq: 660, ms: 70 },   // E5
    { freq: 392, ms: 110 },  // G4
  ]);
}

/** Position/order CLOSED — neutral confirmation blip. */
export function playClosedTone(): void {
  chirp([{ freq: 540, ms: 60 }, { freq: 720, ms: 80 }], 0.14);
}
