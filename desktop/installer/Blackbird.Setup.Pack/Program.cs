using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using Blackbird.Setup.Core;

if (args.Length != 3) throw new ArgumentException("Usage: Blackbird.Setup.Pack <win-unpacked> <output-directory> <version>");
var source = Path.GetFullPath(args[0]);
var output = Path.GetFullPath(args[1]);
if (output.StartsWith(source + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) || output.Equals(source, StringComparison.OrdinalIgnoreCase))
    throw new ArgumentException("Payload output must be outside the application directory.");
SafeDirectories.RejectLinks(source); SafeDirectories.RejectLinks(output);
Directory.CreateDirectory(output);
// A directory-only electron-builder target may omit this updater configuration.
// The existing generic feed continues to deliver a full EXE, not a web package.
var updater = Path.Combine(source, "resources", "app-update.yml");
SafeDirectories.RejectLinks(updater);
if (!File.Exists(updater))
    File.WriteAllText(updater, "provider: generic\nurl: https://tvr.lat/api/desktop/v1/updates/blackbird\nupdaterCacheDirName: blackbird-client-updater\n");
var manifest = await PayloadManifest.CreateAsync(source, args[2]);
var zipPath = Path.Combine(output, "payload.zip");
var pendingZip = zipPath + ".new";
SafeDirectories.RejectLinks(zipPath); SafeDirectories.RejectLinks(pendingZip);
try
{
    using (var zip = new ZipArchive(new FileStream(pendingZip, FileMode.Create, FileAccess.Write, FileShare.None), ZipArchiveMode.Create))
    {
        foreach (var file in manifest.Files)
        {
            var path = Path.Combine(source, file.Path.Replace('/', Path.DirectorySeparatorChar));
            SafeDirectories.RejectLinks(path);
            var entry = zip.CreateEntry(file.Path, CompressionLevel.Optimal);
            entry.LastWriteTime = new DateTimeOffset(2000, 1, 1, 0, 0, 0, TimeSpan.Zero);
            await using var input = File.OpenRead(path);
            await using var destination = entry.Open();
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            var buffer = new byte[65536]; long written = 0; int count;
            while ((count = await input.ReadAsync(buffer)) > 0)
            { await destination.WriteAsync(buffer.AsMemory(0, count)); hash.AppendData(buffer, 0, count); written += count; }
            if (written != file.Size || !Convert.ToHexString(hash.GetHashAndReset()).Equals(file.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new IOException("Application files changed while packing; rebuild the payload.");
        }
    }
    File.Move(pendingZip, zipPath, true);
    var manifestPath = Path.Combine(output, "manifest.json");
    SafeDirectories.RejectLinks(manifestPath);
    File.WriteAllText(manifestPath, JsonSerializer.Serialize(manifest, PayloadManifest.Json));
    Console.WriteLine($"Verified payload {manifest.Version}: {manifest.Files.Length} files, {manifest.Files.Sum(file => file.Size)} bytes expanded.");
}
finally { if (File.Exists(pendingZip)) File.Delete(pendingZip); }
