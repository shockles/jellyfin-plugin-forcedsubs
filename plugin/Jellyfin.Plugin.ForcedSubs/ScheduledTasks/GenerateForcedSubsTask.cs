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

    public string Name => "Generate forced subtitles";

    public string Key => "ForcedSubsGenerateLibrary";

    public string Description => "Finds foreign-language lines in items that haven't been checked yet and writes forced English subtitles for them.";

    public string Category => "Forced Subtitles";

    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers() => Array.Empty<TaskTriggerInfo>();

    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        var config = Plugin.Instance?.Configuration ?? new PluginConfiguration();
        await _client.HealthAsync(cancellationToken).ConfigureAwait(false); // fail fast if the service is down

        var items = _selector.GetCandidates(config);
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
                var previous = await _client.HistoryStatusAsync(item.Path, cancellationToken).ConfigureAwait(false);
                if (previous is null || (previous == "failed" && config.RetryFailed))
                {
                    processed++;
                    var job = await _monitor.SubmitAsync(item, overwrite: false, cancellationToken).ConfigureAwait(false);
                    var done = await WaitAsync(job.Id, n, items.Count, progress, cancellationToken).ConfigureAwait(false);
                    _logger.LogInformation("{Name}: {Status} {Message}", item.Name, done?.Status, done?.Message);
                }
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Forced subtitles failed for {Path}", item.Path);
            }

            progress.Report(100.0 * (n + 1) / items.Count);
        }
    }

    /// <summary>Wait for a job, reporting its progress as part of the task's overall progress.</summary>
    private async Task<ServiceJob?> WaitAsync(string jobId, int index, int count, IProgress<double> progress, CancellationToken ct)
    {
        try
        {
            while (true)
            {
                var job = await _client.JobAsync(jobId, ct).ConfigureAwait(false);
                if (job is null || !job.IsActive)
                {
                    return job;
                }

                progress.Report(100.0 * (index + Math.Clamp(job.Progress, 0, 1)) / count);
                await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
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
