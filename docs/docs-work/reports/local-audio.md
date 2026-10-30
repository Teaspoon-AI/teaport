# local-audio

## Changed files
- src/content/docs/guides/local-audio.md (new)

## What changed
- New task page for the local-audio audio bridge: hardware (built for the ReSpeaker XVF3800; any 16 kHz stereo card works), enabling through TEAPORT_ENABLE_LOCAL_AUDIO=1 (the installer seeds the env file and enables the unit; TEAPORT_LOCAL_AUDIO_DEVICE overrides the card) and disabling through systemd, the sleeping microphone with its wake threshold and pre-roll audio, one-conversation arbitration with backoff, wake words as an asleep session with exact matching and the go-to-sleep phrases, the phone-call LED ring, and echo handling.
- No OLED section, per the packet.

## Removed content
None.

## Open questions
None. Every statement cites the pinned commit.

## Lint
0 error(s), 0 warning(s) in 1 file(s). None kept.
