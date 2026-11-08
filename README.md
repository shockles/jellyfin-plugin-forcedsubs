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
- Movies that already have a forced subtitle (an embedded forced track or a
  `.forced` file) are skipped unless you tick "Redo".
- If a movie has no foreign dialogue, no file is written.

## Things to know
- **The service and Jellyfin must see the same file paths.** If your library is
  on a network share, use UNC paths (`\\NAS\Movies`) rather than mapped drive
  letters, and make sure the Windows user running the service can read the
  share and write to it.
- The service only listens on 127.0.0.1, so it can't be reached from other PCs.
- The model loads when a job starts and unloads after 60 seconds of no work, so
  the GPU goes back to idle. Change this with `--idle-unload` in `start.ps1`.
- Logs: `service\data\service.log`. A record of what has been processed:
  `service\data\history.json`. Delete an entry there to have the library task
  check that movie again.
- **Limits:** made-up languages (Elvish, Klingon) can't be translated. Very
  short exclamations ("Da!") may be missed, because the tool needs more
  confidence on short lines. Movies with foreign subtitles burned into the
  picture will show both.

## Test it without Jellyfin
```powershell
cd service
.\.venv\Scripts\python.exe -m forced_subs.cli "D:\Movies\Some Movie (2020)\Some Movie.mkv" --overwrite
```

## Releasing a new version
Bump nothing by hand. Push an annotated tag and GitHub Actions does the rest:

```powershell
git tag -a v0.2.0 -m "What changed in this version"
git push origin v0.2.0
```

The workflow builds the plugin, attaches the plugin and service zips to a GitHub
release, and adds the version (with its checksum) to `manifest.json` on `main`.
The tag message becomes the changelog Jellyfin shows.

## License
GPL-3.0, like Jellyfin itself.

## Tuning
The settings at the top of `service\forced_subs\pipeline.py` (`Settings` class):
- `foreign_min_prob` (0.60): raise it if English lines get flagged as foreign;
  lower it if foreign lines are missed.
- `short_line_min_prob` (0.85): the same, for lines under 1.2 seconds.
- `refine_below` (0.90): windows where English confidence is below this get
  checked line by line. Raising it catches more but runs slower.
