# Gridmode Lifeboat Android

This is an adjunct Android client for a phone-side music directory.
The MVP scans a user-selected folder, treats each direct child directory as an
album, displays album covers in a grid, and starts album playback on tap.

Expected phone layout:

```text
Music/
  Artist - Album/
    cover.png
    01 Track.mp3
    02 Track.mp3
```

Cover lookup currently prefers `cover.png`, `cover.jpg`, `cover.jpeg`,
`folder.jpg`, and `folder.png`, then falls back to the first image file found in
the album directory.

Audio files currently match Gridmode's existing phone extensions: `mp3`, `flac`,
`m4a`, `ogg`, `opus`, `wav`, `aiff`, and `aif`.

## Build

Open `android/` in Android Studio, or build from this directory with a Gradle
install that is compatible with Android Gradle Plugin 9.2:

```sh
gradle :app:assembleDebug
```

On first launch, pick the phone's music directory with the system folder picker.
The app persists read access to that tree.
