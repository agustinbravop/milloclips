# Reusable Long-Form Animation

These assets are shared by every horizontal video:

- `elmiillor-icon.png`: source icon from the ElmilloR website.
- `fiddlesticks-trailer.mp4`: cached YouTube trailer source.
- `title-card.mp4`: 5-second channel bumper on deep navy: native-color icon at 120% over a broad yellow glow and muted 48-band radial spectrum, settling into a restrained two-cycle 100-105% ambient breath above a stationary wordmark with a soft shadow and two diagonal light passes two seconds apart. The spectrum is rendered at 4x with 192 interpolated points for clean antialiasing; its baseline diameter is 5% smaller than the original ring, with lows on the lower arc and highs at the crown, mirrored left/right.
- `dream-eater-source.*`: Hyleo's `Dream Eater`, a standalone upload of the
  track that sits at 1:20:48 in the creator-safe `Sessions: Star Guardian
  Taliyah` collection. The adjacent JSON records the 0:42.5 title and 0:42
  end-card clip starts; the end card appears when that clip reaches 0:43.
- `end-card.mp4`: 12-second Fiddlesticks outro with official Kick/X icons and
  silent AAC. Its text fades from 0:10 through the end.

The title sting uses `Dream Eater` from 0:42.5 and is not baked into
`title-card.mp4`. `render_long_video` mixes it into the timeline at 30.9375%
gain, 25% below its previous level, with quadratic curves: it fades in over
the last second of the cold open and out over the first second of gameplay.
`title_music_window` derives that window from the cold-open and title durations,
so the two edges cannot drift.

The end-card music is also mixed into the final timeline rather than baked into
the master. It fades in during the last second of gameplay, reaches the original
0:43 sync point when the unchanged 12-second card appears, and fades out during
the card's final second.

`render-long` creates missing masters once and reuses them for every horizontal
video. Do not store card masters inside a VOD workspace.
