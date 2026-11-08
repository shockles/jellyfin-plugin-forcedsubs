using System.Collections.Concurrent;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.IO;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ForcedSubs.Services;

/// <summary>
/// Submits jobs, watches them with a single shared poll loop, refreshes items when their
/// subtitles are written, and queues newly added library items when auto-processing is on.
/// </summary>
public sealed class JobMonitor : BackgroundService
{
    private static readonly TimeSpan Tick = TimeSpan.FromSeconds(5);

    private readonly ForcedSubsClient _client;
    private readonly ItemSelector _selector;
    private readonly ILibraryManager _libraryManager;
    private readonly IProviderManager _providerManager;
    private readonly IFileSystem _fileSystem;
    private readonly ILogger<JobMonitor> _logger;

    private readonly ConcurrentDictionary<string, Guid> _pending = new();
    private readonly ConcurrentDictionary<Guid, DateTime> _autoQueue = new();
    private int _unreachableTicks;

    public JobMonitor(
        ForcedSubsClient client,
        ItemSelector selector,
        ILibraryManager libraryManager,
        IProviderManager providerManager,
        IFileSystem fileSystem,
        ILogger<JobMonitor> logger)
    {
        _client = client;
        _selector = selector;
        _libraryManager = libraryManager;
        _providerManager = providerManager;
        _fileSystem = fileSystem;
        _logger = logger;
    }

    /// <summary>Queue a job for an item and watch it.</summary>
    public async Task<ServiceJob> SubmitAsync(BaseItem item, bool overwrite, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(item.Path))
        {
            throw new InvalidOperationException("Item has no file path.");
        }

        var job = await _client.SubmitAsync(item.Path, item.Id.ToString("N"), overwrite, ct).ConfigureAwait(false);
        if (job.IsActive)
        {
            _pending[job.Id] = item.Id;
        }

        return job;
    }

    public override Task StartAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemAdded += OnItemAdded;
        return base.StartAsync(cancellationToken);
    }

    public override Task StopAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemAdded -= OnItemAdded;
        return base.StopAsync(cancellationToken);
    }

    private void OnItemAdded(object? sender, ItemChangeEventArgs e)
    {
        var config = Plugin.Instance?.Configuration;
        if (config is null || !config.AutoProcessNewItems || e.Item is null)
        {
            return;
        }

        try
        {
            if (_selector.IsEligible(e.Item, config))
            {
                // Wait a while so the file has finished copying and metadata is in.
                _autoQueue[e.Item.Id] = DateTime.UtcNow.AddMinutes(Math.Max(0, config.AutoProcessDelayMinutes));
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "Could not check new item {Name}", e.Item.Name);
        }
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(Tick);
        while (await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false))
        {
            try
            {
                await SubmitDueAutoItems(stoppingToken).ConfigureAwait(false);
                await PollPending(stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Forced subtitle monitor tick failed");
            }
        }
    }

    private async Task SubmitDueAutoItems(CancellationToken ct)
    {
        if (_autoQueue.IsEmpty)
        {
            return;
        }

        var now = DateTime.UtcNow;
        foreach (var (id, due) in _autoQueue)
        {
            if (due > now || !_autoQueue.TryRemove(id, out _))
            {
                continue;
            }

            var item = _libraryManager.GetItemById(id);
            var config = Plugin.Instance?.Configuration;
            if (item is null || config is null || !config.AutoProcessNewItems || !_selector.IsEligible(item, config))
            {
                continue;
            }

            try
            {
                var job = await SubmitAsync(item, overwrite: false, ct).ConfigureAwait(false);
                _logger.LogInformation("Auto-queued forced subtitles for {Name} (job {JobId})", item.Name, job.Id);
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                // Service down: try again in a minute.
                _autoQueue[id] = now.AddMinutes(1);
                _logger.LogDebug(ex, "Service unreachable; will retry {Name}", item.Name);
            }
        }
    }

    private async Task PollPending(CancellationToken ct)
    {
        if (_pending.IsEmpty)
        {
            return;
        }

        List<ServiceJob> jobs;
        try
        {
            jobs = await _client.JobsAsync(200, ct).ConfigureAwait(false);
            _unreachableTicks = 0;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            // Give up on tracked jobs after ~10 minutes of the service being unreachable.
            if (++_unreachableTicks > 120)
            {
                _pending.Clear();
            }

            return;
        }

        var byId = jobs.ToDictionary(j => j.Id);
        foreach (var (jobId, itemId) in _pending)
        {
            if (!byId.TryGetValue(jobId, out var job))
            {
                _pending.TryRemove(jobId, out _); // fell out of the service's recent list
                continue;
            }

            if (job.IsActive)
            {
                continue;
            }

            _pending.TryRemove(jobId, out _);
            if (job.Status == "done" && !string.IsNullOrEmpty(job.Output))
            {
                _logger.LogInformation("Forced subtitles ready for {ItemId}: {Message}", itemId, job.Message);
                RefreshItem(itemId);
            }
            else
            {
                _logger.LogInformation("Forced subtitle job for {ItemId} ended: {Status} {Message}", itemId, job.Status, job.Message);
            }
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
}
