using System.Reflection;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.ForcedSubs.Api;

/// <summary>
/// Serves the web-client script that adds "Generate subtitles" to item menus. The script is
/// public (like the rest of the web client); what it calls is admin-only.
/// </summary>
[ApiController]
[Route("ForcedSubs")]
public class ClientScriptController : ControllerBase
{
    private static readonly Lazy<string> Script = new(() =>
    {
        var asm = Assembly.GetExecutingAssembly();
        using var stream = asm.GetManifestResourceStream("Jellyfin.Plugin.ForcedSubs.Web.menu.js")
                           ?? throw new InvalidOperationException("menu.js resource missing");
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    });

    [HttpGet("ClientScript")]
    [AllowAnonymous]
    [Produces("application/javascript")]
    public ContentResult ClientScript()
    {
        Response.Headers.CacheControl = "public, max-age=3600";
        return Content(Script.Value, "application/javascript; charset=utf-8");
    }
}
