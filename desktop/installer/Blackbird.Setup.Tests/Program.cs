using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Blackbird.Setup.Core;

// Dependency-free portable engine tests; no registry or real application is touched.
// macOS /tmp and /var are symlinks; production intentionally rejects linked ancestors.
var sandbox = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".cache", "blackbird-setup-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(sandbox);
var passed = 0;
try
{
    await Check("fresh install validates all hashes and creates a receipt", async () => {
        var root = Root();
        var installer = new Installation(root);
        using var zip = Archive();
        var result = await installer.InstallAsync(zip, Manifest());
        Assert(result.Version == "1.0.0" && File.ReadAllText(result.Executable) == "client");
        Assert(installer.ReadReceipt()?.Product == PayloadManifest.ProductId);
    });
    await Check("replacement preserves unrelated account data", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        File.WriteAllText(Path.Combine(root, "user-preferences.txt"), "keep");
        using (var zip = Archive("new-client")) await installer.InstallAsync(zip, Manifest("new-client", "2.0.0"));
        Assert(File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "new-client");
        Assert(File.ReadAllText(Path.Combine(root, "user-preferences.txt")) == "keep");
        Assert(!Directory.Exists(Path.Combine(root, ".previous")));
    });
    await Check("hash failure leaves old files and receipt intact", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        using var bad = Archive("tampered");
        await Reject(() => installer.InstallAsync(bad, Manifest("expected", "2.0.0")));
        Assert(File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "client");
        Assert(installer.ReadReceipt()?.Version == "1.0.0");
        Assert(!Directory.EnumerateDirectories(root, ".stage-*").Any());
    });
    await Check("registration failure rolls back app and registration", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        var restored = false;
        using var update = Archive("new-client");
        await Reject(() => installer.InstallAsync(update, Manifest("new-client", "2.0.0"),
            register: () => throw new IOException("test registration failure"), restoreRegistration: () => { restored = true; return Task.CompletedTask; }));
        Assert(restored && installer.ReadReceipt()?.Version == "1.0.0");
        Assert(File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "client");
    });
    await Check("a nonempty unowned directory is never overwritten", async () => {
        var root = Root(); Directory.CreateDirectory(root);
        File.WriteAllText(Path.Combine(root, "important.txt"), "keep");
        using var zip = Archive();
        await Reject(() => new Installation(root).InstallAsync(zip, Manifest()));
        Assert(File.ReadAllText(Path.Combine(root, "important.txt")) == "keep");
    });
    await Check("cancelled extraction does not leave an installation", async () => {
        var root = Root(); using var zip = Archive();
        using var cancellation = new CancellationTokenSource(); cancellation.Cancel();
        await Reject(() => new Installation(root).InstallAsync(zip, Manifest(), token: cancellation.Token));
        Assert(!File.Exists(Path.Combine(root, Installation.ReceiptName)));
        Assert(!Directory.Exists(Path.Combine(root, "app")));
    });
    await Check("archive traversal cannot write outside staging", async () => {
        var root = Root(); using var zip = Archive(extraPath: "../escaped.txt");
        await Reject(() => new Installation(root).InstallAsync(zip, Manifest()));
        Assert(!File.Exists(Path.Combine(root, "escaped.txt")));
    });
    await Check("duplicate archive entries are rejected", async () => {
        using var zip = Archive(extraPath: "BLACKBIRD.exe");
        await Reject(() => new Installation(Root()).InstallAsync(zip, Manifest()));
    });
    await Check("Windows paths, aliases and file-directory collisions are rejected", () => {
        foreach (var path in new[] { "../a", "/absolute", "C:/file", "a\\b", "CON.txt", "aux", "LPT1", "a.", "a ", "x//a", "file:stream", "COM¹.txt" })
            RejectSync(() => PayloadManifest.SafePayloadPath(path));
        var manifest = Manifest();
        RejectSync(() => (manifest with { Files = [..manifest.Files, manifest.Files[0] with { Path = "blackbird.exe" }] }).Validate());
        RejectSync(() => (manifest with { Files = [..manifest.Files, manifest.Files[0] with { Path = "resources" }] }).Validate());
        return Task.CompletedTask;
    });
    await Check("symlinked destinations are rejected without touching their target", async () => {
        var target = Root(); Directory.CreateDirectory(target);
        var link = Root(); Directory.CreateSymbolicLink(link, target);
        try { RejectSync(() => new Installation(link)); Assert(!Directory.EnumerateFileSystemEntries(target).Any()); }
        finally { Directory.Delete(link); }
        await Task.CompletedTask;
    });
    await Check("a concurrent install is refused without touching the active app", async () => {
        var root = Root();
        using var gate = new FileStream(root + ".setup-lock", FileMode.Create, FileAccess.ReadWrite, FileShare.None);
        using var zip = Archive();
        await Reject(() => new Installation(root).InstallAsync(zip, Manifest()));
        Assert(!Directory.Exists(root));
    });
    await Check("interrupted file switch recovers the previous app before retry", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        var tx = Guid.NewGuid().ToString("N");
        Directory.Move(Path.Combine(root, "app"), Path.Combine(root, ".previous"));
        Directory.CreateDirectory(Path.Combine(root, "app"));
        File.WriteAllText(Path.Combine(root, "app", "BLACKBIRD.exe"), "partial-new-app");
        File.WriteAllText(Path.Combine(root, ".blackbird-transaction.json"), JsonSerializer.Serialize(new {
            product = PayloadManifest.ProductId, transaction = tx, stage = ".stage-" + tx, hadApp = true,
        }));
        using var bad = Archive("tampered");
        await Reject(() => installer.InstallAsync(bad, Manifest("expected", "2.0.0")));
        Assert(File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "client");
        Assert(installer.ReadReceipt()?.Version == "1.0.0");
    });
    await Check("uninstall removes only managed files, not preferences", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        File.WriteAllText(Path.Combine(root, "keep.txt"), "preferences");
        installer.Uninstall();
        Assert(!Directory.Exists(Path.Combine(root, "app")) && installer.ReadReceipt() is null);
        Assert(installer.CanUninstall());
        installer.CompleteUninstall();
        Assert(!installer.CanUninstall());
        Assert(File.ReadAllText(Path.Combine(root, "keep.txt")) == "preferences");
    });
    await Check("interrupted first extraction recovers even before the journal rename", async () => {
        var root = Root(); Directory.CreateDirectory(root);
        var tx = Guid.NewGuid().ToString("N"); var stage = Path.Combine(root, ".stage-" + tx);
        Directory.CreateDirectory(stage); File.WriteAllText(Path.Combine(stage, "partial"), "unfinished");
        File.WriteAllText(Path.Combine(root, ".blackbird-transaction.json.new"), JsonSerializer.Serialize(new {
            product = PayloadManifest.ProductId, transaction = tx, stage = ".stage-" + tx, hadApp = false,
        }));
        using var zip = Archive(); await new Installation(root).InstallAsync(zip, Manifest());
        Assert(!Directory.Exists(stage) && File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "client");
    });
    await Check("committed update recovery keeps new files instead of rolling them back", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive("new-client")) await installer.InstallAsync(zip, Manifest("new-client", "2.0.0"));
        var tx = installer.ReadReceipt()!.Transaction;
        Directory.CreateDirectory(Path.Combine(root, ".previous"));
        File.WriteAllText(Path.Combine(root, ".previous", "old-client"), "old");
        File.WriteAllText(Path.Combine(root, ".blackbird-transaction.json"), JsonSerializer.Serialize(new {
            product = PayloadManifest.ProductId, transaction = tx, stage = ".stage-" + tx, hadApp = true,
        }));
        using var bad = Archive("tampered");
        await Reject(() => installer.InstallAsync(bad, Manifest("expected", "3.0.0")));
        Assert(installer.ReadReceipt()?.Version == "2.0.0");
        Assert(File.ReadAllText(Path.Combine(root, "app", "BLACKBIRD.exe")) == "new-client");
        Assert(!Directory.Exists(Path.Combine(root, ".previous")));
    });
    await Check("unknown recovery journal cannot delete unowned files", async () => {
        var root = Root(); Directory.CreateDirectory(root);
        Directory.CreateDirectory(Path.Combine(root, "app"));
        File.WriteAllText(Path.Combine(root, "app", "keep"), "important");
        File.WriteAllText(Path.Combine(root, ".blackbird-transaction.json"), "{}");
        using var zip = Archive(); await Reject(() => new Installation(root).InstallAsync(zip, Manifest()));
        Assert(File.ReadAllText(Path.Combine(root, "app", "keep")) == "important");
    });
    await Check("manifest creation hashes actual source bytes", async () => {
        var root = Root(); Directory.CreateDirectory(Path.Combine(root, "resources"));
        File.WriteAllText(Path.Combine(root, "BLACKBIRD.exe"), "client");
        File.WriteAllText(Path.Combine(root, "resources", "app.asar"), "application");
        var manifest = await PayloadManifest.CreateAsync(root, "1.0.0");
        Assert(manifest.Files.Single(file => file.Path == "BLACKBIRD.exe") == Entry("BLACKBIRD.exe", "client"));
    });
    await Check("updater arguments work, but preview and uninstall cannot be mixed with installation", () => {
        var options = SetupOptions.Parse(["--updated", "/S", "--force-run"]);
        Assert(options.Updated && options.Silent && options.ForceRun);
        foreach (var args in new[] { new[] { "--preview", "--updated" }, new[] { "--preview", "--force-run" }, new[] { "/D=" }, new[] { "--parent-pid=1" }, new[] { "--uninstall-worker" }, new[] { "--uninstall-root=/abc", "--updated" }, new[] { "--unknown" } })
            RejectSync(() => SetupOptions.Parse(args));
        return Task.CompletedTask;
    });
    await Check("failed unregistration retains deletion intent and can be resumed", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        File.WriteAllText(Path.Combine(root, "preferences.txt"), "keep");
        RejectSync(() => installer.Uninstall(() => throw new IOException("test registry locked")));
        Assert(installer.CanUninstall() && installer.ReadReceipt() is not null);
        Assert(File.Exists(Path.Combine(root, ".blackbird-removal.json")));
        var unregistered = false;
        installer.Uninstall(() => { Assert(!Directory.Exists(Path.Combine(root, "app"))); unregistered = true; });
        installer.CompleteUninstall();
        Assert(unregistered && !installer.CanUninstall());
        Assert(File.ReadAllText(Path.Combine(root, "preferences.txt")) == "keep");
    });
    await Check("interrupted removal resumes its owned directory without deleting unknown files", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        var tx = Guid.NewGuid().ToString("N");
        Directory.Move(Path.Combine(root, "app"), Path.Combine(root, ".removing-" + tx));
        File.WriteAllText(Path.Combine(root, "keep.txt"), "unrelated");
        File.WriteAllText(Path.Combine(root, ".blackbird-removal.json.new"), JsonSerializer.Serialize(new {
            product = PayloadManifest.ProductId, transaction = tx, directory = ".removing-" + tx,
        }));
        installer.Uninstall();
        installer.CompleteUninstall();
        Assert(!Directory.Exists(Path.Combine(root, ".removing-" + tx)) && !installer.CanUninstall());
        Assert(File.ReadAllText(Path.Combine(root, "keep.txt")) == "unrelated");
    });
    await Check("an installation cannot race a pending removal", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        RejectSync(() => installer.Uninstall(() => throw new IOException("test")));
        using var update = Archive("new-client");
        await Reject(() => installer.InstallAsync(update, Manifest("new-client", "2.0.0")));
        Assert(installer.CanUninstall());
        installer.Uninstall();
    });
    await Check("failed final setup cleanup remains resumable after app removal", async () => {
        var root = Root(); var installer = new Installation(root);
        using (var zip = Archive()) await installer.InstallAsync(zip, Manifest());
        var helper = Path.Combine(root, "Blackbird.Setup.exe"); File.WriteAllText(helper, "owned helper");
        installer.Uninstall();
        RejectSync(installer.CompleteUninstall);
        Assert(installer.CanUninstall());
        // Simulate the next worker after the helper is no longer locked.
        installer.Uninstall(); File.Delete(helper); installer.CompleteUninstall();
        Assert(!installer.CanUninstall());
    });
    Console.WriteLine($"{passed} installer engine tests passed.");
}
finally { SafeDirectories.DeleteOwnedTree(sandbox); }

