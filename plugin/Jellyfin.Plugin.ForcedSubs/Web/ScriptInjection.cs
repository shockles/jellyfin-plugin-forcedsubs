using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ForcedSubs.Web;

/// <summary>Puts <see cref="ScriptInjectionMiddleware"/> at the front of Jellyfin's HTTP pipeline.</summary>
public class ScriptInjectionStartupFilter : IStartupFilter
{
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next)
        => app =>
        {
            app.UseMiddleware<ScriptInjectionMiddleware>();
            next(app);
        };
}

/// <summary>
/// Adds one &lt;script&gt; tag to the Jellyfin web client's index.html as it is served, so the
/// "Generate subtitles" item-menu entry is available. Nothing on disk is modified, so it
/// survives Jellyfin updates and disappears when the plugin is removed.
/// </summary>
public class ScriptInjectionMiddleware
{
    private const string Marker = "forcedsubs-menu";

    private readonly RequestDelegate _next;
    private readonly ILogger<ScriptInjectionMiddleware> _logger;

    public ScriptInjectionMiddleware(RequestDelegate next, ILogger<ScriptInjectionMiddleware> logger)
    {
        _next = next;
        _logger = logger;
    }

    public async Task InvokeAsync(HttpContext context)
    {
        if (!HttpMethods.IsGet(context.Request.Method)
            || !IsIndexPath(context.Request.Path.Value)
            || Plugin.Instance?.Configuration.ShowInItemMenu == false)
        {
            await _next(context).ConfigureAwait(false);
            return;
        }

        // Ask for a plain, complete response so it can be edited.
        context.Request.Headers.Remove("Accept-Encoding");
        context.Request.Headers.Remove("If-None-Match");
        context.Request.Headers.Remove("If-Modified-Since");

        var original = context.Response.Body;
        await using var buffer = new MemoryStream();
        context.Response.Body = buffer;
        try
        {
            await _next(context).ConfigureAwait(false);
        }
        finally
        {
            context.Response.Body = original;
        }

        buffer.Position = 0;
        var isHtml = context.Response.StatusCode == StatusCodes.Status200OK
                     && (context.Response.ContentType?.Contains("text/html", StringComparison.OrdinalIgnoreCase) ?? false)
                     && !context.Response.Headers.ContainsKey("Content-Encoding");
        if (!isHtml)
        {
            await buffer.CopyToAsync(original).ConfigureAwait(false);
            return;
        }

        var html = Encoding.UTF8.GetString(buffer.ToArray());
        var at = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
        if (at >= 0 && !html.Contains(Marker, StringComparison.Ordinal))
        {
            var version = typeof(Plugin).Assembly.GetName().Version?.ToString() ?? "0";
            // index.html lives at <base>/web/, so ../ForcedSubs reaches the plugin's endpoint under any base URL.
            var tag = $"<script id=\"{Marker}\" defer src=\"../ForcedSubs/ClientScript?v={version}\"></script>";
            html = html.Insert(at, tag);
        }

        var bytes = Encoding.UTF8.GetBytes(html);
        context.Response.ContentLength = bytes.Length;
        context.Response.Headers.Remove("ETag");
        context.Response.Headers.Remove("Last-Modified");
        context.Response.Headers.CacheControl = "no-cache";
        try
        {
            await original.WriteAsync(bytes).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is IOException or OperationCanceledException)
        {
            _logger.LogDebug(ex, "Client disconnected while sending index.html");
        }
    }

    private static bool IsIndexPath(string? path)
    {
        if (string.IsNullOrEmpty(path))
        {
            return false;
        }

        return path.EndsWith("/web/", StringComparison.OrdinalIgnoreCase)
               || path.EndsWith("/web/index.html", StringComparison.OrdinalIgnoreCase);
    }
}
