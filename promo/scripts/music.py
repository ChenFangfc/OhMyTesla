"""The film's soundtrack, synthesised: scored to the picture's scene cues.

    python3 promo/scripts/music.py            # → promo/out/soundtrack.wav (31 s, 48 kHz stereo)

One chord per scene, changing on the cut; a bell on each title line; a chime
when the push lands on the phone; a low swell into the end card. Warm, quiet
and unhurried, under the words rather than on top of them. Loudness is set
afterwards by ffmpeg's loudnorm (EBU R128) when the film is muxed.
Needs numpy and scipy.
"""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, fftconvolve

SR = 48000
DUR = 31.0
OUT = Path(__file__).resolve().parent.parent / "out" / "soundtrack.wav"
RNG = np.random.default_rng(7)
N = int(DUR * SR)

# Scene starts (seconds) — the same cut points as src/film.js.
CUTS = [0.0, 4.8, 9.2, 13.8, 18.2, 21.4, 24.8, 27.8]
# One chord per scene (MIDI notes), in D major: home, lift, a question, motion,
# open space, settle, lean, home again.
CHORDS = [
    [50, 57, 62, 66, 69, 76],     # Dmaj9      the idea
    [47, 54, 59, 62, 66, 73],     # Bm9        overview
    [43, 50, 55, 59, 62, 69],     # Gmaj9      Sentry
    [40, 47, 52, 55, 62, 66],     # Em9        data
    [42, 50, 54, 57, 62, 69],     # D/F#       footprint
    [43, 50, 55, 59, 64, 71],     # G6/9       controls
    [45, 52, 57, 62, 64, 69],     # Asus4      the Mac
    [38, 50, 57, 62, 66, 76],     # Dmaj9 low  end card
]


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def lp(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "low", output="sos"), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f / (SR / 2), "high", output="sos"), x)


def env(n, a, r, sustain=1.0):
    """Attack/release envelope over n samples (a, r in seconds)."""
    t = np.arange(n) / SR
    e = np.minimum(1.0, t / max(a, 1e-3)) * sustain
    rel = np.clip((t - (n / SR - r)) / max(r, 1e-3), 0, 1)
    return e * (1 - rel) ** 2


class Mix:
    def __init__(self):
        self.l = np.zeros(N)
        self.r = np.zeros(N)

    def add(self, t0, sig_l, sig_r=None, gain=1.0):
        i = int(t0 * SR)
        if i >= N:
            return
        sig_r = sig_l if sig_r is None else sig_r
        n = min(len(sig_l), N - i)
        self.l[i:i + n] += gain * sig_l[:n]
        self.r[i:i + n] += gain * sig_r[:n]


def pad(chord, dur, gain=0.11):
    """Soft detuned saws through a low-pass: a warm string-like bed."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out_l, out_r = np.zeros(n), np.zeros(n)
    for k, m in enumerate(chord):
        f = hz(m)
        for det, side in ((-0.06, 0), (0.0, 1), (0.06, 2)):
            ff = f * 2 ** (det / 12)
            ph = RNG.uniform(0, 2 * np.pi)
            saw = 2 * ((ff * t + ph / (2 * np.pi)) % 1.0) - 1
            tri = 2 * np.abs(saw) - 1
            v = 0.55 * saw + 0.45 * tri
            if side != 2:
                out_l += v
            if side != 0:
                out_r += v
    sl = lp(out_l, 1200, 4)
    sr = lp(out_r, 1200, 4)
    mod = 0.85 + 0.15 * np.sin(2 * np.pi * 0.25 * t)
    e = env(n, 0.9, 1.2)
    scale = gain / len(chord) / 2.0
    return sl * e * mod * scale, sr * e * mod * scale


def pluck(m, dur=0.9, gain=0.12, bright=3000):
    """A soft plucked note: a decaying saw through a falling filter."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(m)
    saw = 2 * ((f * t) % 1.0) - 1
    x = lp(saw * np.exp(-t * 6.5), bright, 2)
    return x * env(n, 0.004, 0.25) * gain


