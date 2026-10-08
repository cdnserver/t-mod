using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using Blackbird.Setup.Core;

namespace Blackbird.Setup;

/// <summary>Per-user integration only. Never elevates or modifies another user's installation.</summary>
internal sealed class WindowsRegistration
{
    private const string InstallKey = @"Software\Blackbird\Client";
    private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Blackbird.Client";
    private const string ProtocolKey = @"Software\Classes\blackbird";
    private static readonly string[] Keys = [InstallKey, UninstallKey, ProtocolKey];
    private readonly string root;
    private readonly bool desktopShortcut;
    private readonly Dictionary<string, RegistrySnapshot?> registry;
    private readonly Dictionary<string, byte[]?> shortcuts;
    private string? installerBackup;
    private string? stagedInstaller;
    private bool replacedInstaller;
    private readonly string executable;
    private string InstallerPath => Path.Combine(root, "Blackbird.Setup.exe");
    private string StartShortcut => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Blackbird.lnk");
    private string DesktopPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Blackbird.lnk");

    public WindowsRegistration(string installRoot, bool desktop)
    {
        root = Path.GetFullPath(installRoot);
        executable = Path.Combine(root, "app", "BLACKBIRD.exe");
        desktopShortcut = desktop;
        registry = Keys.ToDictionary(key => key, RegistrySnapshot.Capture);
        shortcuts = new[] { StartShortcut, DesktopPath }.ToDictionary(file => file, file => {
            SafeDirectories.RejectLinks(file);
            return File.Exists(file) ? File.ReadAllBytes(file) : null;
        });
    }

    public static string DefaultRoot()
    {
        using var key = Registry.CurrentUser.OpenSubKey(InstallKey);
        var saved = key?.GetValue("InstallLocation") as string;
        if (!string.IsNullOrEmpty(saved) && new Installation(saved).ReadReceipt() is not null) return saved;
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Blackbird Client");
    }

    public async Task StageInstallerAsync()
    {
        var source = Environment.ProcessPath ?? throw new IOException("Не удалось определить файл установщика.");
        if (Path.GetFullPath(source).Equals(InstallerPath, StringComparison.OrdinalIgnoreCase)) return;
        SafeDirectories.RejectLinks(InstallerPath);
        stagedInstaller = Path.Combine(root, ".setup-next-" + Guid.NewGuid().ToString("N"));
        await using (var input = File.OpenRead(source))
        await using (var output = new FileStream(stagedInstaller, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536, true))
        { await input.CopyToAsync(output); await output.FlushAsync(); output.Flush(true); }
        if (File.Exists(InstallerPath))
        {
            installerBackup = Path.Combine(root, ".setup-previous-" + Guid.NewGuid().ToString("N"));
            File.Move(InstallerPath, installerBackup);
        }
        File.Move(stagedInstaller, InstallerPath);
        stagedInstaller = null;
        replacedInstaller = true;
    }

