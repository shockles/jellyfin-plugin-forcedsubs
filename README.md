# Forced Subtitles for Jellyfin

Finds the foreign-language lines in a mostly English movie (a few lines of
Russian, Japanese, and so on), translates only those lines to English with
Whisper large-v3, and writes `Movie.en.forced.srt` next to the movie. Jellyfin
shows forced subtitles automatically when English audio is playing.

There are two parts:

| Folder | What it is |
|---|---|
| `service/` | A small Python service that does the transcription and translation on the GPU. Listens on `http://127.0.0.1:8796`. |
| `plugin/` | The Jellyfin plugin (Jellyfin 12.4, .NET 10). It adds a dashboard page with a **Generate** button and a library-wide scheduled task. |

## Install on the server PC

### 1. Requirements
- NVIDIA driver (an RTX 4060 is fine; the model uses about 3 GB of VRAM)
- Python 3.10–3.13 from python.org
- ffmpeg on PATH: `winget install Gyan.FFmpeg`

### 2. Service
Download `forced-subs-service_<version>.zip` from the
[latest release](https://github.com/shockles/jellyfin-plugin-forcedsubs/releases/latest)
and unzip it on the server (for example to `C:\ForcedSubs\service`). Then, in PowerShell:

```powershell
cd C:\ForcedSubs\service
powershell -ExecutionPolicy Bypass -File .\install.ps1 -AutoStart
```

This installs everything, downloads the Whisper large-v3 model (~3 GB), checks
that it runs on the GPU, and registers a scheduled task that starts the service
when you log in. To run it by hand instead, use `.\start.ps1`. Check that it's up
by opening http://127.0.0.1:8796/health in a browser.

### 3. Plugin
1. In Jellyfin, go to **Dashboard > Plugins > Repositories**, press **+**, and add:
   - Name: `Forced Subtitles`
   - URL: `https://raw.githubusercontent.com/shockles/jellyfin-plugin-forcedsubs/main/manifest.json`
2. Go to **Catalog**, find **Forced Subtitles (Whisper)** under Subtitles, and install it.
3. Restart Jellyfin, then open **Dashboard > Plugins > Forced Subtitles (Whisper)**.
   The top of the page should say "Service connected".

Jellyfin will offer updates when new versions are released.

<details><summary>Manual install instead</summary>

Unzip `forced-subs-plugin_<version>.zip` from the latest release into a new folder
named `Forced Subtitles_<version>` inside Jellyfin's plugins folder (usually
`C:\ProgramData\Jellyfin\Server\plugins\`), then restart Jellyfin.
</details>

## Use it
- **One movie:** on the plugin page, search for the title and press **Generate**.
  Progress shows under *Recent jobs*. When it finishes, Jellyfin rescans the
  movie and the subtitle appears as "English (Forced)".
- **Whole library:** Dashboard > Scheduled Tasks > **Generate forced subtitles**.
  It processes one movie at a time and skips anything it has already checked.
  It has no schedule by default; you can add one there.
- **New movies automatically:** turn on "Automatically process new items" in the
  plugin settings. New items are queued a set number of minutes after they're added.
- Running jobs can be cancelled from *Recent jobs*. **Forget** on a search result
  (or "Forget all checked items" in settings) makes the library task check items again.
- Movies that already have a forced subtitle (an embedded forced track or a
  `.forced` file) are skipped unless you tick "Redo".
- If a movie has no foreign dialogue, no file is written.

## Settings
Everything is on the plugin page and applies to the next job; the service doesn't
need a restart.

| Section | Options |
|---|---|
| Detection | Sensitivity preset (low / normal / high / custom), main language, languages you don't need subtitled, only-these-languages |
| Existing subtitles & audio | Skip items with an embedded forced track, skip items with a `.forced` file, preferred audio languages, skip items without them, ignore commentary tracks |
| Subtitle output | `en` or `eng` file tag, also mark as default, "[Russian]" language labels, characters per line, lines per subtitle, min/max seconds on screen, extra phrases to drop |
| Library task & automation | Auto-process new items (with a delay), movies and/or episodes, which libraries, only items added in the last N days, minimum runtime, max items per run, retry failures, forget all |
| Service connection | Address, API key, request timeout, path mappings |
| Model & GPU | Whisper model, GPU or CPU, precision, beam size, idle seconds before the model is unloaded |
| Advanced detection tuning | Confidence thresholds, short-line rules, speech detection threshold, window and batch sizes, translation chunking |

## Things to know
- **The service and Jellyfin must see the same files.** If your library is on a
  network share, use UNC paths (`\\NAS\Movies`) rather than mapped drive letters,
  and make sure the Windows user running the service can read the share and
  write to it. If the service sees the files at different paths (another PC,
  Docker), add **path mappings** in the plugin settings, e.g.
  `D:\Movies => /media/movies`.
- By default the service only listens on 127.0.0.1. To run it on another
  machine, start it with `--host 0.0.0.0 --api-key <secret>` and put the same key
  in the plugin's API key setting.
- The model loads when a job starts and unloads after 60 seconds of no work (set
  in the plugin), so the GPU goes back to idle.
- Logs: `service\data\service.log`. A record of what has been processed:
  `service\data\history.json`.
- **Limits:** made-up languages (Elvish, Klingon) can't be translated. Very
  short exclamations ("Da!") may be missed, because the tool needs more
  confidence on short lines. Movies with foreign subtitles burned into the
  picture will show both.

## Test it without Jellyfin
```powershell
cd service
.\.venv\Scripts\python.exe -m forced_subs.cli "D:\Movies\Some Movie (2020)\Some Movie.mkv" --overwrite
```
Any setting can be overridden with `-o`, e.g. `-o skip_languages=es,fr -o language_prefix=true`.

## Releasing a new version
No version numbers to edit. Push an annotated tag and GitHub Actions does the rest:

```powershell
git tag -a v0.2.0 -m "What changed in this version"
git push origin v0.2.0
```

The workflow builds the plugin, attaches the plugin and service zips to a GitHub
release, and adds the version (with its checksum) to `manifest.json` on `main`.
The tag message becomes the changelog Jellyfin shows.

## License
GPL-3.0, like Jellyfin itself.
