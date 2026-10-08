namespace Blackbird.Setup.Core;

public sealed record SetupOptions(bool Preview, bool Silent, bool Updated, bool ForceRun, string? Directory, string? UninstallRoot, bool UninstallWorker, int ParentPid)
{
    public static SetupOptions Parse(string[] args)
    {
        bool preview = false, silent = false, updated = false, run = false, worker = false;
        string? directory = null, uninstallRoot = null;
        int pid = 0;
        foreach (var arg in args)
        {
            if (arg == "--preview") preview = true;
            else if (arg.Equals("/S", StringComparison.OrdinalIgnoreCase)) silent = true;
            else if (arg == "--updated") updated = true;
            else if (arg == "--force-run") run = true;
            else if (arg.StartsWith("/D=", StringComparison.OrdinalIgnoreCase) && arg.Length > 3) directory = Path.GetFullPath(arg[3..]);
            else if (arg.StartsWith("--uninstall-root=") && arg.Length > 17) uninstallRoot = Path.GetFullPath(arg[17..]);
            else if (arg == "--uninstall-worker") worker = true;
            else if (arg.StartsWith("--parent-pid=") && int.TryParse(arg[13..], out var parent) && parent > 0) pid = parent;
            else throw new ArgumentException("Неизвестный параметр установщика.");
        }
        if (preview && (silent || updated || run || uninstallRoot is not null || directory is not null || worker || pid > 0))
            throw new ArgumentException("Предпросмотр не может менять установку.");
        if (worker && (uninstallRoot is null || pid == 0)) throw new ArgumentException("Некорректный запуск удаления.");
        if (pid > 0 && !worker) throw new ArgumentException("Некорректный процесс удаления.");
        if (uninstallRoot is not null && (updated || directory is not null || silent || run)) throw new ArgumentException("Несовместимые параметры удаления.");
        return new(preview, silent, updated, run, directory, uninstallRoot, worker, pid);
    }
}
