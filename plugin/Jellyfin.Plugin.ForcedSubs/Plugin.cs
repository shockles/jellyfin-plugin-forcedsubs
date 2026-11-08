using Jellyfin.Plugin.ForcedSubs.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.ForcedSubs;

/// <summary>
/// Generates forced English subtitles for the foreign-language lines in a movie,
/// using a local Whisper service.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    public static Plugin? Instance { get; private set; }

    public override string Name => "Forced Subtitles (Whisper)";

    public override Guid Id => Guid.Parse("221cf741-9df3-4388-ad06-8761cce21b32");

    public override string Description =>
        "Translates only the foreign-language lines of a movie into a forced English subtitle, using a local Whisper service.";

    public IEnumerable<PluginPageInfo> GetPages()
    {
        return new[]
        {
            new PluginPageInfo
            {
                Name = Name,
                EmbeddedResourcePath = GetType().Namespace + ".Configuration.configPage.html",
            },
        };
    }
}