    public void Apply(string version)
    {
        using (var key = Registry.CurrentUser.CreateSubKey(InstallKey)) key.SetValue("InstallLocation", root);
        using (var key = Registry.CurrentUser.CreateSubKey(UninstallKey))
        {
            key.SetValue("DisplayName", "Blackbird — Технологии Товарищества");
            key.SetValue("DisplayVersion", version);
            key.SetValue("Publisher", "Технологии Товарищества");
            key.SetValue("InstallLocation", root);
            key.SetValue("DisplayIcon", executable);
            key.SetValue("UninstallString", $"\"{InstallerPath}\" \"--uninstall-root={root}\"");
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
        using (var key = Registry.CurrentUser.CreateSubKey(ProtocolKey))
        {
            key.SetValue("", "URL:Blackbird service link"); key.SetValue("URL Protocol", "");
            using var icon = key.CreateSubKey("DefaultIcon"); icon.SetValue("", $"\"{executable}\",0");
            using var command = key.CreateSubKey(@"shell\open\command"); command.SetValue("", $"\"{executable}\" \"%1\"");
        }
        CreateShortcut(StartShortcut);
        if (desktopShortcut) CreateShortcut(DesktopPath);
        else RemoveOwnedShortcut(DesktopPath, executable);
    }

    public void Restore()
    {
        foreach (var pair in registry) RegistrySnapshot.Restore(pair.Key, pair.Value);
        foreach (var pair in shortcuts)
        {
            SafeDirectories.RejectLinks(pair.Key);
            if (pair.Value is null) File.Delete(pair.Key); else File.WriteAllBytes(pair.Key, pair.Value);
        }
        if (replacedInstaller) File.Delete(InstallerPath);
        if (installerBackup is not null && File.Exists(installerBackup)) File.Move(installerBackup, InstallerPath, true);
        if (stagedInstaller is not null) File.Delete(stagedInstaller);
    }

    public void Finish()
    {
        // Failure to remove the backup must not change the successful app/receipt commit.
        if (installerBackup is not null) { try { File.Delete(installerBackup); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
    }

    public static void Remove(string root)
    {
        var executable = Path.Combine(root, "app", "BLACKBIRD.exe");
        using (var key = Registry.CurrentUser.OpenSubKey(InstallKey))
            if (string.Equals(key?.GetValue("InstallLocation") as string, root, StringComparison.OrdinalIgnoreCase)) Registry.CurrentUser.DeleteSubKeyTree(InstallKey, false);
        using (var key = Registry.CurrentUser.OpenSubKey(UninstallKey))
            if (string.Equals(key?.GetValue("InstallLocation") as string, root, StringComparison.OrdinalIgnoreCase)) Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
        using (var key = Registry.CurrentUser.OpenSubKey(ProtocolKey + @"\shell\open\command"))
            if (string.Equals(key?.GetValue("") as string, $"\"{executable}\" \"%1\"", StringComparison.OrdinalIgnoreCase)) Registry.CurrentUser.DeleteSubKeyTree(ProtocolKey, false);
        RemoveOwnedShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Blackbird.lnk"), executable);
        RemoveOwnedShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Blackbird.lnk"), executable);
    }

    private void CreateShortcut(string path)
    {
        SafeDirectories.RejectLinks(path);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        WithShortcut(path, shortcut => {
            shortcut.TargetPath = executable;
            shortcut.WorkingDirectory = Path.GetDirectoryName(executable);
            shortcut.Description = "Blackbird — Технологии Товарищества";
            shortcut.IconLocation = executable + ",0";
            shortcut.Save();
        });
    }

    private static void RemoveOwnedShortcut(string path, string executable)
    {
        SafeDirectories.RejectLinks(path);
        if (!File.Exists(path)) return;
        var owned = false;
        WithShortcut(path, shortcut => owned = string.Equals((string)shortcut.TargetPath, executable, StringComparison.OrdinalIgnoreCase));
        if (owned) File.Delete(path);
    }

    private static void WithShortcut(string path, Action<dynamic> action)
    {
        var type = Type.GetTypeFromProgID("WScript.Shell") ?? throw new IOException("Компонент ярлыков Windows недоступен.");
        dynamic shell = Activator.CreateInstance(type)!;
        dynamic? shortcut = null;
        try { shortcut = shell.CreateShortcut(path); action(shortcut); }
        finally
        {
            if (shortcut is not null) Marshal.FinalReleaseComObject(shortcut);
            Marshal.FinalReleaseComObject(shell);
        }
    }

    public static void LaunchUninstallWorker(string root)
    {
        if (!new Installation(root).CanUninstall()) throw new IOException("Установка Blackbird не найдена.");
        // Never delete the running executable or schedule deletion through a startup hook.
        var temporary = Path.Combine(Path.GetTempPath(), "Blackbird.Setup", Guid.NewGuid().ToString("N"));
        SafeDirectories.RejectLinks(temporary);
        Directory.CreateDirectory(temporary);
        var copy = Path.Combine(temporary, "Blackbird.Setup.exe");
        File.Copy(Environment.ProcessPath!, copy);
        var start = new ProcessStartInfo(copy) { UseShellExecute = false };
        start.ArgumentList.Add("--uninstall-worker");
        start.ArgumentList.Add("--uninstall-root=" + root);
        start.ArgumentList.Add("--parent-pid=" + Environment.ProcessId);
        _ = Process.Start(start) ?? throw new IOException("Не удалось открыть удаление Blackbird.");
    }

    private sealed record RegistrySnapshot(Dictionary<string, (object Value, RegistryValueKind Kind)> Values, Dictionary<string, RegistrySnapshot> Children)
    {
        public static RegistrySnapshot? Capture(string path)
        {
            using var key = Registry.CurrentUser.OpenSubKey(path);
            return key is null ? null : CaptureKey(key);
        }
        private static RegistrySnapshot CaptureKey(RegistryKey key) => new(
            key.GetValueNames().ToDictionary(name => name, name => (key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames)!, key.GetValueKind(name))),
            key.GetSubKeyNames().ToDictionary(name => name, name => { using var child = key.OpenSubKey(name)!; return CaptureKey(child); }));
        public static void Restore(string path, RegistrySnapshot? state)
        {
            Registry.CurrentUser.DeleteSubKeyTree(path, false);
            if (state is null) return;
            using var key = Registry.CurrentUser.CreateSubKey(path);
            Write(key, state);
        }
        private static void Write(RegistryKey key, RegistrySnapshot state)
        {
            foreach (var pair in state.Values) key.SetValue(pair.Key, pair.Value.Value, pair.Value.Kind);
            foreach (var pair in state.Children) { using var child = key.CreateSubKey(pair.Key); Write(child, pair.Value); }
        }
    }
}
