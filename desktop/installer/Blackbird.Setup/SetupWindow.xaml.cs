using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Animation;
using Microsoft.Win32;
using Blackbird.Setup.Core;

namespace Blackbird.Setup;

public partial class SetupWindow : Window
{
    private readonly SetupOptions options;
    private string installRoot;
    private bool busy, complete, canCancel, removed;
    private string? launchPath;
    private CancellationTokenSource? cancellation;

    public SetupWindow(SetupOptions arguments)
    {
        InitializeComponent();
        options = arguments;
        installRoot = options.UninstallRoot ?? options.Directory ?? WindowsRegistration.DefaultRoot();
        LocationLabel.Text = installRoot;
        if (options.Preview) { Eyebrow.Text = "ПРЕДПРОСМОТР · ФАЙЛЫ НЕ ИЗМЕНЯЮТСЯ"; Footnote.Text = "Демонстрация интерфейса установщика.\nНичего не устанавливается."; }
        if (options.UninstallRoot is not null)
        {
            Heading.Text = "Удалить\nBlackbird?";
            Description.Text = "Будут удалены только файлы клиента и его ярлыки. Ваш аккаунт, настройки и данные сервисов сохранятся.";
            BrowseButton.IsEnabled = false; DesktopShortcut.Visibility = Visibility.Collapsed;
            ActionButton.Content = "Удалить клиент";
        }
    }

    private void WindowLoaded(object sender, RoutedEventArgs e)
    {
        Scene.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(700)) { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
        Orbit.BeginAnimation(OpacityProperty, new DoubleAnimation(.25, .65, TimeSpan.FromSeconds(5)) { AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever });
        if (options.Updated) _ = InstallAsync();
    }
    public void StartSilently() => _ = InstallAsync();
    private void DragWindow(object sender, MouseButtonEventArgs e) { if (e.ChangedButton == MouseButton.Left) DragMove(); }
    private void Minimize(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    private void CloseWindow(object sender, RoutedEventArgs e) => Close();
    private void WindowClosing(object? sender, CancelEventArgs e)
    {
        if (busy) { e.Cancel = true; if (canCancel) cancellation?.Cancel(); }
        else Application.Current.Shutdown(complete || removed ? 0 : 1);
    }
    private void ChooseDirectory(object sender, RoutedEventArgs e)
    {
        var picker = new OpenFolderDialog { Title = "Выберите расположение Blackbird", Multiselect = false };
        if (picker.ShowDialog(this) != true) return;
        // The picker chooses a parent, not an arbitrary existing directory to replace.
        installRoot = Path.Combine(picker.FolderName, "Blackbird Client");
        LocationLabel.Text = installRoot;
    }
    private async void PrimaryAction(object sender, RoutedEventArgs e)
    {
        if (busy) { if (canCancel) cancellation?.Cancel(); return; }
        if (complete)
        {
            try
            {
                if (launchPath is not null && !options.Preview) Process.Start(new ProcessStartInfo(launchPath) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(launchPath)! });
                Application.Current.Shutdown();
            }
            catch (Exception error) { LogFailure(error); ErrorText.Text = "Не удалось открыть Blackbird. Проверьте файл приложения и повторите запуск."; ErrorText.Visibility = Visibility.Visible; }
            return;
        }
        if (removed) { Application.Current.Shutdown(); return; }
        if (options.UninstallRoot is not null) await UninstallAsync(); else await InstallAsync();
    }

    private void SetBusy(bool value)
    {
        busy = value;
        BrowseButton.IsEnabled = !value;
        DesktopShortcut.IsEnabled = !value;
        CloseButton.IsEnabled = !value;
        ProgressPanel.Visibility = value || complete ? Visibility.Visible : Visibility.Collapsed;
        ErrorText.Visibility = Visibility.Collapsed;
        ActionButton.Content = value ? "Отменить" : "Повторить  →";
    }

    private void Report(InstallProgress progress)
    {
        // Ignore queued progress after failure/success so it cannot disable Retry/Open.
        if (!busy) return;
        StageLabel.Text = progress.Stage; DetailLabel.Text = progress.Detail;
        var fraction = Math.Clamp(progress.Fraction, 0, 1);
        PercentLabel.Text = $"{Math.Round(fraction * 100)}%";
        ProgressLine.BeginAnimation(System.Windows.Controls.Primitives.RangeBase.ValueProperty,
            new DoubleAnimation(ProgressLine.Value, fraction, TimeSpan.FromMilliseconds(180)));
        canCancel = fraction < .9;
        ActionButton.IsEnabled = canCancel;
    }

