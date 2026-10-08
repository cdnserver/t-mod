using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;

namespace Blackbird.Setup.Core;

public sealed record InstallationReceipt(int Schema, string Product, string Version, string Transaction, string Executable);
internal sealed record InstallationJournal(string Product, string Transaction, string Stage, bool HadApp);
internal sealed record RemovalJournal(string Product, string Transaction, string Directory);
public sealed record InstallProgress(string Stage, string Detail, double Fraction);
public sealed record InstallResult(string Executable, string Version, bool CleanupPending);

/// <summary>Owns only root/app, its receipt and transaction directories. Account data is never touched.</summary>
public sealed class Installation
{
    public const string ReceiptName = ".blackbird-install.json";
    private const string JournalName = ".blackbird-transaction.json";
    private const string RemovalName = ".blackbird-removal.json";
    private readonly string root;
    public Installation(string installRoot)
    {
        root = Path.GetFullPath(installRoot).TrimEnd(Path.DirectorySeparatorChar);
        if (Path.GetDirectoryName(root) is not { Length: > 0 } || root == Path.GetPathRoot(root))
            throw new InvalidDataException("Нельзя установить приложение в корень диска.");
        SafeDirectories.RejectLinks(root);
    }
    public string Root => root;
    private string AppPath => Path.Combine(root, "app");
    private string PreviousPath => Path.Combine(root, ".previous");
    private string ReceiptPath => Path.Combine(root, ReceiptName);
    private string JournalPath => Path.Combine(root, JournalName);
    private string RemovalPath => Path.Combine(root, RemovalName);

    public bool CanUninstall() => ReadReceipt() is not null || ReadRemovalJournal() is not null;

    public InstallationReceipt? ReadReceipt()
    {
        SafeDirectories.RejectLinks(ReceiptPath);
        if (!File.Exists(ReceiptPath)) return null;
        var receipt = JsonSerializer.Deserialize<InstallationReceipt>(File.ReadAllText(ReceiptPath), PayloadManifest.Json);
        if (receipt is null || receipt.Schema != 1 || receipt.Product != PayloadManifest.ProductId || receipt.Executable != "BLACKBIRD.exe" || !Guid.TryParseExact(receipt.Transaction, "N", out _))
            throw new InvalidDataException("Каталог не принадлежит установщику Blackbird.");
        return receipt;
    }

    public async Task<InstallResult> InstallAsync(Stream archive, PayloadManifest manifest, IProgress<InstallProgress>? progress = null,
        Func<Task>? register = null, Func<Task>? restoreRegistration = null, CancellationToken token = default, long additionalBytes = 0)
    {
        manifest.Validate();
        if (additionalBytes < 0 || additionalBytes > PayloadManifest.MaxExpandedBytes) throw new ArgumentOutOfRangeException(nameof(additionalBytes));
        using var gate = Acquire();
        Recover();
        if (ReadRemovalJournal() is not null) throw new IOException("Предыдущее удаление не завершено. Сначала повторите удаление Blackbird.");
        var oldReceipt = ReadReceipt();
        ValidateOwnership(oldReceipt);
        var tx = Guid.NewGuid().ToString("N");
        var stage = Path.Combine(root, ".stage-" + tx);
        var activated = false;
        var registrationStarted = false;
        // Keep the original receipt until the replacement and registration both succeed.
        var oldReceiptBytes = File.Exists(ReceiptPath) ? await File.ReadAllBytesAsync(ReceiptPath, token) : null;
        try
        {
            progress?.Report(new("Проверка", "Подготавливаем безопасную установку", .02));
            CheckSpace(checked(manifest.Files.Sum(file => file.Size) + additionalBytes));
            // Journal BEFORE extraction, so even a process crash during decompression is recoverable.
            AtomicWrite(JournalPath, new InstallationJournal(PayloadManifest.ProductId, tx, Path.GetFileName(stage), Directory.Exists(AppPath)));
            Directory.CreateDirectory(stage);
            await ExtractVerifiedAsync(archive, manifest, stage, progress, token);
            token.ThrowIfCancellationRequested();
            // Cancellation is not observed during commit: finish it or roll back as one unit.
            progress?.Report(new("Установка", "Переключаем приложение на новую версию", .9));
            if (Directory.Exists(AppPath)) Directory.Move(AppPath, PreviousPath);
            Directory.Move(stage, AppPath);
            activated = true;
            if (register is not null) { registrationStarted = true; await register(); }
            AtomicWrite(ReceiptPath, new InstallationReceipt(1, PayloadManifest.ProductId, manifest.Version, tx, manifest.Executable));
        }
        catch (Exception installError)
        {
            try
            {
                if (registrationStarted && restoreRegistration is not null) await restoreRegistration();
                if (activated) SafeDirectories.DeleteOwnedTree(AppPath);
                if (Directory.Exists(PreviousPath)) Directory.Move(PreviousPath, AppPath);
                if (oldReceiptBytes is not null) File.WriteAllBytes(ReceiptPath, oldReceiptBytes);
                else if (File.Exists(ReceiptPath)) File.Delete(ReceiptPath);
                SafeDirectories.DeleteOwnedTree(stage);
                if (File.Exists(JournalPath)) File.Delete(JournalPath);
            }
            catch (Exception rollbackError)
            {
                // Leave the journal and previous directory for the next recovery attempt.
                throw new AggregateException("Не удалось завершить откат. Не удаляйте папку установки; запустите установщик снова.", installError, rollbackError);
            }
            throw;
        }
        // Commit is durable. A cleanup failure must NOT roll back a working installation.
        var cleanupPending = false;
        try { SafeDirectories.DeleteOwnedTree(PreviousPath); File.Delete(JournalPath); }
        catch (IOException) { cleanupPending = true; }
        catch (UnauthorizedAccessException) { cleanupPending = true; }
        progress?.Report(new("Готово", "Blackbird установлен. Данные аккаунта сохранены", 1));
        return new(Path.Combine(AppPath, manifest.Executable), manifest.Version, cleanupPending);
    }

