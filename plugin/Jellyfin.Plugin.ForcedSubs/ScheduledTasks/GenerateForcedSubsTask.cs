using Jellyfin.Plugin.ForcedSubs.Configuration;
using Jellyfin.Plugin.ForcedSubs.Services;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ForcedSubs.ScheduledTasks;

/// <summary>
/// Runs every eligible item the service hasn't seen yet through the forced-subtitle
/// pipeline, one at a time. No schedule by default: run it from
/// Dashboard &gt; Scheduled Tasks, or add a trigger there.
/// </summary>
public class GenerateForcedSubsTask : IScheduledTask
{
    private readonly ItemSelector _selector;
    private readonly ForcedSubsClient _client;
    private readonly JobMonitor _monitor;
    private readonly ILogger<GenerateForcedSubsTask> _logger;

    public GenerateForcedSubsTask(ItemSelector selector, ForcedSubsClient client, JobMonitor monitor, ILogger<GenerateForcedSubsTask> logger)
    {
        _selector = selector;
        _client = client;
        _monitor = monitor;
        _logger = logger;
    }

    public string Name => "Generate subtitles";

    public string Key => "ForcedSubsGenerateLibrary";

    public string Description => "Makes forced and/or full subtitles, in the chosen languages, for items that haven't been done with the current settings.";

    public string Category => "Whisper Subtitles";

    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers() => Array.Empty<TaskTriggerInfo>();

    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        var config = Plugin.Instance?.Configuration ?? new PluginConfiguration();
        await _client.HealthAsync(cancellationToken).ConfigureAwait(false); // fail fast if the service is down

        var items = _selector.GetCandidates(config);
        _logger.LogInformation("Subtitle task: {Count} candidate items", items.Count);
        var currentSignature = ForcedSubsClient.Signature(config);
        var processed = 0;
        for (var n = 0; n < items.Count; n++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (config.MaxItemsPerRun > 0 && processed >= config.MaxItemsPerRun)
            {
                _logger.LogInformation("Reached the limit of {Max} items for this run", config.MaxItemsPerRun);
                break;
            }

            var item = items[n];
            try
            {
                var (status, signature) = await _client.HistoryAsync(item.Path, cancellationToken).ConfigureAwait(false);
                if (status is null
                    || (status == "failed" && config.RetryFailed)
                    || (status != "failed" && signature != currentSignature))
                {
                    processed++;
                    var job = await _monitor.SubmitAsync(item, overwrite: false, cancellationToken).ConfigureAwait(false);
                    var done = await WaitAsync(job.Id, n, items.Count, progress, cancellationToken).ConfigureAwait(false);
                    _logger.LogInformation("{Name}: {Status} {Message}", item.Name, done?.Status, done?.Message);
                }
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw; // the task itself was cancelled
            }
            catch (Exception ex)
            {
                // Includes HTTP timeouts (TaskCanceledException): log and move on to the next item.
                _logger.LogWarning(ex, "Subtitles failed for {Path}", item.Path);
            }

            progress.Report(100.0 * (n + 1) / items.Count);
        }
    }

    /// <summary>Wait for a job, reporting its progress as part of the task's overall progress.</summary>
    private async Task<ServiceJob?> WaitAsync(string jobId, int index, int count, IProgress<double> progress, CancellationToken ct)
    {
        try
        {
            var failures = 0;
            while (true)
            {
                ServiceJob? job;
                try
                {
                    job = await _client.JobAsync(jobId, ct).ConfigureAwait(false);
                    failures = 0;
                }
                catch (Exception ex) when (!ct.IsCancellationRequested && ex is HttpRequestException or TaskCanceledException)
                {
                    // Service busy or restarting: keep waiting for up to ~5 minutes.
                    if (++failures > 100)
                    {
                        throw;
                    }

                    await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
                    continue;
                }

                if (job is null || !job.IsActive)
                {
                    return job;
                }

                progress.Report(100.0 * (index + Math.Clamp(job.Progress, 0, 1)) / count);
                await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Task cancelled in Jellyfin: stop the running job too.
            try
            {
                await _client.CancelAsync(jobId, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
            }

            throw;
        }
    }
}
