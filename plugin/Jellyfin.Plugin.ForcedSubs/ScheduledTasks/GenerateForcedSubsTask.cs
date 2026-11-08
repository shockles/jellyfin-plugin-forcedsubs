using Jellyfin.Data.Enums;
using Jellyfin.Plugin.ForcedSubs.Services;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ForcedSubs.ScheduledTasks;

/// <summary>
/// Runs every movie (and optionally episode) the service hasn't seen yet through
/// the forced-subtitle pipeline, one at a time. No schedule by default: run it from
/// Dashboard &gt; Scheduled Tasks, or add a trigger there.
/// </summary>
public class GenerateForcedSubsTask : IScheduledTask
{
    private readonly ILibraryManager _libraryManager;
    private readonly ForcedSubsClient _client;
    private readonly ILogger<GenerateForcedSubsTask> _logger;

    public GenerateForcedSubsTask(ILibraryManager libraryManager, ForcedSubsClient client, ILogger<GenerateForcedSubsTask> logger)
    {
        _libraryManager = libraryManager;
        _client = client;
        _logger = logger;
    }

    public string Name => "Generate forced subtitles";

    public string Key => "ForcedSubsGenerateLibrary";

    public string Description => "Finds foreign-language lines in movies that haven't been checked yet and writes forced English subtitles for them.";

    public string Category => "Forced Subtitles";

    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers() => Array.Empty<TaskTriggerInfo>();

    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        var config = Plugin.Instance?.Configuration;
        var kinds = config?.IncludeEpisodes == true
            ? new[] { BaseItemKind.Movie, BaseItemKind.Episode }
            : new[] { BaseItemKind.Movie };

        var items = _libraryManager.GetItemList(new InternalItemsQuery
        {
            IncludeItemTypes = kinds,
            IsVirtualItem = false,
            Recursive = true,
        }).Where(i => !string.IsNullOrEmpty(i.Path) && File.Exists(i.Path)).ToList();

        await _client.HealthAsync(cancellationToken).ConfigureAwait(false); // fail fast if the service is down

        for (var n = 0; n < items.Count; n++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var item = items[n];
            try
            {
                var previous = await _client.HistoryStatusAsync(item.Path, cancellationToken).ConfigureAwait(false);
                var retry = previous == "failed" && config?.RetryFailed != false;
                if (previous is null || retry)
                {
                    var job = await _client.SubmitAsync(item, overwrite: false, cancellationToken).ConfigureAwait(false);
                    var done = await _client.WaitAsync(job.Id, cancellationToken).ConfigureAwait(false);
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
}