    public void Uninstall(Action? unregister = null)
    {
        using var gate = Acquire();
        Recover();
        var journal = ReadRemovalJournal(promote: true);
        if (journal is null)
        {
            var receipt = ReadReceipt() ?? throw new InvalidDataException("Установка Blackbird не найдена.");
            ValidateOwnership(receipt);
            var tx = Guid.NewGuid().ToString("N");
            journal = new(PayloadManifest.ProductId, tx, ".removing-" + tx);
            AtomicWrite(RemovalPath, journal);
        }
        var removing = Path.Combine(root, journal.Directory);
        ValidateTree(AppPath); ValidateTree(removing);
        if (Directory.Exists(AppPath))
        {
            if (Directory.Exists(removing)) throw new IOException("Неоднозначное состояние удаления. Каталоги сохранены для проверки.");
            Directory.Move(AppPath, removing);
        }
        // Keep the receipt and Windows uninstall entry until all managed files are gone.
        // On an interrupted/failed deletion, running Uninstall again resumes this directory.
        SafeDirectories.DeleteOwnedTree(removing);
        SafeDirectories.DeleteOwnedTree(PreviousPath);
        unregister?.Invoke();
        File.Delete(ReceiptPath);
        // Keep the intent until the native worker has also removed its setup copy.
        // This lets an antivirus/file-lock failure at that final step be retried.
    }

    public void CompleteUninstall()
    {
        using var gate = Acquire();
        var journal = ReadRemovalJournal(promote: true) ?? throw new InvalidDataException("Журнал удаления не найден.");
        if (ReadReceipt() is not null || Directory.Exists(AppPath) || Directory.Exists(PreviousPath) || Directory.Exists(Path.Combine(root, journal.Directory)) || File.Exists(Path.Combine(root, "Blackbird.Setup.exe")))
            throw new IOException("Файлы приложения ещё не удалены.");
        File.Delete(RemovalPath);
    }

    private RemovalJournal? ReadRemovalJournal(bool promote = false)
    {
        SafeDirectories.RejectLinks(RemovalPath);
        SafeDirectories.RejectLinks(RemovalPath + ".new");
        var path = File.Exists(RemovalPath) ? RemovalPath : RemovalPath + ".new";
        if (!File.Exists(path)) return null;
        var journal = JsonSerializer.Deserialize<RemovalJournal>(File.ReadAllText(path), PayloadManifest.Json);
        if (journal is null || journal.Product != PayloadManifest.ProductId || !Guid.TryParseExact(journal.Transaction, "N", out _) || journal.Directory != ".removing-" + journal.Transaction)
            throw new InvalidDataException("Повреждён журнал удаления. Файлы не удалены.");
        // A flushed but not yet renamed journal also proves a resumable deletion intent.
        if (promote && path != RemovalPath) File.Move(path, RemovalPath);
        return journal;
    }