string Root() => Path.Combine(sandbox, Guid.NewGuid().ToString("N"));
async Task Check(string name, Func<Task> test) { await test(); passed++; Console.WriteLine("PASS " + name); }
static void Assert(bool value) { if (!value) throw new Exception("Assertion failed"); }
static async Task Reject(Func<Task> action) { try { await action(); } catch { return; } throw new Exception("Expected rejection"); }
static void RejectSync(Action action) { try { action(); } catch { return; } throw new Exception("Expected rejection"); }
static PayloadManifest Manifest(string client = "client", string version = "1.0.0") => new(1, PayloadManifest.ProductId, version, "BLACKBIRD.exe", [Entry("BLACKBIRD.exe", client), Entry("resources/app.asar", "application")]);
static PayloadFile Entry(string path, string contents) => new(path, Encoding.UTF8.GetByteCount(contents), Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(contents))).ToLowerInvariant());
static MemoryStream Archive(string client = "client", string? extraPath = null)
{
    var stream = new MemoryStream();
    using (var zip = new ZipArchive(stream, ZipArchiveMode.Create, true))
    {
        foreach (var item in new[] { ("BLACKBIRD.exe", client), ("resources/app.asar", "application") }.Concat(extraPath is null ? [] : new[] { (extraPath, "extra") }))
        { using var writer = new StreamWriter(zip.CreateEntry(item.Item1).Open()); writer.Write(item.Item2); }
    }
    stream.Position = 0; return stream;
}
