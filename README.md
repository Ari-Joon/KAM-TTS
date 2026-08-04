# KAM TTS

A voice-cloned text-to-speech Chrome (MV3) extension paired with a local
Python/Flask server. It reads web pages aloud using XTTS-v2, with a Whisper-based
quality-learning loop that adapts synthesis parameters per chunk fingerprint
over time.

## Features

- **Voice cloning from your own recordings** — 8–12 short clips, averaged by
  XTTS-v2 into a personal voice. Multiple named voice profiles, switchable live
  from the dashboard, each with fully isolated learning.
- **Reads real pages properly** — equations (`X_{t+1}` → "X sub t plus 1"),
  LaTeX, acronyms, numbers, code tokens; in-page highlight bar tracks the
  spoken chunk, maths included.
- **A closed learning loop** — Whisper listens back to every chunk, scores it,
  and KAM tunes its own synthesis parameters per sentence *fingerprint*
  (length, punctuation, wording, complexity). Thumbs and reports feed the same
  system; the AI page shows quality trends and whether your reports worked.
  **Everything learned is stored per fingerprint, never by moving your sliders**,
  so your Speech Tuning settings stay yours and learning layers on top of them.
- **Runs on whatever you have** — NVIDIA (CUDA), AMD (ROCm), Apple Silicon
  (Metal), Intel Arc (XPU) or plain CPU. KAM detects which backends are present,
  picks the best one, proves it can actually run before committing to it, and
  falls back cleanly if it can't.
- **Hardware-adaptive** — measures real synthesis speed on first boot, then
  adapts buffering, standby and listen-back to suit anything from a CPU-only
  laptop to a high-end GPU. The measurement streams to the dashboard console
  while it runs.
- **Guards its own output** — every synthesised chunk gets checked for the ways
  XTTS fails, meaning runaway babble, a looped fragment or a sentence cut off
  mid-word, and is re-synthesised at a steadier temperature rather than played.
- **Local and private** — everything runs on your machine; per-install API
  token, localhost-only server, nothing leaves your computer.

> **Licensing / commercial use.** KAM TTS builds on XTTS-v2. Verify the licence
> of the exact model weights you use before any commercial use — some XTTS-v2
> releases are non-commercial. Whisper, spaCy, PyTorch and Flask are permissively
> licensed. Voice cloning also carries consent obligations: only clone voices you
> have the right to use.

---

## Hardware

KAM runs on whatever compute your machine has. On startup it detects every
backend PyTorch can see, picks the best one, and then verifies it with a real
operation before loading the model. I added that last step because a backend can
report itself as available and still fail on the first kernel, which both MPS and
fresh ROCm installs do fairly often, so it drops back to CPU immediately rather
than breaking halfway through a page.

| Backend | Hardware | Notes |
|---------|----------|-------|
| CUDA | NVIDIA | Fastest. TF32 is enabled automatically on Ampere and newer. |
| ROCm | AMD, Linux | PyTorch reports it as "cuda", so KAM tells them apart and skips the NVIDIA-only tuning. |
| MPS | Apple Silicon | fp32 with a CPU fallback for the operations Metal is missing. Usable, but slower than CUDA. |
| XPU | Intel Arc | Needs the Intel XPU build of PyTorch. |
| CPU | anything | Always works. The thread count is capped so listen-back doesn't starve synthesis. |

You can force a choice with the `KAM_DEVICE` environment variable (`cpu`,
`cuda`, `mps`, `xpu`), which is useful if you want to keep a GPU free or work
around a driver that's misbehaving.

### Speed

**The server measures itself.** The first time it boots on a given machine it
times the real synthesis path, prints the results to the dashboard console as
they happen, and adapts its buffering to them. You don't have to run anything.

To measure it by hand, or before installing the extension:

```bash
python hardware_profile.py
```

Both report the **real-time factor (RTF)**, which is seconds of compute per
second of audio. They share the same code so the numbers agree.

| RTF | Meaning |
|-----|---------|
| < 0.5 | Synthesis comfortably outpaces playback |
| 0.5–1.0 | Keeps ahead, fine for continuous reading |
| 1.0–2.0 | Slower than playback, so brief buffering |
| > 2.0 | Reading stalls often, a GPU is strongly recommended |

Useful flags: `--no-test` reports the machine without timing anything, and
`--device cpu` measures a specific backend. Set `KAM_NO_BENCHMARK=1` if you don't
want the server measuring itself on first boot.

**Minimum:** 4-core CPU, 8 GB RAM. CPU-only inference works but is several times
slower than playback, so it's fine for short passages and not for continuous
reading. KAM detects this and buffers more deeply, and samples listen-back rather
than running it on every chunk, so the two don't fight over the same cores.

