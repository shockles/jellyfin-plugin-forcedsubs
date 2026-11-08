using System.Collections.Concurrent;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.IO;
using Microsoft.Extensions.Logging;

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

/// <summary>Talks to the local service and refreshes items when their subtitles are ready.</summary>
public class ForcedSubsClient
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    private readonly IHttpClientFactory _httpFactory;
    private readonly ILibraryManager _libraryManager;
    private readonly IProviderManager _providerManager;
    private readonly IFileSystem _fileSystem;
    private readonly ILogger<ForcedSubsClient> _logger;
    private readonly ConcurrentDictionary<string, Guid> _watching = new();

    public ForcedSubsClient(
        IHttpClientFactory httpFactory,
        ILibraryManager libraryManager,
        IProviderManager providerManager,
        IFileSystem fileSystem,
        ILogger<ForcedSubsClient> logger)
    {
        _httpFactory = httpFactory;
        _libraryManager = libraryManager;
        _providerManager = providerManager;
        _fileSystem = fileSystem;
        _logger = logger;
    }

    private static string BaseUrl => (Plugin.Instance?.Configuration.ServiceUrl ?? "http://127.0.0.1:8796").TrimEnd('/');

    private HttpClient Http()
    {
        var c = _httpFactory.CreateClient("ForcedSubs");
        c.Timeout = TimeSpan.FromSeconds(30);
        return c;
    }

    public async Task<JsonElement> HealthAsync(CancellationToken ct)
    {
        return await Http().GetFromJsonAsync<JsonElement>($"{BaseUrl}/health", Json, ct).ConfigureAwait(false);
    }

    public async Task<List<ServiceJob>> JobsAsync(int limit, CancellationToken ct)
    {
        return await Http().GetFromJsonAsync<List<ServiceJob>>($"{BaseUrl}/jobs?limit={limit}", Json, ct).ConfigureAwait(false)
            ?? new List<ServiceJob>();
    }

    public async Task<ServiceJob?> JobAsync(string id, CancellationToken ct)
    {
        return await Http().GetFromJsonAsync<ServiceJob>($"{BaseUrl}/jobs/{Uri.EscapeDataString(id)}", Json, ct).ConfigureAwait(false);
    }

    /// <summary>Last recorded result for a file, or null if the service has never processed it.</summary>
    public async Task<string?> HistoryStatusAsync(string path, CancellationToken ct)
    {
        var doc = await Http().GetFromJsonAsync<JsonElement>(
            $"{BaseUrl}/history?path={Uri.EscapeDataString(path)}", Json, ct).ConfigureAwait(false);
        return doc.ValueKind == JsonValueKind.Object && doc.TryGetProperty("status", out var s) ? s.GetString() : null;
    }

    /// <summary>Queue a job for an item and start watching it so the item is refreshed when done.</summary>
    public async Task<ServiceJob> SubmitAsync(BaseItem item, bool overwrite, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(item.Path))
        {
            throw new InvalidOperationException("Item has no file path.");
        }

        var body = new { path = item.Path, overwrite, item_id = item.Id.ToString("N") };
        using var resp = await Http().PostAsJsonAsync($"{BaseUrl}/jobs", body, Json, ct).ConfigureAwait(false);
        if (!resp.IsSuccessStatusCode)
        {
            var text = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            throw new HttpRequestException($"Service returned {(int)resp.StatusCode}: {text}");
        }

        var job = await resp.Content.ReadFromJsonAsync<ServiceJob>(Json, ct).ConfigureAwait(false)
            ?? throw new HttpRequestException("Empty response from service.");

        if (_watching.TryAdd(job.Id, item.Id))
        {
            _ = Task.Run(() => WatchAsync(job.Id, item.Id));
        }

        return job;
    }

    /// <summary>Wait for a job to finish (used by the library task).</summary>
    public async Task<ServiceJob?> WaitAsync(string jobId, CancellationToken ct)
    {
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var job = await JobAsync(jobId, ct).ConfigureAwait(false);
            if (job is null || !job.IsActive)
            {
                return job;
            }

            await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
        }
    }

    private async Task WatchAsync(string jobId, Guid itemId)
    {
        try
        {
            ServiceJob? job;
            var failures = 0;
            while (true)
            {
                await Task.Delay(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
                try
                {
                    job = await JobAsync(jobId, CancellationToken.None).ConfigureAwait(false);
                    failures = 0;
                }
                catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
                {
                    // Service restarted or briefly unreachable; give up after ~10 minutes.
                    if (++failures > 120)
                    {
                        return;
                    }

                    continue;
                }

                if (job is null || !job.IsActive)
                {
                    break;
                }
            }

            if (job is { Status: "done" } && !string.IsNullOrEmpty(job.Output))
            {
                _logger.LogInformation("Forced subtitles ready for {ItemId}: {Message}", itemId, job.Message);
                RefreshItem(itemId);
            }
            else if (job is not null)
            {
                _logger.LogInformation("Forced subtitle job for {ItemId} ended: {Status} {Message}", itemId, job.Status, job.Message);
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error watching forced subtitle job {JobId}", jobId);
        }
        finally
        {
            _watching.TryRemove(jobId, out _);
        }
    }

    /// <summary>Ask Jellyfin to rescan the item so it notices the new .forced.srt file.</summary>
    public void RefreshItem(Guid itemId)
    {
        var options = new MetadataRefreshOptions(new DirectoryService(_fileSystem))
        {
            MetadataRefreshMode = MetadataRefreshMode.Default,
            ImageRefreshMode = MetadataRefreshMode.Default,
            ReplaceAllMetadata = false,
            ReplaceAllImages = false,
        };
        _providerManager.QueueRefresh(itemId, options, RefreshPriority.High);
    }

    public BaseItem? GetItem(Guid id) => _libraryManager.GetItemById(id);
}
