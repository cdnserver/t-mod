using System.Security.Cryptography;
using System.Text.Json;

namespace Blackbird.Setup.Core;

public sealed record PayloadFile(string Path, long Size, string Sha256);
public sealed record PayloadManifest(int Schema, string Product, string Version, string Executable, PayloadFile[] Files)
{
    public const string ProductId = "lat.tvr.technology.blackbird";
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web) { WriteIndented = true };
    public const long MaxExpandedBytes = 16L * 1024 * 1024 * 1024;

    public static PayloadManifest Read(Stream stream)
    {
        var result = JsonSerializer.Deserialize<PayloadManifest>(stream, Json) ?? throw new InvalidDataException("Пустой манифест установки.");
        result.Validate();
        return result;
    }

    public void Validate()
    {
        if (Schema != 1 || Product != ProductId || Files is null || Files.Length is < 2 or > 30000)
            throw new InvalidDataException("Несовместимый пакет Blackbird.");
        if (string.IsNullOrWhiteSpace(Version) || Version.Length > 80 || !Version.All(c => char.IsAsciiLetterOrDigit(c) || ".-+".Contains(c)))
            throw new InvalidDataException("Некорректная версия пакета.");
        SafePayloadPath(Executable);
        if (!Executable.Equals("BLACKBIRD.exe", StringComparison.Ordinal))
            throw new InvalidDataException("Неизвестная точка запуска.");
        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long size = 0;
        foreach (var file in Files)
        {
            SafePayloadPath(file.Path);
            if (!paths.Add(file.Path) || file.Size < 0 || file.Size > MaxExpandedBytes || file.Sha256 is null ||
                file.Sha256.Length != 64 || !file.Sha256.All(Uri.IsHexDigit))
                throw new InvalidDataException("Некорректный список файлов пакета.");
            size = checked(size + file.Size);
            if (size > MaxExpandedBytes) throw new InvalidDataException("Пакет превышает допустимый размер.");
        }
        // Prevent a file from being used as an ancestor directory (including case aliases).
        foreach (var file in Files)
        {
            var path = file.Path;
            for (var slash = path.IndexOf('/'); slash >= 0; slash = path.IndexOf('/', slash + 1))
                if (paths.Contains(path[..slash])) throw new InvalidDataException("Пересекающиеся пути в пакете.");
        }
        if (!paths.Contains(Executable) || !paths.Contains("resources/app.asar"))
            throw new InvalidDataException("В пакете отсутствуют файлы приложения.");
    }

    public static void SafePayloadPath(string path)
    {
        if (string.IsNullOrEmpty(path) || path.Length > 220 || path.Any(c => c < 32 || "\\:*?\"<>|".Contains(c)))
            throw new InvalidDataException("Недопустимый путь в пакете.");
        foreach (var part in path.Split('/'))
        {
            var stem = part.Split('.')[0].ToUpperInvariant();
            if (part.Length == 0 || part is "." or ".." || part.EndsWith('.') || part.EndsWith(' ') ||
                stem is "CON" or "PRN" or "AUX" or "NUL" ||
                (stem.Length == 4 && (stem.StartsWith("COM") || stem.StartsWith("LPT")) && "123456789¹²³".Contains(stem[3])))
                throw new InvalidDataException("Недопустимое имя файла в пакете.");
        }
    }

    public static async Task<PayloadManifest> CreateAsync(string directory, string version, CancellationToken token = default)
    {
        SafeDirectories.RejectLinks(directory);
        var files = new List<PayloadFile>();
        foreach (var path in EnumerateRegularFiles(directory).Order(StringComparer.Ordinal))
        {
            token.ThrowIfCancellationRequested();
            var relative = System.IO.Path.GetRelativePath(directory, path).Replace('\\', '/');
            SafePayloadPath(relative);
            await using var stream = File.OpenRead(path);
            files.Add(new(relative, stream.Length, Convert.ToHexString(await SHA256.HashDataAsync(stream, token)).ToLowerInvariant()));
        }
        var manifest = new PayloadManifest(1, ProductId, version, "BLACKBIRD.exe", files.ToArray());
        manifest.Validate();
        return manifest;
    }

    private static IEnumerable<string> EnumerateRegularFiles(string directory)
    {
        foreach (var entry in Directory.EnumerateFileSystemEntries(directory))
        {
            SafeDirectories.RejectLinks(entry);
            if (Directory.Exists(entry)) foreach (var file in EnumerateRegularFiles(entry)) yield return file;
            else yield return entry;
        }
    }
}

public static class SafeDirectories
{
    public static void RejectLinks(string path)
    {
        var current = System.IO.Path.GetFullPath(path);
        while (!string.IsNullOrEmpty(current))
        {
            // GetAttributes also detects a dangling symlink, unlike File.Exists.
            try
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("Установка через символические ссылки запрещена.");
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            current = System.IO.Path.GetDirectoryName(current);
        }
    }

    public static void DeleteOwnedTree(string path)
    {
        if (!Directory.Exists(path)) return;
        RejectLinks(path);
        foreach (var entry in Directory.EnumerateFileSystemEntries(path))
        {
            RejectLinks(entry);
            if (Directory.Exists(entry)) DeleteOwnedTree(entry);
            else File.Delete(entry);
        }
        Directory.Delete(path);
    }
}