    private FileStream Acquire()
    {
        SafeDirectories.RejectLinks(root);
        var parent = Path.GetDirectoryName(root)!;
        Directory.CreateDirectory(parent);
        var lockPath = root + ".setup-lock";
        SafeDirectories.RejectLinks(lockPath);
        FileStream gate;
        try { gate = new(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
        catch (IOException e) { throw new IOException("Уже работает другой установщик Blackbird. Дождитесь его завершения.", e); }
        try { Directory.CreateDirectory(root); return gate; }
        catch { gate.Dispose(); throw; }
    }

    private void ValidateOwnership(InstallationReceipt? receipt)
    {
        SafeDirectories.RejectLinks(root);
        if (receipt is not null) { ValidateTree(AppPath); return; }
        if (Directory.EnumerateFileSystemEntries(root).Any())
            throw new InvalidDataException("Папка занята другими файлами. Выберите пустую папку; старый клиент автоматически не удаляется.");
    }

    private static void ValidateTree(string path)
    {
        SafeDirectories.RejectLinks(path);
        if (!Directory.Exists(path)) return;
        foreach (var entry in Directory.EnumerateFileSystemEntries(path))
        {
            SafeDirectories.RejectLinks(entry);
            if (Directory.Exists(entry)) ValidateTree(entry);
        }
    }

    private void Recover()
    {
        SafeDirectories.RejectLinks(JournalPath);
        SafeDirectories.RejectLinks(JournalPath + ".new");
        if (!File.Exists(JournalPath) && File.Exists(JournalPath + ".new"))
        {
            // A crash can happen between flushing the initial journal and its atomic rename.
            var pending = JsonSerializer.Deserialize<InstallationJournal>(File.ReadAllText(JournalPath + ".new"), PayloadManifest.Json);
            ValidateJournal(pending);
            File.Move(JournalPath + ".new", JournalPath);
        }
        if (!File.Exists(JournalPath))
        {
            if (Directory.Exists(PreviousPath)) throw new InvalidDataException("Найдена резервная версия без журнала. Требуется проверка каталога установки.");
            return;
        }
        var journal = JsonSerializer.Deserialize<InstallationJournal>(File.ReadAllText(JournalPath), PayloadManifest.Json) ?? throw new InvalidDataException("Пустой журнал установки.");
        ValidateJournal(journal);
        var stage = Path.Combine(root, journal.Stage);
        var receipt = ReadReceipt();
        ValidateTree(AppPath); ValidateTree(PreviousPath); ValidateTree(stage);
        if (receipt?.Transaction == journal.Transaction)
            SafeDirectories.DeleteOwnedTree(PreviousPath);
        else if (Directory.Exists(PreviousPath))
        {
            SafeDirectories.DeleteOwnedTree(AppPath);
            Directory.Move(PreviousPath, AppPath);
        }
        else if (!journal.HadApp) SafeDirectories.DeleteOwnedTree(AppPath);
        else if (!Directory.Exists(AppPath)) throw new InvalidDataException("Предыдущая версия недоступна. Журнал сохранён.");
        SafeDirectories.DeleteOwnedTree(stage);
        SafeDirectories.RejectLinks(ReceiptPath + ".new");
        File.Delete(ReceiptPath + ".new");
        File.Delete(JournalPath);
    }

    private static void ValidateJournal(InstallationJournal? journal)
    {
        if (journal is null || journal.Product != PayloadManifest.ProductId || !Guid.TryParseExact(journal.Transaction, "N", out _) || journal.Stage != ".stage-" + journal.Transaction)
            throw new InvalidDataException("Повреждён журнал установки. Файлы не удалены.");
    }

    private void CheckSpace(long expanded)
    {
        var drive = new DriveInfo(Path.GetPathRoot(root)!);
        if (drive.IsReady && drive.AvailableFreeSpace < expanded + 128L * 1024 * 1024)
            throw new IOException("Для установки недостаточно свободного места.");
    }

    private static void AtomicWrite<T>(string path, T value)
    {
        var temporary = path + ".new";
        SafeDirectories.RejectLinks(temporary);
        // Unlink a stale temporary name; never truncate through an existing hard link.
        File.Delete(temporary);
        using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        { JsonSerializer.Serialize(stream, value, PayloadManifest.Json); stream.Flush(true); }
        File.Move(temporary, path, true);
    }

    private static async Task ExtractVerifiedAsync(Stream stream, PayloadManifest manifest, string stage, IProgress<InstallProgress>? progress, CancellationToken token)
    {
        using var zip = new ZipArchive(stream, ZipArchiveMode.Read, leaveOpen: true);
        var expected = manifest.Files.ToDictionary(file => file.Path, StringComparer.OrdinalIgnoreCase);
        if (zip.Entries.Count != expected.Count) throw new InvalidDataException("Состав архива не совпадает с манифестом.");
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var total = Math.Max(1, manifest.Files.Sum(file => file.Size));
        long written = 0;
        long lastReport = 0;
        foreach (var entry in zip.Entries)
        {
            token.ThrowIfCancellationRequested();
            PayloadManifest.SafePayloadPath(entry.FullName);
            if (!seen.Add(entry.FullName) || !expected.TryGetValue(entry.FullName, out var file) || entry.Length != file.Size ||
                ((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000)
                throw new InvalidDataException("Архив содержит неизвестные, повторяющиеся или ссылочные файлы.");
            var destination = Path.Combine(stage, file.Path.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            SafeDirectories.RejectLinks(destination);
            await using var input = entry.Open();
            await using var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536, useAsync: true);
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            var buffer = new byte[65536];
            long size = 0;
            int count;
            while ((count = await input.ReadAsync(buffer, token)) != 0)
            {
                size += count;
                if (size > file.Size) throw new InvalidDataException("Размер распакованного файла не совпадает с манифестом.");
                hash.AppendData(buffer, 0, count);
                await output.WriteAsync(buffer.AsMemory(0, count), token);
                written += count;
            }
            if (size != file.Size || !Convert.ToHexString(hash.GetHashAndReset()).Equals(file.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Проверка целостности не пройдена. Установленная версия не изменена.");
            await output.FlushAsync(token);
            output.Flush(true);
            if (Environment.TickCount64 - lastReport > 50 || written == total)
            {
                lastReport = Environment.TickCount64;
                progress?.Report(new("Установка", "Распаковываем и проверяем файлы приложения", .05 + .8 * written / total));
            }
        }
    }
}
