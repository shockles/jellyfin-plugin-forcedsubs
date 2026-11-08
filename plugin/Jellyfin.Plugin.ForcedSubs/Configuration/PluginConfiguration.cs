using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.ForcedSubs.Configuration;

/// <summary>
/// All plugin settings. Everything except the connection and library settings is sent
/// to the service with each job, so changes apply without restarting the service.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    // ---- What to make
    public bool MakeForced { get; set; } = true;

    public bool MakeFull { get; set; }

    /// <summary>Gets or sets the subtitle languages, comma-separated Whisper codes (e.g. "en,es,ja").</summary>
    public string SubtitleLanguages { get; set; } = "en";

    // ---- Connection
    public string ServiceUrl { get; set; } = "http://127.0.0.1:8796";

    public string ApiKey { get; set; } = string.Empty;

    public int RequestTimeoutSeconds { get; set; } = 30;

    /// <summary>Gets or sets path rewrites, one per line: "jellyfin path =&gt; service path".</summary>
    public string PathMappings { get; set; } = string.Empty;

    // ---- Model
    public string Model { get; set; } = "large-v3";

    public string Device { get; set; } = "cuda";

    public string ComputeType { get; set; } = "int8_float16";

    public int BeamSize { get; set; } = 5;

    public int IdleUnloadSeconds { get; set; } = 60;

    public int TranscribeBatchSize { get; set; } = 8;

    public string TranslationModel { get; set; } = "nllb-3.3B";

    public int TranslationBeamSize { get; set; } = 4;

    // ---- Audio track
    public string AudioLanguages { get; set; } = "eng";

    public bool RequireAudioLanguage { get; set; }

    public bool SkipCommentary { get; set; } = true;

    // ---- Detection
    public string MainLanguage { get; set; } = "en";

    public string Sensitivity { get; set; } = "normal";

    public double ForeignMinProb { get; set; } = 0.60;

    public double ShortLineMinProb { get; set; } = 0.85;

    public double ShortLineSeconds { get; set; } = 1.2;

    public double RefineBelow { get; set; } = 0.90;

    public double VadThreshold { get; set; } = 0.5;

    public double MinLineSeconds { get; set; } = 0.25;

    public double WindowSeconds { get; set; } = 12;

    public int BatchSize { get; set; } = 8;

    public string SkipLanguages { get; set; } = string.Empty;

    public string OnlyLanguages { get; set; } = string.Empty;

    // ---- Translation
    public double MaxChunkSeconds { get; set; } = 28;

    public double MergeGapSeconds { get; set; } = 1.5;

    public string DropPhrases { get; set; } = string.Empty;

    // ---- Existing subtitles
    public bool SkipIfEmbeddedForced { get; set; } = true;

    public bool SkipIfExternalForced { get; set; } = true;

    public bool SkipExistingFull { get; set; } = true;

    // ---- Output

    /// <summary>Gets or sets the file name language tag: "1" = Movie.en.srt, "2" = Movie.eng.srt.</summary>
    public string LanguageTagStyle { get; set; } = "1";

    public bool MarkDefault { get; set; }

    public bool LanguagePrefix { get; set; }

    public int MaxLineChars { get; set; } = 42;

    public int MaxLines { get; set; } = 2;

    public double MinCueSeconds { get; set; } = 1.0;

    public double MaxCueSeconds { get; set; } = 7.0;

    // ---- Library task
    public bool IncludeMovies { get; set; } = true;

    public bool IncludeEpisodes { get; set; }

    /// <summary>Gets or sets the libraries to process (empty = all).</summary>
    public string[] LibraryIds { get; set; } = Array.Empty<string>();

    public bool RetryFailed { get; set; } = true;

    public int OnlyAddedWithinDays { get; set; }

    public int MinRuntimeMinutes { get; set; }

    public int MaxItemsPerRun { get; set; }

    // ---- Web client

    /// <summary>Gets or sets a value indicating whether "Generate subtitles" is added to item menus (admins only).</summary>
    public bool ShowInItemMenu { get; set; } = true;

    // ---- Automation
    public bool AutoProcessNewItems { get; set; }

    public int AutoProcessDelayMinutes { get; set; } = 10;
}
