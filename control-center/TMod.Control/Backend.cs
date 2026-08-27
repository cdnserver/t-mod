using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json;

namespace TMod.Control;

internal sealed record ServiceState(string Service, string State, string Health, string Label);
internal sealed record SystemSnapshot(bool Ok, string Release, string Branch, bool DockerReady, IReadOnlyList<ServiceState> Services)
{
    internal static SystemSnapshot Offline(string detail = "unknown") => new(false, detail, "offline", false, []);
}

internal sealed class Backend : IDisposable
{
    internal static readonly string[] Services =
    [
        "tmod-postgres", "tmod-db-migrate", "tmod-discord-bot", "tmod-web", "tmod-worker",
        "atlas-qdrant", "atlas-forum-browser", "tmod-caddy", "minecraft", "minecraft-supervisor"
    ];

    internal static readonly string[] Groups = ["core", "atlas", "minecraft"];

    private readonly string _payloadPath;
    internal string ProjectDir { get; }
    internal string PersistentDir { get; }

    internal Backend(string projectDir, string persistentDir)
    {
        ProjectDir = Path.GetFullPath(projectDir);
        PersistentDir = Path.GetFullPath(persistentDir);
        _payloadPath = ExtractPayload();
    }

    internal static string? FindProject(string? requested)
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var candidates = new[]
        {
            requested,
            AppContext.BaseDirectory,
            Path.Combine(AppContext.BaseDirectory, "esgiel"),
            Path.Combine(home, "Desktop", "esgiel")
        };
        return candidates
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .Select(value => Path.GetFullPath(value!))
            .FirstOrDefault(value => File.Exists(Path.Combine(value, "docker-compose.yml")));
    }

    private static string ExtractPayload()
    {
        var assembly = Assembly.GetExecutingAssembly();
        var resource = assembly.GetManifestResourceNames().Single(name => name.EndsWith("TModControlPayload.ps1", StringComparison.Ordinal));
        var cacheDir = Path.Combine(Path.GetTempPath(), "TMod", "Control", "1.2.0");
        Directory.CreateDirectory(cacheDir);
        var destination = Path.Combine(cacheDir, "control.ps1");
        using var source = assembly.GetManifestResourceStream(resource) ?? throw new InvalidOperationException("Встроенный управляющий модуль отсутствует.");
        using var memory = new MemoryStream();
        source.CopyTo(memory);
        var expected = memory.ToArray();
        if (!expected.AsSpan().StartsWith(new byte[] { 0xEF, 0xBB, 0xBF })) throw new InvalidDataException("Встроенный управляющий модуль имеет неверную кодировку.");
        if (!File.Exists(destination) || !File.ReadAllBytes(destination).SequenceEqual(expected))
        {
            var temporary = destination + ".tmp";
            File.WriteAllBytes(temporary, expected);
            File.Move(temporary, destination, true);
        }
        return destination;
    }

    private ProcessStartInfo StartInfo(string action, string? service = null, string? group = null, bool json = false)
    {
        var info = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            UseShellExecute = false,
            WorkingDirectory = ProjectDir,
        };
        foreach (var value in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _payloadPath, "-ProjectDir", ProjectDir, "-PersistentDir", PersistentDir, "-Action", action, "-NoAnimation" })
            info.ArgumentList.Add(value);
        if (!string.IsNullOrWhiteSpace(service)) { info.ArgumentList.Add("-Service"); info.ArgumentList.Add(service); }
        if (!string.IsNullOrWhiteSpace(group)) { info.ArgumentList.Add("-Group"); info.ArgumentList.Add(group); }
        if (json) info.ArgumentList.Add("-Json");
        return info;
    }

    internal async Task<SystemSnapshot> GetStatusAsync(TimeSpan timeout)
    {
        Process? process = null;
        try
        {
            var info = StartInfo("status", json: true);
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.CreateNoWindow = true;
            var startedProcess = Process.Start(info) ?? throw new InvalidOperationException("PowerShell не запустился.");
            process = startedProcess;
            using var cancellation = new CancellationTokenSource(timeout);
            using var killRegistration = cancellation.Token.Register(() =>
            {
                try { if (!startedProcess.HasExited) startedProcess.Kill(true); } catch { }
            });
            var outputTask = startedProcess.StandardOutput.ReadToEndAsync(cancellation.Token);
            var errorTask = startedProcess.StandardError.ReadToEndAsync(cancellation.Token);
            await startedProcess.WaitForExitAsync(cancellation.Token);
            var output = await outputTask;
            _ = await errorTask;
            if (startedProcess.ExitCode != 0) return SystemSnapshot.Offline("backend error");
            var start = output.IndexOf('{');
            var end = output.LastIndexOf('}');
            if (start < 0 || end <= start) return SystemSnapshot.Offline("invalid status");
            using var document = JsonDocument.Parse(output[start..(end + 1)]);
            var root = document.RootElement;
            var services = new List<ServiceState>();
            foreach (var item in root.GetProperty("services").EnumerateArray())
            {
                services.Add(new(
                    item.GetProperty("service").GetString() ?? "unknown",
                    item.GetProperty("state").GetString() ?? "unknown",
                    item.GetProperty("health").GetString() ?? "",
                    item.GetProperty("label").GetString() ?? "OFFLINE"));
            }
            return new(
                root.GetProperty("ok").GetBoolean(),
                root.GetProperty("release").GetString() ?? "unknown",
                root.GetProperty("branch").GetString() ?? "unknown",
                root.GetProperty("docker_ready").GetBoolean(),
                services);
        }
        catch
        {
            return SystemSnapshot.Offline("status timeout");
        }
        finally
        {
            process?.Dispose();
        }
    }

    internal int RunVisible(string action, string? service = null, string? group = null)
    {
        using var process = Process.Start(StartInfo(action, service, group)) ?? throw new InvalidOperationException("Не удалось запустить управляющий модуль.");
        process.WaitForExit();
        return process.ExitCode;
    }

    internal static bool SelfTest()
    {
        var assembly = Assembly.GetExecutingAssembly();
        var resource = assembly.GetManifestResourceNames().SingleOrDefault(name => name.EndsWith("TModControlPayload.ps1", StringComparison.Ordinal));
        if (resource is null || Themes.All.Count != 4 || Services.Distinct().Count() != Services.Length) return false;
        using var stream = assembly.GetManifestResourceStream(resource)!;
        Span<byte> bom = stackalloc byte[3];
        return stream.Read(bom) == 3 && bom.SequenceEqual(new byte[] { 0xEF, 0xBB, 0xBF });
    }

    public void Dispose()
    {
        // The versioned cache is intentionally retained to avoid extracting on every launch.
    }
}
