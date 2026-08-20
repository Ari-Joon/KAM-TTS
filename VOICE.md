# The voice in this repository

`server/voice_samples/` holds eight recordings of my own voice. They ship with
the project on purpose, so that a fresh clone can be heard speaking straight
away rather than making you record sixteen passages before you know whether you
like it. XTTS-v2 clones from them at startup, so anything KAM TTS reads aloud on
a fresh install is read in my voice.

**The code is MIT. The recordings are not.** They are two different things in
one repository and the licence in `LICENSE` covers only the first.

## What you may do with them

- Run KAM TTS and listen to it, for as long as you like.
- Evaluate the project, develop against it, and use my voice while you do.
- Keep them in a fork, since that is how forking works.

## What you may not do with them

- Use my voice in anything you publish, ship, sell or monetise.
- Redistribute the recordings, or a model or embedding derived from them, as a
  voice in its own right.
- Use them to make it sound as though I said something I did not say, or to
  represent yourself or anyone else as me.
- Use them to get past voice authentication anywhere.

That is a statement of what I intend rather than a piece of technology. Anyone
determined to ignore it can, which is true of every voice sample published
anywhere. I would rather say plainly what the recordings are for than pretend
the question does not arise.

## Using your own voice instead

This is the point of the project, and it takes about fifteen minutes.

1. Open the dashboard and press **● Record** in the top bar.
2. Press **📜 Recording passages** and read each one as its own clip. They exist
   to cover a spread of prosody, so read them the way you would actually speak
   rather than performing them.
3. Press **+ New voice**, give it a name, and record into that instead if you
   want to keep mine around for comparison.
4. Switch with **Use**. Each voice learns independently, so tuning and quality
   history never bleed between them.

To remove my voice from your copy entirely, delete `server/voice_samples/*.wav`
and `server/voice_latents.pt`. The server will print instructions on the next
start rather than failing, since a fresh install with no clips is the expected
first run and not an error.
