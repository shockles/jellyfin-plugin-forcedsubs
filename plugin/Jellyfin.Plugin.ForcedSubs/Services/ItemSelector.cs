using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.ForcedSubs.Configuration;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;

namespace Jellyfin.Plugin.ForcedSubs.Services;

/// <summary>Decides which library items the library task and auto-processing should handle.</summary>
public class ItemSelector
{
    private readonly ILibraryManager _libraryManager;

    public ItemSelector(ILibraryManager libraryManager)
    {
        _libraryManager = libraryManager;
    }

    private static BaseItemKind[] Kinds(PluginConfiguration c)
    {
        var kinds = new List<BaseItemKind>(2);
        if (c.IncludeMovies)
        {
            kinds.Add(BaseItemKind.Movie);
        }

        if (c.IncludeEpisodes)
        {
            kinds.Add(BaseItemKind.Episode);
        }

        return kinds.ToArray();
    }

    private static Guid[] LibraryGuids(PluginConfiguration c)
        => c.LibraryIds.Select(id => Guid.TryParse(id, out var g) ? g : Guid.Empty).Where(g => g != Guid.Empty).ToArray();

    /// <summary>Items the library task should consider, newest first.</summary>
    public List<BaseItem> GetCandidates(PluginConfiguration c)
    {
        var kinds = Kinds(c);
        if (kinds.Length == 0)
        {
            return new List<BaseItem>();
        }

        var query = new InternalItemsQuery
        {
            IncludeItemTypes = kinds,
            IsVirtualItem = false,
            Recursive = true,
            OrderBy = new[] { (ItemSortBy.DateCreated, SortOrder.Descending) },
        };

        var libs = LibraryGuids(c);
        if (libs.Length > 0)
        {
            query.AncestorIds = libs;
        }

        if (c.OnlyAddedWithinDays > 0)
        {
            query.MinDateCreated = DateTime.UtcNow.AddDays(-c.OnlyAddedWithinDays);
        }

        return _libraryManager.GetItemList(query).Where(i => PassesFileChecks(i, c)).ToList();
    }

    /// <summary>Whether a newly added item qualifies for automatic processing.</summary>
    public bool IsEligible(BaseItem item, PluginConfiguration c)
    {
        if (item.IsVirtualItem || !Kinds(c).Contains(item.GetBaseItemKind()) || !PassesFileChecks(item, c))
        {
            return false;
        }

        var libs = LibraryGuids(c);
        return libs.Length == 0 || _libraryManager.GetCollectionFolders(item).Any(f => libs.Contains(f.Id));
    }

    private static bool PassesFileChecks(BaseItem item, PluginConfiguration c)
    {
        if (string.IsNullOrEmpty(item.Path) || !File.Exists(item.Path))
        {
            return false;
        }

        // Runtime may not be known yet for brand-new items; only filter when it is.
        if (c.MinRuntimeMinutes > 0 && item.RunTimeTicks is { } ticks && TimeSpan.FromTicks(ticks).TotalMinutes < c.MinRuntimeMinutes)
        {
            return false;
        }

        return true;
    }
}