**Recommended:** NVIDIA GPU with 6 GB or more of VRAM and CUDA, plus 16 GB RAM.
That gives real-time or faster synthesis.

Under 6 GB of VRAM works but leaves little headroom, so use a shorter idle
standby timeout (Speech Tuning, then Idle standby) and the model will release
VRAM when it isn't being used.

---

## Setup

**One command** (recommended). From the `server/` folder, using the Python you
intend to run KAM with:

```
python setup_kam.py
```

This checks your hardware before downloading anything, installs dependencies,
fetches the spaCy model, registers the Chrome native-messaging host, checks for
voice reference audio, and offers to profile synthesis speed. It is idempotent —
safe to re-run.

You must still **install PyTorch for your hardware first** — it is the one
dependency that must match your machine. Get the correct command from
[pytorch.org](https://pytorch.org): the CUDA build for NVIDIA, the ROCm build
for AMD on Linux, the default macOS wheel for Apple Silicon, the XPU build for
Intel Arc, or the CPU build for anything else. `requirements.txt` deliberately
leaves torch unpinned for this reason. KAM adapts to whichever you installed.

<details>
<summary>Manual setup (if you prefer to run the steps yourself)</summary>

1. Install PyTorch for your hardware from [pytorch.org](https://pytorch.org).
2. Install the rest:
   ```
   pip install -r requirements.txt
   python -m spacy download en_core_web_sm
   ```
3. **Add voice reference clips.** Record the **16 standard passages** (shown in
   the dashboard under 🎤 → *Recording passages*) as separate WAV files, 8–15
   seconds each, clean speech, no processing, into `server/voice_samples/`.
   XTTS averages them into a single speaker embedding — separate clips clone
   better than one long file. A single `server/my_voice.wav` also works.
4. **Check the clips**:
   ```
   python check_voice_clips.py
   ```
5. **Register the native-messaging host**:
   ```
   python register_host.py
   ```
</details>

Then **load the extension**: `chrome://extensions` → Developer mode → Load
unpacked → select the `extension/` folder. Start the server from the dashboard
power button, or `python server.py`.

---

## Recording good reference clips

Clone quality is set almost entirely by the reference audio, so this matters
more than any parameter:

- **The 16 standard passages** (12–20 clips is the sweet spot) (dashboard → 🎤 → *Recording passages*) cover
  declaratives, questions, numbers, long clauses, lists, warmth, explanation,
  closings, exclamations, quoted speech, asides and fragments — the full
  prosodic range KAM reads daily.
- **8–15 seconds per clip.** More clips is not automatically better — the encoder averages
  them, so one poor clip drags the result down.
- **Quiet room, consistent mic distance** (~15–20 cm), no fans or traffic.
- **No processing** — no noise gates, compression, EQ or "enhancement".
- **Don't clip.** Slightly quiet beats peaking.
- **Read the way you want KAM to read to you.** The clone mirrors your delivery,
  not just your timbre.
- **Vary structure**: statements, a question, a list, a longer sentence with
  clauses, and something with numbers or an acronym.

Optionally add `voice_samples/transcripts.txt` (one line per clip). On startup
the server analyses their prosody and logs which structures your reference audio
actually covers.

### Clips are screened automatically

XTTS averages **every** clip in a folder into one speaker embedding, so a single
clipped, near-silent or noisy recording drags the whole voice down and there's no
way to hear which one did it.

So KAM measures each clip before computing latents and **excludes the unusable
ones**, naming them and saying why in the console. Since the clips are averaged,
dropping a bad one can only improve things. It never rejects all of them, and if
nothing passes it uses everything and prints the reasons instead.

You can check any profile without switching to it:

```bash
python check_voice_clips.py voices/my-voice
```

Same measurements and same thresholds as the server, so this tells you in advance
exactly what the server is going to do.

## Speech quality

Two things keep quality even across voice profiles, rather than it depending on
how carefully a given voice happened to be recorded:

- **The clip gate above**, so a profile recorded later on a laptop mic is held
  to the same standard as the first one.
- **Output validation.** XTTS is autoregressive and sometimes fails to emit a
  stop token. It doesn't raise an error when that happens, it just returns
  audio, and the audio is babble or a repeated fragment or a sentence cut off
  mid-word. So KAM checks the waveform against the text it was meant to speak,
  and if it fails it re-synthesises at a lower temperature and a higher
  repetition penalty instead of playing it. Two retries, since sampling failures
  are random and a colder re-roll almost always works.

`GET /quality/rejections` reports how many were caught and how they failed, which
gives you the hallucination rate as a number rather than an impression. `GET
/diagnose/<chunk_id>` explains any single chunk: how it was labelled, which
parameters were used and where they came from, what it cost, how it scored, and
which learned rules rewrote it.

Rejected attempts are also kept as **evidence**. A chunk that fails at
temperature 0.45 and then succeeds at 0.29 is a controlled comparison, since the
text is identical, one variable changed, and I know the outcome on both sides.
It's the only causal data the system produces. Ordinary reading never moves the
sampling parameters, so without it the self-tuner has almost nothing to work out
a direction from.

## How learning is stored

Two rules keep the loop honest and reversible:

- **Learning never writes your settings.** Everything KAM works out lives in
  `good_settings.json`, keyed by chunk fingerprint, and gets applied *on top of*
  your live values at synthesis time. The Speech Tuning sliders stay yours.
- **Reinforcement uses what the chunk was actually made with.** A thumbs-up
  reinforces the parameters stored on that chunk's row rather than whatever the
  sliders read when you clicked, because a read spans many chunks and those two
  are often different.

You can see both per chunk through `/diagnose/<chunk_id>`, which reports where
each parameter came from: either a value learned for that fingerprint, or your
own defaults.

---

## Voice profiles

Create additional voices from the dashboard: **🎤 → ＋ New voice** creates a
folder under `server/voices/<name>/`, opens it, and shows the 16 recording
passages. Record them, clean them (`python clean_voice_clips.py --in <raw>
--out <folder>`), press **Use**. Switching is instant once a voice's latents
are cached, and **each voice learns independently** — tuning, quality history
and baselines never bleed between voices. Only word pronunciations are shared.

---

## Security

- The local API token is **generated per install** on first server run and stored
  in `server/kam_token.txt` (gitignored). The extension fetches it from the
  server's `/token` endpoint — no shared secret ships in source.
- Chrome gives an unpacked extension a **different ID on every machine**, so the
  server reads yours from the native-host manifest that `register_host.py`
  writes. Running that one setup step is what tells both the power button and
  the server which extension to trust.
- The server binds to `127.0.0.1` only; CORS restricts callers to the extension
  origin.
- Override with the `KAM_TOKEN` or `KAM_EXTENSION_ID` environment variables for
  custom setups.

---

## Environment variables

All optional — KAM works with none of them set.

| Variable | Effect |
|----------|--------|
| `KAM_DEVICE` | Force a backend: `cpu`, `cuda`, `mps`, `xpu`. Ignored (with a log line) if that backend isn't present. |
| `KAM_CPU_THREADS` | Override the CPU thread cap used for inference. |
| `KAM_NO_BENCHMARK` | Set to `1` to skip the first-boot speed measurement. |
| `KAM_TOKEN` | Use a fixed API token instead of the per-install generated one. |
| `KAM_EXTENSION_ID` | Override which extension origin is allowed through CORS. Accepts several IDs separated by commas. Not normally needed, since the server reads the ID from the native-host manifest that `register_host.py` writes. |
| `KAM_PYTHON` | Python interpreter the native host launches the server with. |
| `KAM_SERVER_PY` | Path to `server.py` if it isn't next to `kam_host.py`. |
| `KAM_FRESH_LATENTS` | Set to `1` to recompute voice latents instead of using the cache. |

---

## Troubleshooting

**The dashboard opens but nothing loads, or every request fails.** That is
almost always the extension ID. Chrome gives an unpacked extension a different
ID on each machine, and the server only accepts requests from the one it knows
about. Find yours at `chrome://extensions` with Developer mode on, then run
`python register_host.py <YOUR_EXTENSION_ID>` and restart the server. It prints
which origin it trusts on every boot, so check the first few console lines.

**It says "No GPU in use" but I have one.** The console prints which backends it
found and why one was rejected. Usually it's a PyTorch build that doesn't match
the hardware, like a CPU wheel on an NVIDIA machine, so reinstall torch from
[pytorch.org](https://pytorch.org) for your setup.

**A GPU was detected and then dropped to CPU.** The backend passed detection and
then failed a real operation, and the console gives you the error. This is common
on Apple Silicon with older torch versions and on partial ROCm installs. Use
`KAM_DEVICE` to force it if you think it's wrong.

**Reading stalls and buffers.** Check the measured RTF on the dashboard. Anything
above 1.0 means synthesis is slower than playback on this machine, which is
expected on CPU.

**A voice sounds worse than the default one.** Run `python check_voice_clips.py
voices/<name>`. It's almost always the clips, and the report names the specific
problem in each file.

**Chunks occasionally sound garbled.** `GET /quality/rejections` shows how many
were caught and re-synthesised. If the count is high, lower the temperature in
Speech Tuning, since a high sampling temperature is what drives these failures.

---

## What is not committed

Runtime state and personal data are gitignored: the token file, the SQLite
quality database, learned settings and baselines, your voice clips, cached
latents, logs, and the generated native-host manifest. All are created per
install.
