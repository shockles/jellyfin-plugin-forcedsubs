using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.ForcedSubs.Configuration;

public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>Gets or sets the address of the local forced-subtitle service.</summary>
    public string ServiceUrl { get; set; } = "http://127.0.0.1:8796";

    /// <summary>Gets or sets a value indicating whether the library task also processes TV episodes.</summary>
    public bool IncludeEpisodes { get; set; }

    /// <summary>Gets or sets a value indicating whether the library task retries items that failed before.</summary>
    public bool RetryFailed { get; set; } = true;
}
