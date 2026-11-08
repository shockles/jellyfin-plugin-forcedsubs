using Jellyfin.Plugin.ForcedSubs.Services;
using Jellyfin.Plugin.ForcedSubs.Web;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.ForcedSubs;

public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<ForcedSubsClient>();
        serviceCollection.AddSingleton<ItemSelector>();
        serviceCollection.AddSingleton<JobMonitor>();
        serviceCollection.AddHostedService(sp => sp.GetRequiredService<JobMonitor>());
        serviceCollection.AddTransient<IStartupFilter, ScriptInjectionStartupFilter>();
    }
}
