# Forced Subtitles for Jellyfin

Makes subtitles for your movies on your own GPU, with no cloud service:

- **Forced subtitles:** only the foreign-language lines in a mostly English movie
  (a few lines of Russian, Japanese, and so on), e.g. `Movie.en.forced.srt`.
  Jellyfin shows these automatically when English audio is playing.
- **Full subtitles:** every line of dialogue, e.g. `Movie.en.srt`.
- **In any of 97 languages:** pick as many as you like, e.g. English, Spanish
  and Japanese gives `Movie.en.srt`, `Movie.es.srt`, `Movie.ja.srt` and the
  matching forced files.

English comes straight from Whisper large-v3, including its translation of the
foreign lines. Other languages are translated from that English with Meta's
NLLB-200 model, which adds roughly a minute per language for a full movie.
A Spanish forced subtitle leaves out lines that are already spoken in Spanish.

There are two parts:

| Folder | What it is |
|---|---|
| `service/` | A small Python service that does the transcription and translation on the GPU. Listens on `http://127.0.0.1:8796`. |
| `plugin/` | The Jellyfin plugin (Jellyfin 12.4, .NET 10). It adds a dashboard page with a **Generate** button and a library-wide scheduled task. |

## Install on the server PC

### Easiest: one click
1. Download **`Install-ForcedSubs.bat`** from the
   [latest release](https://github.com/shockles/jellyfin-plugin-forcedsubs/releases/latest)
   onto the PC that runs Jellyfin, and double-click it.
2. It installs Python and ffmpeg if they're missing, downloads the service into
   `C:\ForcedSubs`, downloads the models (~6.5 GB the first time), makes the
   service start when you log in, and checks that it's running.
3. Add the plugin in Jellyfin (step 3 below).

Run it again any time to update the service to the latest release.

### Or step by step

#### 1. Requirements
- NVIDIA driver (an RTX 4060 is fine; the model uses about 3 GB of VRAM)
- Python 3.10â€“3.13 from python.org
- ffmpeg on PATH: `winget install Gyan.FFmpeg`

#### 2. Service
Download `forced-subs-service_<version>.zip` from the
[latest release](https://github.com/shockles/jellyfin-plugin-forcedsubs/releases/latest)
and unzip it on the server (for example to `C:\ForcedSubs\service`). Then, in PowerShell:

```powershell
cd C:\ForcedSubs\service
powershell -ExecutionPolicy Bypass -File .\install.ps1 -AutoStart
```

This installs everything, downloads the Whisper large-v3 model (~3 GB) and the
NLLB-200 3.3B translation model (~3.3 GB; add `-TranslationModel none` to skip
it if you only want English), checks that it runs on the GPU, and registers a
scheduled task that starts the service when you log in. To run it by hand instead, use `.\start.ps1`. Check that it's up
by opening http://127.0.0.1:8796/health in a browser.

#### 3. Plugin
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
| What to make | Forced subtitles, full subtitles, and which of the 97 subtitle languages (searchable list) |
| Detection | Sensitivity preset (low / normal / high / custom), main language, languages you don't need subtitled, only-these-languages |
| Existing subtitles & audio | Skip items with an embedded forced track, skip items with a `.forced` file, don't make full subtitles in a language the item already has, preferred audio languages, skip items without them, ignore commentary tracks |
| Subtitle output | Two- or three-letter language tag in file names, mark forced as default, "[Russian]" language labels, characters per line, lines per subtitle, min/max seconds on screen, extra phrases to drop |
| Library task & automation | Auto-process new items (with a delay), movies and/or episodes, which libraries, only items added in the last N days, minimum runtime, max items per run, retry failures, forget all |
| Service connection | Address, API key, request timeout, path mappings |
| Model & GPU | Whisper model, GPU or CPU, precision, translation model (NLLB-200 3.3B or 1.3B), beam sizes, full-subtitle batch size, idle seconds before models are unloaded |
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
- The library task redoes an item when you change what to make (add a language,
  turn on full subtitles). Files that already exist are kept; only the missing
  ones are written.
- **Translation quality:** good enough to follow a movie, not professional.
  Very short lines ("Good evening.") are the weakest spot; the 3.3B model
  handles them much better than 1.3B. Latin, Breton and Hawaiian are
  recognised in speech but can't be subtitle languages.
- **Licenses:** Whisper is MIT. NLLB-200 is CC-BY-NC-4.0: free for personal,
  non-commercial use.
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
