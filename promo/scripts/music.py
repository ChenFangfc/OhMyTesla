"""The film's score, synthesised and cut to the picture: 120 BPM, a bar is 2 s.

    python3 promo/scripts/music.py            # → promo/out/soundtrack.wav (36 s, 48 kHz stereo)

  0–4 s    a pulse and a sub under the three words; a hit on each word; a riser
  4 s      the drop, on the light bar: crash, sub, a bright stab; the groove starts
  8–10 s   a soft blip as each callout pops out on the truck
  11–13 s  two notification pings land with the pushes on the phone
  15–22 s  a 16th arp joins the groove for the data and the map
  23–25 s  quiet key ticks while the assistant's answer types out
  28–31 s  stop-time: a hit and a stab on each of the three words, a riser
  32 s     the last hit for the end card, then the chord rings out
  A soft swoosh leads into each scene.

D minor, one chord a bar: Dm – B♭ – F – C. Drums, bass and pads are made
here from oscillators and noise; nothing is sampled. Loudness is set when the
film is muxed (ffmpeg loudnorm, EBU R128). Needs numpy and scipy.
"""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, sosfilt

SR = 48000
DUR = 36.0
BEAT = 0.5
OUT = Path(__file__).resolve().parent.parent / "out" / "soundtrack.wav"
RNG = np.random.default_rng(11)
N = int(DUR * SR)

# Cues from src/film.js
WORDS = (1.0, 2.0, 3.0)          # the three opening words
DROP = 4.0                       # the light bar
CALLOUTS = (8.3, 8.7, 9.1, 9.5)  # the car's state pops out on the truck
PINGS = (11.6, 12.6)             # the two pushes
TOOL = 22.95                     # the assistant calls the tool
TYPING = (23.4, 25.0)            # the answer types out
CUTS = (11.0, 15.0, 19.0, 22.0, 26.0)   # a swoosh into each scene
STOPS = (28.0, 29.0, 30.0)       # self-hosted, open source, free
END = 32.0                       # the end card
GROOVE = (DROP, STOPS[0])        # where the drums play

# one chord a bar, as MIDI notes (root, then the voicing)
PROG = [
    (38, [62, 65, 69, 74]),      # Dm
    (34, [62, 65, 70, 74]),      # B♭
    (41, [60, 65, 69, 72]),      # F
    (36, [60, 64, 67, 72]),      # C
]


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def lp(x, f, order=2):
    return sosfilt(butter(order, min(f, SR / 2 - 100) / (SR / 2), "low", output="sos"), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "high", output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo / (SR / 2), min(hi, SR / 2 - 100) / (SR / 2)], "band", output="sos"), x)


def ts(dur):
    return np.arange(int(dur * SR)) / SR


