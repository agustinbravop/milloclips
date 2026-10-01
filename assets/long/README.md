# Reusable Long-Form Animation

These assets are shared by every horizontal video:

- `elmiillor-icon.png`: source icon from the ElmilloR website.
- `fiddlesticks-trailer.mp4`: cached YouTube trailer source.
- `title-card.mp4`: channel bumper on deep navy, seven seconds by default and rendered to the edit's requested duration: centered native-color icon with a 100–108% idle breath, broad yellow glow, and bright 48-band radial spectrum. The ring expands up to 108 pixels on musical peaks, rendered at 4x with 192 interpolated points, with lows on the lower arc and highs at the crown, mirrored left/right. Each video frame uses its matching audio window without temporal smoothing; analysis starts at the music clip start plus its one-second pre-card lead (0:43.5). The one-second smoothstep entrance is reversed for the exit, and matching half-second dissolves blend both edges into surrounding footage.
- `dream-eater-source.*`: Hyleo's `Dream Eater`, a standalone upload of the
  track that sits at 1:20:48 in the creator-safe `Sessions: Star Guardian
  Taliyah` collection. The adjacent JSON records the 0:42.5 title and 0:42
  end-card clip starts; the end card appears when that clip reaches 0:43.
- `end-card.mp4`: 12-second Fiddlesticks outro with official Kick/X icons and
  silent AAC. Its text fades from 0:10 through the end.

The title sting uses `Dream Eater` from 0:42.5 and is not baked into
`title-card.mp4`. `render_long_video` mixes it into the timeline at 7.734375%
gain with quadratic curves: it fades in from one second before the card through
the card's first second, and out over the first second of gameplay.
`title_music_window` derives that window from the cold-open and title durations,
so the two edges cannot drift.
The completed video fades in from black over its first second with cubic ease-out
(`1 - (1 - t)^3`): fast at the beginning, slow at the end.

The end-card music is also mixed into the final timeline rather than baked into
the master. A half-second cross-dissolve blends the final gameplay scene into the
end card, matching the cold-open-to-title transition. Its music starts one second
before that dissolve, reaches the original
0:43 sync point when the unchanged 12-second card appears, and fades out during
the card's final second.

`render-long` creates missing masters once and reuses them for every horizontal
video. Do not store card masters inside a VOD workspace.
