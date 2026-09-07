# Third-party notices

KAM TTS itself is MIT licensed (see [LICENSE](LICENSE)), except the voice
recordings, which are not (see [VOICE.md](VOICE.md)).

Scanning bundles the following open-source libraries in `extension/lib/`,
unmodified, under their own licences. They are vendored rather than fetched from
a CDN so that scanning works with the network unplugged, which is the same reason
the rest of the project runs locally.

| Library | Used for | Licence |
|---|---|---|
| [Tesseract.js](https://github.com/naptha/tesseract.js) | reading text off a photographed page, in the browser as WebAssembly | Apache-2.0 |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) core and `eng.traineddata` | the recogniser itself and its English data | Apache-2.0 |
| [qrcode.js](https://github.com/davidshimjs/qrcodejs) (David Shim) | the pairing code the phone's camera reads | MIT |

`extension/scan-core.js` is my own image code, carried over unchanged from
[KAM PDFs](https://github.com/Ari-Joon/KAM-PDFs), which is also MIT. It finds the
edges of a page in a photograph, corrects the perspective and cleans it up.

The XTTS-v2 model weights are not bundled and are licensed separately (Coqui
Public Model License, non-commercial). Check the licence of the exact weights you
download before any commercial use.