def chord_at(t):
    return PROG[int(t // 2.0) % len(PROG)]


class Bus:
    def __init__(self):
        self.l = np.zeros(N)
        self.r = np.zeros(N)

    def add(self, t0, sig, gain=1.0, pan=0.0, sig_r=None):
        i = int(round(t0 * SR))
        if i >= N or i + len(sig) <= 0:
            return
        sr = sig if sig_r is None else sig_r
        a = max(0, -i)
        n = min(len(sig) - a, N - max(i, 0))
        gl, gr = gain * np.sqrt(0.5 * (1 - pan)), gain * np.sqrt(0.5 * (1 + pan))
        self.l[max(i, 0):max(i, 0) + n] += gl * sig[a:a + n]
        self.r[max(i, 0):max(i, 0) + n] += gr * sr[a:a + n]


# ── instruments ──────────────────────────────────────────────────────────
def kick():
    t = ts(0.42)
    f = 46 + 110 * np.exp(-t * 34)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7)
    click = hp(RNG.standard_normal(len(t)), 2500) * np.exp(-t * 400) * 0.35
    return np.tanh(1.6 * (body + click)) * 0.9


def clap():
    t = ts(0.35)
    n = RNG.standard_normal(len(t))
    e = np.zeros(len(t))
    for d in (0.0, 0.011, 0.023):
        e += np.where(t >= d, np.exp(-(t - d) * (90 if d < 0.02 else 16)), 0)
    return bp(n, 900, 5200) * e * 0.55


def hat(open_=False):
    t = ts(0.25 if open_ else 0.06)
    return hp(RNG.standard_normal(len(t)), 7500, 4) * np.exp(-t * (14 if open_ else 70)) * 0.22


def bass(m, dur):
    t = ts(dur)
    f = hz(m)
    saw = 2 * ((f * t) % 1.0) - 1
    sub = np.sin(2 * np.pi * f * t)
    cut = 260 + 900 * np.exp(-t * 14)
    x = np.zeros_like(t)
    # a falling low-pass, done in short blocks
    blk = 512
    for i in range(0, len(t), blk):
        x[i:i + blk] = lp(saw[i:i + blk], cut[i], 2)
    e = np.minimum(1, t / 0.004) * np.exp(-t * 2.2)
    return np.tanh(1.4 * (0.55 * x + 0.75 * sub)) * e * 0.5


def pad(notes, dur):
    t = ts(dur)
    l, r = np.zeros_like(t), np.zeros_like(t)
    for m in notes:
        for det, side in ((-0.09, -1), (0.0, 0), (0.09, 1)):
            f = hz(m) * 2 ** (det / 12)
            ph = RNG.uniform(0, 1)
            v = 2 * ((f * t + ph) % 1.0) - 1
            if side <= 0:
                l += v
            if side >= 0:
                r += v
    e = np.minimum(1, t / 0.35) * np.minimum(1, np.maximum(0, (dur - t) / 0.4))
    k = 0.06 / len(notes)
    return lp(l, 1600, 2) * e * k, lp(r, 1600, 2) * e * k


def stab(notes, dur=0.9, bright=5200):
    t = ts(dur)
    x = np.zeros_like(t)
    for m in notes:
        for det in (-0.12, 0.0, 0.12):
            f = hz(m + 12) * 2 ** (det / 12)
            x += 2 * ((f * t + RNG.uniform(0, 1)) % 1.0) - 1
    return lp(x, bright, 2) * np.exp(-t * 5.5) * np.minimum(1, t / 0.003) * (0.11 / len(notes))


def pluck(m, dur=0.3):
    t = ts(dur)
    f = hz(m)
    saw = 2 * ((f * t) % 1.0) - 1
    return lp(saw * np.exp(-t * 16), 3600, 2) * np.minimum(1, t / 0.002) * 0.12


def impact(big=1.0):
    t = ts(2.6)
    f = 30 + 60 * np.exp(-t * 6)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.4)
    crash = hp(RNG.standard_normal(len(t)), 3000) * np.exp(-t * 2.8) * 0.28
    body = lp(RNG.standard_normal(len(t)), 900) * np.exp(-t * 9) * 0.6
    return np.tanh(1.3 * big * (boom + crash + body)) * 0.8


def thump():
    t = ts(0.9)
    f = 38 + 70 * np.exp(-t * 20)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 5) * 0.9


def riser(dur):
    t = ts(dur)
    n = RNG.standard_normal(len(t))
    out = np.zeros_like(t)
    blocks = 32
    for b in range(blocks):
        a, z = b * len(t) // blocks, (b + 1) * len(t) // blocks
        fc = 300 * 2 ** (5.2 * b / blocks)
        out[a:z] = bp(n[a:z], fc / 1.5, fc * 1.5)
    tone = np.sin(2 * np.pi * np.cumsum(180 * 2 ** (2.5 * t / dur)) / SR) * 0.25
    e = (t / dur) ** 2.4
    return (out + tone) * e * 0.4


