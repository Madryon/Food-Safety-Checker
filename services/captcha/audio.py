"""
services/captcha/audio.py
Pure Python audio synthesizer for accessibility alternative.
Generates an audible WAV sequence for 4 digits without any third-party C-bindings.
"""

import io
import math
import struct
import wave
import random

SAMPLE_RATE = 16000

# Base formant frequencies for synthesized vocal/harmonic digit recognition
DIGIT_PROFILES = {
    "0": {"f0": 130, "f1": 300, "f2": 2100, "name": "zero", "mod": 1.0},
    "1": {"f0": 160, "f1": 500, "f2": 1100, "name": "one",  "mod": 1.1},
    "2": {"f0": 180, "f1": 320, "f2": 950,  "name": "two",  "mod": 1.2},
    "3": {"f0": 200, "f1": 290, "f2": 2200, "name": "three","mod": 1.3},
    "4": {"f0": 220, "f1": 520, "f2": 920,  "name": "four", "mod": 1.4},
    "5": {"f0": 240, "f1": 710, "f2": 1200, "name": "five", "mod": 1.5},
    "6": {"f0": 260, "f1": 350, "f2": 1800, "name": "six",  "mod": 1.6},
    "7": {"f0": 280, "f1": 550, "f2": 1750, "name": "seven","mod": 1.7},
    "8": {"f0": 300, "f1": 490, "f2": 1850, "name": "eight","mod": 1.8},
    "9": {"f0": 320, "f1": 680, "f2": 1300, "name": "nine", "mod": 1.9},
}

def synthesize_digit_waveform(digit: str, duration_sec: float = 0.6) -> list:
    """Synthesizes a distinct acoustic waveform for a single digit."""
    prof = DIGIT_PROFILES.get(digit, DIGIT_PROFILES["0"])
    num_samples = int(SAMPLE_RATE * duration_sec)
    samples = []

    f0 = prof["f0"]
    f1 = prof["f1"]
    f2 = prof["f2"]

    for i in range(num_samples):
        t = i / SAMPLE_RATE

        # Trapezoidal / Gaussian envelope to prevent clicks
        fade_in = min(1.0, i / (SAMPLE_RATE * 0.05))
        fade_out = min(1.0, (num_samples - i) / (SAMPLE_RATE * 0.08))
        env = fade_in * fade_out

        # Fundamental + resonant formants
        s_f0 = 0.40 * math.sin(2 * math.pi * f0 * t)
        s_f1 = 0.35 * math.sin(2 * math.pi * f1 * t)
        s_f2 = 0.25 * math.sin(2 * math.pi * f2 * t)

        # Subtle harmonic overtone for timbre differentiation
        s_h3 = 0.15 * math.sin(2 * math.pi * (f0 * 3) * t)

        sample_val = (s_f0 + s_f1 + s_f2 + s_h3) * env

        # Clamping to 16-bit integer
        int_val = int(max(-32767, min(32767, sample_val * 32767 * 0.65)))
        samples.append(int_val)

    # 0.25 second silence pause after digit
    pause_samples = int(SAMPLE_RATE * 0.25)
    samples.extend([0] * pause_samples)
    return samples

def generate_chime_intro() -> list:
    """Synthesizes a soft intro chime before digits begin."""
    chime_samples = int(SAMPLE_RATE * 0.35)
    samples = []
    freq = 587.33  # D5 musical note
    for i in range(chime_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 8.0)
        s = 0.4 * math.sin(2 * math.pi * freq * t) * env
        samples.append(int(s * 32767))
    # brief pause
    samples.extend([0] * int(SAMPLE_RATE * 0.2))
    return samples

def generate_captcha_audio_wav(digits: str) -> bytes:
    """Generates complete WAV file bytes for a 4-digit code."""
    all_samples = generate_chime_intro()

    for d in str(digits):
        if d in DIGIT_PROFILES:
            all_samples.extend(synthesize_digit_waveform(d))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)       # Mono
        wf.setsampwidth(2)      # 16-bit PCM
        wf.setframerate(SAMPLE_RATE)
        # Pack samples to signed 16-bit little-endian
        raw_bytes = struct.pack(f"<{len(all_samples)}h", *all_samples)
        wf.writeframes(raw_bytes)

    buf.seek(0)
    return buf.getvalue()