def bell(m, dur=2.2, gain=0.10):
    """A clear bell: inharmonic partials with long, staggered decays."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(m)
    x = (np.sin(2 * np.pi * f * t) * np.exp(-t * 2.2)
         + 0.45 * np.sin(2 * np.pi * f * 2.76 * t) * np.exp(-t * 4.0)
         + 0.20 * np.sin(2 * np.pi * f * 5.40 * t) * np.exp(-t * 7.0))
    return x * env(n, 0.002, 0.4) * gain


def sub(m, dur, gain=0.16):
    n = int(dur * SR)
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * hz(m) * t) * env(n, 0.35, 0.9) * gain


def swell(dur=0.9, gain=0.05):
    """A soft rising air sweep into a cut."""
    n = int(dur * SR)
    x = RNG.standard_normal(n)
    out = np.zeros(n)
    # rising band-pass, done in short blocks
    blocks = 24
    for b in range(blocks):
        a, z = b * n // blocks, (b + 1) * n // blocks
        fc = 400 * (2 ** (3.2 * b / blocks))
        sos = butter(2, [fc / 1.4 / (SR / 2), min(fc * 1.4, 18000) / (SR / 2)], "band", output="sos")
        out[a:z] = sosfilt(sos, x[a:z])
    e = np.linspace(0, 1, n) ** 2.2
    e[-int(0.05 * SR):] *= np.linspace(1, 0, int(0.05 * SR))
    return out * e * gain


def kick(gain=0.20):
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 42 + 70 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-t * 11) * gain


def reverb(l, r, seconds=2.4, wet=0.22):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    decay = np.exp(-t * 3.0 / seconds)
    irl = lp(RNG.standard_normal(n), 6000) * decay
    irr = lp(RNG.standard_normal(n), 6000) * decay
    irl /= np.sqrt(np.sum(irl ** 2))
    irr /= np.sqrt(np.sum(irr ** 2))
    wl = fftconvolve(l, irl)[:len(l)]
    wr = fftconvolve(r, irr)[:len(r)]
    return (1 - wet) * l + wet * wl, (1 - wet) * r + wet * wr


def main():
    mx = Mix()
    bounds = CUTS + [DUR]
    for i, ch in enumerate(CHORDS):
        a, b = bounds[i], bounds[i + 1]
        # each pad overlaps the next a little: no gap at the cut
        dur = (b - a) + 1.4 if i < len(CHORDS) - 1 else DUR - a
        pl, pr = pad(ch, dur, gain=0.13 if i < 7 else 0.15)
        mx.add(a, pl, pr)
        mx.add(a, sub(ch[0] - 12 if ch[0] > 45 else ch[0], dur, gain=0.10 if i else 0.06))
        if i > 0:
            mx.add(max(0, a - 0.9), swell(0.9, gain=0.035))
        # a plucked figure from the second scene on: chord tones in eighths (120 BPM)
        if 1 <= i <= 6:
            fig = [ch[2] + 12, ch[3] + 12, ch[4] + 12, ch[3] + 12, ch[5], ch[4] + 12, ch[3] + 12, ch[2] + 12]
            step = 0.25
            k, t = 0, a
            while t < b - 0.05:
                note = fig[k % len(fig)]
                pan = 0.5 + 0.35 * np.sin(k * 1.3)
                p = pluck(note, 0.9, gain=0.075 if i in (3, 4) else 0.06)
                mx.add(t, p * (1.2 - pan), p * (0.2 + pan))
                k += 1
                t += step
            # a quiet pulse under the busier middle
            if i in (3, 4, 5, 6):
                t = a
                while t < b - 0.05:
                    mx.add(t, kick(0.08))
                    t += 1.0
    # bells on the title lines (scene 1) and the end card's words
    for j, t in enumerate((0.35, 0.77, 1.19)):
        mx.add(t, bell([78, 81, 86][j], 2.4, gain=0.07))
    mx.add(2.35, bell(74, 2.0, gain=0.045))
    # the push lands on the phone: a two-note chime
    mx.add(11.0, bell(88, 1.6, gain=0.13))
    mx.add(11.12, bell(93, 1.8, gain=0.11))
    # the end card: a low swell, the mark, the words
    mx.add(27.4, swell(0.6, gain=0.045))
    mx.add(28.0, kick(0.26))
    for j, t in enumerate((28.3, 28.8, 29.2)):
        mx.add(t, bell([81, 85, 90][j], 2.6, gain=0.06))
    l, r = mx.l, mx.r
    l, r = hp(l, 30), hp(r, 30)
    l, r = reverb(l, r)
    # fades and a gentle master curve
    fade_in = np.minimum(1, np.arange(N) / (0.6 * SR))
    fade_out = np.clip((DUR - np.arange(N) / SR) / 2.2, 0, 1) ** 1.5
    l, r = l * fade_in * fade_out, r * fade_in * fade_out
    peak = max(np.max(np.abs(l)), np.max(np.abs(r)), 1e-9)
    l, r = np.tanh(l / peak * 1.1) * 0.89, np.tanh(r / peak * 1.1) * 0.89
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(OUT, SR, (np.stack([l, r], axis=1) * 32767).astype(np.int16))
    print(f"wrote {OUT} ({DUR:.0f} s, peak {max(np.max(np.abs(l)), np.max(np.abs(r))):.2f})")


if __name__ == "__main__":
    main()