def ping(notes, dur=1.4, gain=0.22):
    t = ts(dur)
    x = np.zeros_like(t)
    for k, m in enumerate(notes):
        tt = t - 0.09 * k
        on = tt >= 0
        f = hz(m)
        x += on * (np.sin(2 * np.pi * f * tt) * np.exp(-np.maximum(tt, 0) * 5)
                   + 0.3 * np.sin(2 * np.pi * f * 2.0 * tt) * np.exp(-np.maximum(tt, 0) * 9))
    return x * gain


def tick():
    t = ts(0.03)
    return hp(RNG.standard_normal(len(t)), 3500) * np.exp(-t * 260) * 0.25


def blip(m):
    t = ts(0.35)
    return np.sin(2 * np.pi * hz(m) * t) * np.exp(-t * 14) * np.minimum(1, t / 0.002) * 0.3


def swoosh(dur=0.45):
    t = ts(dur)
    n = RNG.standard_normal(len(t))
    out = np.zeros_like(t)
    blocks = 12
    for b in range(blocks):
        a, z = b * len(t) // blocks, (b + 1) * len(t) // blocks
        fc = 600 * 2 ** (2.5 * b / blocks)
        out[a:z] = bp(n[a:z], fc / 1.6, fc * 1.6)
    e = np.sin(np.pi * t / dur) ** 2
    return out * e * 0.18


def reverb(l, r, seconds=2.2, wet=0.3):
    t = ts(seconds)
    d = np.exp(-t * 3.2 / seconds)
    irl = lp(RNG.standard_normal(len(t)), 7000) * d
    irr = lp(RNG.standard_normal(len(t)), 7000) * d
    irl /= np.sqrt(np.sum(irl ** 2))
    irr /= np.sqrt(np.sum(irr ** 2))
    return l + wet * fftconvolve(l, irl)[:N], r + wet * fftconvolve(r, irr)[:N]