    private async Task InstallAsync()
    {
        if (busy) return;
        SetBusy(true); canCancel = true; cancellation = new();
        try
        {
            var progress = new Progress<InstallProgress>(Report);
            if (options.Preview)
            {
                for (var step = 0; step <= 100; step += 2)
                {
                    cancellation.Token.ThrowIfCancellationRequested();
                    Report(new("Предпросмотр", "Демонстрация установки — файлы не изменяются", step / 100d));
                    await Task.Delay(50, cancellation.Token);
                }
            }
            else
            {
                var assembly = Assembly.GetExecutingAssembly();
                using var manifestStream = assembly.GetManifestResourceStream("Blackbird.Manifest") ?? throw new IOException("В установщике нет пакета приложения. Это исходный предпросмотр, а не релиз.");
                var manifest = PayloadManifest.Read(manifestStream);
                await WaitForClientExitAsync(cancellation.Token);
                var desktop = DesktopShortcut.IsChecked == true;
                var registration = new WindowsRegistration(installRoot, desktop);
                var installer = new Installation(installRoot);
                var result = await Task.Run(async () => {
                    using var payload = assembly.GetManifestResourceStream("Blackbird.Payload") ?? throw new IOException("Пакет приложения отсутствует.");
                    return await installer.InstallAsync(payload, manifest, progress,
                        register: async () => { await registration.StageInstallerAsync(); await Dispatcher.InvokeAsync(() => registration.Apply(manifest.Version)); },
                        restoreRegistration: () => Dispatcher.InvokeAsync(registration.Restore).Task,
                        token: cancellation.Token,
                        additionalBytes: Path.GetFullPath(Environment.ProcessPath!).Equals(Path.Combine(installRoot, "Blackbird.Setup.exe"), StringComparison.OrdinalIgnoreCase) ? 0 : new FileInfo(Environment.ProcessPath!).Length);
                });
                registration.Finish(); launchPath = result.Executable;
                Footnote.Text = result.CleanupPending ? "Приложение установлено. Резервная копия\nбудет очищена при следующем обновлении." : "Всё готово. Войдите в свой аккаунт\nили создайте его в клиенте.";
            }
            Report(new("Готово", options.Preview ? "Предпросмотр завершён" : "Blackbird установлен", 1));
            complete = true; busy = false; canCancel = false;
            Heading.Text = "Пространство\nготово.";
            Description.Text = options.Preview ? "Так выглядит завершение установки. Ни один файл приложения не был изменён." : "Blackbird установлен. Откройте клиент и продолжите работу с вашими сервисами.";
            LocationPanel.Visibility = DesktopShortcut.Visibility = Visibility.Collapsed;
            ActionButton.Content = options.Preview ? "Закрыть предпросмотр" : "Открыть Blackbird  →";
            ActionButton.IsEnabled = CloseButton.IsEnabled = true;
            if (options.Silent || options.ForceRun)
            {
                if (options.ForceRun && launchPath is not null) Process.Start(new ProcessStartInfo(launchPath) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(launchPath)! });
                Application.Current.Shutdown();
            }
        }
        catch (OperationCanceledException) { ShowFailure("Установка отменена. Предыдущая версия не изменена."); }
        catch (Exception error) { LogFailure(error); ShowFailure(error.Message); }
        finally { cancellation?.Dispose(); cancellation = null; }
    }

    private async Task UninstallAsync()
    {
        SetBusy(true); canCancel = false; ActionButton.IsEnabled = false;
        try
        {
            if (options.ParentPid > 0)
            {
                try { using var parent = Process.GetProcessById(options.ParentPid); using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15)); await parent.WaitForExitAsync(timeout.Token); }
                catch (ArgumentException) { }
            }
            await WaitForClientExitAsync(CancellationToken.None);
            var installer = new Installation(installRoot);
            var registration = new WindowsRegistration(installRoot, false);
            // The worker is outside the installation; it can remove the owned setup copy.
            await Task.Run(() => installer.Uninstall(() => Dispatcher.Invoke(() => {
                try { WindowsRegistration.Remove(installRoot); }
                catch { registration.Restore(); throw; }
            })));
            var setup = Path.Combine(installRoot, "Blackbird.Setup.exe"); SafeDirectories.RejectLinks(setup); File.Delete(setup);
            installer.CompleteUninstall();
            try { if (!Directory.EnumerateFileSystemEntries(installRoot).Any()) Directory.Delete(installRoot); }
            catch (IOException) { } catch (UnauthorizedAccessException) { }
            busy = false; removed = true; Heading.Text = "До встречи.";
            Description.Text = "Клиент удалён. Ваш аккаунт и личные данные остались в сохранности.";
            LocationPanel.Visibility = ProgressPanel.Visibility = Visibility.Collapsed;
            ActionButton.Content = "Закрыть"; ActionButton.IsEnabled = CloseButton.IsEnabled = true;
        }
        catch (Exception error) { LogFailure(error); ShowFailure(error.Message); }
    }

    private async Task WaitForClientExitAsync(CancellationToken token)
    {
        var executable = Path.Combine(installRoot, "app", "BLACKBIRD.exe");
        var deadline = DateTime.UtcNow.AddSeconds(options.Updated ? 30 : 2);
        while (true)
        {
            token.ThrowIfCancellationRequested();
            var running = false;
            foreach (var process in Process.GetProcessesByName("BLACKBIRD"))
                using (process)
                {
                    try { if (string.Equals(process.MainModule?.FileName, executable, StringComparison.OrdinalIgnoreCase)) running = true; }
                    catch (Win32Exception) { } catch (InvalidOperationException) { }
                }
            if (!running) return;
            if (DateTime.UtcNow >= deadline) throw new IOException("Закройте Blackbird через меню в трее и нажмите «Повторить». Принудительное завершение не используется.");
            await Task.Delay(500, token);
        }
    }

    private void ShowFailure(string message)
    {
        SetBusy(false); canCancel = false;
        ErrorText.Text = message; ErrorText.Visibility = Visibility.Visible;
        ActionButton.IsEnabled = CloseButton.IsEnabled = true;
        if (options.Silent) { MessageBox.Show(message, "Blackbird — обновление не установлено", MessageBoxButton.OK, MessageBoxImage.Warning); Application.Current.Shutdown(1); }
    }
    private static void LogFailure(Exception error)
    {
        try
        {
            var directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Blackbird", "SetupLogs");
            SafeDirectories.RejectLinks(directory); Directory.CreateDirectory(directory);
            File.WriteAllText(Path.Combine(directory, DateTime.UtcNow.ToString("yyyyMMdd-HHmmss-fff") + ".log"), error.ToString());
        }
        catch (IOException) { } catch (UnauthorizedAccessException) { }
    }
}
