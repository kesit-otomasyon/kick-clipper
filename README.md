# kick-clipper

Watches live Kick streams of the streamers in `config.yaml` (only streamers who allowed it) and turns
their loudest moments into vertical YouTube Shorts while the stream is still on, crediting the streamer.

- `clipper/watch.py` records the stream on the GitHub runner in 5 s pieces (last ~6 min only), finds
  loudness bursts, cuts 45 s around them, writes Turkish captions (faster-whisper), a hook and a title
  (Gemini/Groq), checks the moment is safe, and uploads it scheduled 15 min ahead.
- Daily limits per streamer and per channel; moments already uploaded are skipped.
- `.github/workflows/clip.yml` checks every 15 minutes who is live; a long stream continues in a new run.

Secrets: `YT_TOKEN_CLIPS`, `GEMINI_API_KEY`, `GROQ_API_KEY`.
Font: Anton (SIL Open Font License, `assets/fonts/OFL-Anton.txt`). Face detector: OpenCV YuNet (MIT).
