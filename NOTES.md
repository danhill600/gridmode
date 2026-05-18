# Gridmode Notes

## High-resolution Now Playing covers

The grid cover cache is optimized for thumbnail browsing, so cached images can
look soft when Now Playing/Info displays covers at large sizes.

Future work: add a separate high-resolution display-cover cache for
Now Playing/Info, probably 800-1200px. Prefer MusicBrainz/Cover Art Archive
1200px or original images when available, then fall back to Last.fm, then
upscale only when no better source exists.
