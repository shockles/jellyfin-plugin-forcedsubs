using System.Globalization;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Jellyfin.Plugin.ForcedSubs.Configuration;

namespace Jellyfin.Plugin.ForcedSubs.Services;

/// <summary>A job as reported by the forced-subtitle service.</summary>
public class ServiceJob
{
    [JsonPropertyName("id")] public string Id { get; set; } = string.Empty;
    [JsonPropertyName("path")] public string Path { get; set; } = string.Empty;
    [JsonPropertyName("item_id")] public string? ItemId { get; set; }
    [JsonPropertyName("status")] public string Status { get; set; } = string.Empty;
    [JsonPropertyName("stage")] public string Stage { get; set; } = string.Empty;
    [JsonPropertyName("progress")] public double Progress { get; set; }
    [JsonPropertyName("message")] public string Message { get; set; } = string.Empty;
    [JsonPropertyName("output")] public string? Output { get; set; }
    [JsonPropertyName("created")] public double Created { get; set; }
    [JsonPropertyName("finished")] public double? Finished { get; set; }

    [JsonIgnore] public bool IsActive => Status is "queued" or "running";
}

/// <summary>Thin HTTP client for the service. Stateless; reads the current plugin configuration per call.</summary>
public class ForcedSubsClient
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    private readonly IHttpClientFactory _httpFactory;

    public ForcedSubsClient(IHttpClientFactory httpFactory)
    {
        _httpFactory = httpFactory;
    }

    private static PluginConfiguration Config => Plugin.Instance?.Configuration ?? new PluginConfiguration();

    private static string BaseUrl => Config.ServiceUrl.TrimEnd('/');

    private HttpClient Http()
    {
        var config = Config;
        var c = _httpFactory.CreateClient("ForcedSubs");
        c.Timeout = TimeSpan.FromSeconds(Math.Clamp(config.RequestTimeoutSeconds, 5, 600));
        c.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        if (!string.IsNullOrWhiteSpace(config.ApiKey))
        {
            c.DefaultRequestHeaders.Add("X-Api-Key", config.ApiKey.Trim());
        }

        return c;
    }

    public Task<JsonElement> HealthAsync(CancellationToken ct)
        => Http().GetFromJsonAsync<JsonElement>($"{BaseUrl}/health", Json, ct);

    public async Task<List<ServiceJob>> JobsAsync(int limit, CancellationToken ct)
        => await Http().GetFromJsonAsync<List<ServiceJob>>($"{BaseUrl}/jobs?limit={limit}", Json, ct).ConfigureAwait(false)
           ?? new List<ServiceJob>();

    /// <summary>One job, or null if the service doesn't know it (e.g. it restarted and lost its in-memory jobs).</summary>
    public async Task<ServiceJob?> JobAsync(string id, CancellationToken ct)
    {
        using var resp = await Http().GetAsync($"{BaseUrl}/jobs/{Uri.EscapeDataString(id)}", ct).ConfigureAwait(false);
        if (resp.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return null;
        }

        await EnsureOk(resp, ct).ConfigureAwait(false);
        return await resp.Content.ReadFromJsonAsync<ServiceJob>(Json, ct).ConfigureAwait(false);
    }

    public async Task<ServiceJob?> CancelAsync(string id, CancellationToken ct)
    {
        using var resp = await Http().PostAsync($"{BaseUrl}/jobs/{Uri.EscapeDataString(id)}/cancel", null, ct).ConfigureAwait(false);
        await EnsureOk(resp, ct).ConfigureAwait(false);
        return await resp.Content.ReadFromJsonAsync<ServiceJob>(Json, ct).ConfigureAwait(false);
    }

    /// <summary>Last recorded result for a file: (status, signature), or (null, null) if never processed.</summary>
    public async Task<(string? Status, string? Signature)> HistoryAsync(string path, CancellationToken ct)
    {
        var doc = await Http().GetFromJsonAsync<JsonElement>(
            $"{BaseUrl}/history?path={Uri.EscapeDataString(MapPath(path))}", Json, ct).ConfigureAwait(false);
        if (doc.ValueKind != JsonValueKind.Object)
        {
            return (null, null);
        }

        string? Get(string name) => doc.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        return (Get("status"), Get("signature"));
    }

    /// <summary>
    /// Describes what a job is asked to produce. The library task redoes an item when this
    /// changes, e.g. after adding a subtitle language or turning on full subtitles.
    /// </summary>
    public static string Signature(PluginConfiguration c)
    {
        var langs = c.SubtitleLanguages
            .Split(new[] { ',', ';', ' ', '\n' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(l => l.ToLowerInvariant())
            .Distinct()
            .Order(StringComparer.Ordinal);
        return $"forced={c.MakeForced};full={c.MakeFull};langs={string.Join(',', langs)}";
    }

    /// <summary>Forget one file (path != null) or every file, so the library task checks it again.</summary>
    public async Task<int> ForgetAsync(string? path, CancellationToken ct)
    {
        var url = path is null ? $"{BaseUrl}/history" : $"{BaseUrl}/history?path={Uri.EscapeDataString(MapPath(path))}";
        using var resp = await Http().DeleteAsync(url, ct).ConfigureAwait(false);
        await EnsureOk(resp, ct).ConfigureAwait(false);
        var doc = await resp.Content.ReadFromJsonAsync<JsonElement>(Json, ct).ConfigureAwait(false);
        return doc.TryGetProperty("removed", out var n) ? n.GetInt32() : 0;
    }

    public async Task<ServiceJob> SubmitAsync(string path, string itemId, bool overwrite, CancellationToken ct)
    {
        var config = Config;
        var body = new
        {
            path = MapPath(path),
            overwrite,
            item_id = itemId,
            options = BuildOptions(config),
            signature = Signature(config),
        };
        using var resp = await Http().PostAsJsonAsync($"{BaseUrl}/jobs", body, Json, ct).ConfigureAwait(false);
        await EnsureOk(resp, ct).ConfigureAwait(false);
        return await resp.Content.ReadFromJsonAsync<ServiceJob>(Json, ct).ConfigureAwait(false)
               ?? throw new HttpRequestException("Empty response from service.");
    }

    private static async Task EnsureOk(HttpResponseMessage resp, CancellationToken ct)
    {
        if (!resp.IsSuccessStatusCode)
        {
            var text = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            throw new HttpRequestException($"Service returned {(int)resp.StatusCode}: {text}");
        }
    }

    /// <summary>Options the service applies to one job (snake_case = the service's Settings fields).</summary>
    internal static Dictionary<string, object> BuildOptions(PluginConfiguration c) => new()
    {
        ["make_forced"] = c.MakeForced,
        ["make_full"] = c.MakeFull,
        ["subtitle_languages"] = c.SubtitleLanguages,
        ["transcribe_batch_size"] = c.TranscribeBatchSize,
        ["translation_model"] = c.TranslationModel,
        ["translation_beam_size"] = c.TranslationBeamSize,
        ["skip_existing_full"] = c.SkipExistingFull,
        ["language_tag_style"] = c.LanguageTagStyle,
        ["model"] = c.Model,
        ["device"] = c.Device,
        ["compute_type"] = c.ComputeType,
        ["beam_size"] = c.BeamSize,
        ["idle_unload"] = c.IdleUnloadSeconds,
        ["audio_languages"] = c.AudioLanguages,
        ["require_audio_language"] = c.RequireAudioLanguage,
        ["skip_commentary"] = c.SkipCommentary,
        ["main_language"] = c.MainLanguage,
        ["foreign_min_prob"] = c.ForeignMinProb,
        ["short_line_min_prob"] = c.ShortLineMinProb,
        ["short_line_seconds"] = c.ShortLineSeconds,
        ["refine_below"] = c.RefineBelow,
        ["vad_threshold"] = c.VadThreshold,
        ["min_line_seconds"] = c.MinLineSeconds,
        ["window_seconds"] = c.WindowSeconds,
        ["batch_size"] = c.BatchSize,
        ["skip_languages"] = c.SkipLanguages,
        ["only_languages"] = c.OnlyLanguages,
        ["max_chunk_seconds"] = c.MaxChunkSeconds,
        ["merge_gap_seconds"] = c.MergeGapSeconds,
        ["drop_phrases"] = c.DropPhrases,
        ["skip_if_embedded_forced"] = c.SkipIfEmbeddedForced,
        ["skip_if_external_forced"] = c.SkipIfExternalForced,
        ["mark_default"] = c.MarkDefault,
        ["language_prefix"] = c.LanguagePrefix,
        ["max_line_chars"] = c.MaxLineChars,
        ["max_lines"] = c.MaxLines,
        ["min_cue_seconds"] = c.MinCueSeconds,
        ["max_cue_seconds"] = c.MaxCueSeconds,
    };

    /// <summary>Rewrite a Jellyfin path to the path the service sees, using the configured mappings.</summary>
    public static string MapPath(string path)
    {
        foreach (var raw in Config.PathMappings.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            var parts = raw.Split("=>", 2, StringSplitOptions.TrimEntries);
            if (parts.Length != 2 || parts[0].Length == 0)
            {
                continue;
            }

            if (path.StartsWith(parts[0], true, CultureInfo.InvariantCulture))
            {
                var rest = path[parts[0].Length..];
                var target = parts[1];
                // Mapping to a Linux/Docker path: use forward slashes for the remainder.
                if (target.Contains('/', StringComparison.Ordinal) && !target.Contains('\\', StringComparison.Ordinal))
                {
                    rest = rest.Replace('\\', '/');
                }

                return target + rest;
            }
        }

        return path;
    }
}