# ── the arrangement ──────────────────────────────────────────────────────
def arrange():
    drums, music, fx = Bus(), Bus(), Bus()
    kicks = []

    # intro: a sub on D and a filtered 16th pulse that opens up toward the drop
    t = ts(DROP)
    sub = np.sin(2 * np.pi * hz(38) * t) * np.minimum(1, t / 0.8) * 0.12
    music.add(0, sub)
    for k in range(int(DROP / 0.125)):
        at = k * 0.125
        m = (50, 57, 62, 57)[k % 4]
        p = pluck(m, 0.18) * (0.35 + 0.65 * at / DROP)
        music.add(at, lp(p, 600 + 2600 * at / DROP), gain=0.55, pan=0.25 * np.sin(k))
    for w in WORDS:
        fx.add(w, thump(), gain=0.8)
        fx.add(w, stab(PROG[0][1], 0.6, 2400), gain=0.6)
    fx.add(DROP - 1.0, riser(1.0), gain=0.9)

    # the drop
    fx.add(DROP, impact(1.0), gain=1.0)
    fx.add(DROP, stab(PROG[0][1], 1.4), gain=1.1)

    # the groove: four on the floor, claps on 2 and 4, offbeat hats
    a, b = GROOVE
    beat = a
    while beat < b - 1e-6:
        k = int(round((beat - a) / BEAT))
        drums.add(beat, kick(), gain=0.95)
        kicks.append(beat)
        if k % 2 == 1:
            drums.add(beat, clap(), gain=0.7)
        drums.add(beat + BEAT / 2, hat(open_=(k % 4 == 3)), gain=0.9, pan=0.3)
        if beat >= 15.0:
            drums.add(beat + BEAT / 4, hat(), gain=0.35, pan=-0.3)
            drums.add(beat + 3 * BEAT / 4, hat(), gain=0.35, pan=-0.3)
        beat += BEAT

    # bass in eighths and a pad, one chord a bar, from the drop to the stops
    bar = DROP
    while bar < STOPS[0] - 1e-6:
        root, notes = chord_at(bar)
        for e in range(8):
            at = bar + e * 0.25
            if at >= STOPS[0]:
                break
            music.add(at, bass(root + (12 if e % 4 == 3 else 0), 0.24), gain=1.3)
        l, r = pad(notes, min(2.0, STOPS[0] - bar) + 0.3)
        music.add(bar, l, sig_r=r, gain=2.6)
        bar += 2.0

    # a 16th arp over the data and the map
    for k in range(int((22.0 - 15.0) / 0.125)):
        at = 15.0 + k * 0.125
        _, notes = chord_at(at)
        m = [notes[0] + 12, notes[2], notes[1] + 12, notes[3]][k % 4]
        music.add(at, pluck(m, 0.25), gain=0.9, pan=0.4 * np.sin(k * 0.9))

    # the callouts, the phone, the assistant
    for i, at in enumerate(CALLOUTS):
        fx.add(at, blip((86, 88, 90, 93)[i]), gain=0.7, pan=0.3 * (i - 1.5))
    fx.add(PINGS[0], ping([88, 95]), gain=0.9)
    fx.add(PINGS[1], ping([93, 100, 93]), gain=1.1)
    fx.add(TOOL, blip(91), gain=0.6)
    k = 0
    at = TYPING[0]
    while at < TYPING[1]:
        fx.add(at, tick(), gain=0.5 + 0.4 * ((k * 7) % 5) / 5, pan=0.2 * np.sin(k))
        at += 0.055 + 0.02 * ((k * 3) % 4) / 4
        k += 1
    for c in CUTS:
        fx.add(c - 0.42, swoosh(), gain=0.8)

    # stop-time: a hit and a stab per word, then a riser into the end card
    for i, s in enumerate(STOPS):
        root, notes = PROG[(0, 1, 3)[i]]
        fx.add(s, thump(), gain=0.9)
        fx.add(s, stab(notes, 0.8), gain=1.0)
        music.add(s, bass(root, 0.9), gain=0.9)
        kicks.append(s)
    fx.add(END - 1.0, riser(1.0), gain=1.0)

    # the end card: the last hit, the chord rings out
    fx.add(END, impact(1.1), gain=1.0)
    fx.add(END, stab(PROG[0][1], 2.2, 3800), gain=1.0)
    l, r = pad(PROG[0][1] + [69 + 12], DUR - END)
    music.add(END, l, sig_r=r, gain=1.6)
    t = ts(DUR - END)
    music.add(END, np.sin(2 * np.pi * hz(38) * t) * np.exp(-t * 0.6) * 0.5)

    # sidechain: the bass and pads duck under every kick
    duck = np.ones(N)
    tt = np.arange(N) / SR
    for k in kicks:
        i = int(k * SR)
        j = min(N, i + int(0.3 * SR))
        duck[i:j] = np.minimum(duck[i:j], 1 - 0.65 * np.exp(-(tt[i:j] - k) / 0.09))
    music.l *= duck
    music.r *= duck
    return drums, music, fx


def main():
    drums, music, fx = arrange()
    tt = np.arange(N) / SR
    fx.l, fx.r = reverb(fx.l, fx.r, 2.6, 0.45)
    music.l, music.r = reverb(music.l, music.r, 1.8, 0.18)
    l = drums.l + music.l + fx.l
    r = drums.r + music.r + fx.r
    l, r = hp(l, 28), hp(r, 28)
    fade_out = np.clip((DUR - tt) / 1.4, 0, 1) ** 1.3
    l, r = l * fade_out, r * fade_out
    peak = max(np.max(np.abs(l)), np.max(np.abs(r)), 1e-9)
    l, r = np.tanh(l / peak * 1.25) * 0.92, np.tanh(r / peak * 1.25) * 0.92
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(OUT, SR, (np.stack([l, r], axis=1) * 32767).astype(np.int16))
    print(f"wrote {OUT} ({DUR:.0f} s)")


if __name__ == "__main__":
    main()
