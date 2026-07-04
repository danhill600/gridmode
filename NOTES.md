# Gridmode Notes

## High-resolution Now Playing covers

The grid cover cache is optimized for thumbnail browsing, so cached images can
look soft when Now Playing/Info displays covers at large sizes.

Future work: add a separate high-resolution display-cover cache for
Now Playing/Info, probably 800-1200px. Prefer MusicBrainz/Cover Art Archive
1200px or original images when available, then fall back to Last.fm, then
upscale only when no better source exists.

## Interaction model: daily keys vs Tools

Top-level keys should be reserved for frequent browsing/listening actions.
`t` is intentionally global for adding the current song to `sick_tunes`; that is
a clutch listening action and should work everywhere.

Maintenance, diagnostics, slow global repair jobs, and rarely used operations
belong behind the command prompt and Tools tab, not on single-letter global
bindings.

Current direction:

- `r` means "refresh this tab." It reloads/rebuilds the active tab, reconciles
  identity, checks the cover cache, and offers targeted hydration for missing
  covers in that tab.
- `u` is not a public binding. Targeted cover hydration is available through
  `r`, `:hydrate`, and Tools.
- `:` opens a command prompt for operations that do not deserve a top-level key.
  Useful commands should include aliases, e.g. `tools`, `refresh`, `hydrate`,
  and `sick-tunes`.
- Tools is a transient tab for maintenance. Navigate with `j/k`, jump sections
  with `J/K`, and run with Enter. Planned/destructive/global actions should be
  listed there before receiving top-level bindings.

Cover workflow split:

- Normal update path: targeted, current-view, user-facing. This is for "I added
  albums; show me covers now." It should not be blocked by the global
  `failures.json` backlog.
- Deep cover repair: global, slow, failure-aware, maintenance-oriented. This is
  for retrying old exotic/tough missing covers and should live in Tools/CLI, not
  in the normal refresh path.
