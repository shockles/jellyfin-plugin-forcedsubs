using System.Net.Mime;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.ForcedSubs.Services;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.ForcedSubs.Api;

/// <summary>Endpoints used by the plugin's dashboard page. Admins only.</summary>
[ApiController]
[Route("ForcedSubs")]
[Authorize(Policy = "RequiresElevation")]
[Produces(MediaTypeNames.Application.Json)]
public class ForcedSubsController : ControllerBase
{
    private readonly ILibraryManager _libraryManager;
    private readonly ForcedSubsClient _client;

    public ForcedSubsController(ILibraryManager libraryManager, ForcedSubsClient client)
    {
        _libraryManager = libraryManager;
        _client = client;
    }

    [HttpGet("Health")]
    public async Task<ActionResult> Health(CancellationToken ct)
    {
        try
        {
            return Ok(await _client.HealthAsync(ct).ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return StatusCode(StatusCodes.Status502BadGateway, new { ok = false, error = ex.Message });
        }
    }

    [HttpGet("Search")]
    public ActionResult Search([FromQuery] string? term, [FromQuery] int limit = 40)
    {
        if (string.IsNullOrWhiteSpace(term))
        {
            return Ok(Array.Empty<object>());
        }

        var items = _libraryManager.GetItemList(new InternalItemsQuery
        {
            SearchTerm = term.Trim(),
            IncludeItemTypes = new[] { BaseItemKind.Movie, BaseItemKind.Episode },
            IsVirtualItem = false,
            Recursive = true,
            Limit = Math.Clamp(limit, 1, 100),
        });

        return Ok(items.Select(Describe));
    }

    [HttpPost("Generate/{itemId}")]
    public async Task<ActionResult> Generate([FromRoute] Guid itemId, [FromQuery] bool overwrite, CancellationToken ct)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "Item not found" });
        }

        try
        {
            var job = await _client.SubmitAsync(item, overwrite, ct).ConfigureAwait(false);
            return Ok(job);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or InvalidOperationException)
        {
            return StatusCode(StatusCodes.Status502BadGateway, new { error = ex.Message });
        }
    }

    [HttpGet("Jobs")]
    public async Task<ActionResult> Jobs([FromQuery] int limit = 30, CancellationToken ct = default)
    {
        try
        {
            var jobs = await _client.JobsAsync(Math.Clamp(limit, 1, 200), ct).ConfigureAwait(false);
            return Ok(jobs.Select(j => new
            {
                j.Id,
                j.Status,
                j.Stage,
                j.Progress,
                j.Message,
                j.Output,
                j.Created,
                j.Finished,
                Name = NameFor(j),
            }));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return StatusCode(StatusCodes.Status502BadGateway, new { error = ex.Message });
        }
    }

    private string NameFor(ServiceJob job)
    {
        if (Guid.TryParse(job.ItemId, out var id) && _libraryManager.GetItemById(id) is { } item)
        {
            return DisplayName(item);
        }

        return System.IO.Path.GetFileNameWithoutExtension(job.Path);
    }

    private static string DisplayName(BaseItem item)
    {
        if (item is Episode ep)
        {
            var s = ep.ParentIndexNumber is { } sn ? $"S{sn:00}" : string.Empty;
            var e = ep.IndexNumber is { } en ? $"E{en:00}" : string.Empty;
            return $"{ep.SeriesName} {s}{e} - {ep.Name}";
        }

        return item.ProductionYear is { } y ? $"{item.Name} ({y})" : item.Name;
    }

    private static object Describe(BaseItem item)
    {
        var hasForced = false;
        if (!string.IsNullOrEmpty(item.Path))
        {
            var dir = System.IO.Path.GetDirectoryName(item.Path);
            var stem = System.IO.Path.GetFileNameWithoutExtension(item.Path);
            if (dir is not null && Directory.Exists(dir))
            {
                hasForced = Directory.EnumerateFiles(dir, stem + "*")
                    .Any(f => System.IO.Path.GetFileName(f).Contains(".forced", StringComparison.OrdinalIgnoreCase));
            }
        }

        return new
        {
            Id = item.Id.ToString("N"),
            Name = DisplayName(item),
            Type = item.GetBaseItemKind().ToString(),
            item.Path,
            HasForcedFile = hasForced,
        };
    }
}
