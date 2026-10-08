using System.IO;
using System.Windows;
using Blackbird.Setup.Core;

namespace Blackbird.Setup;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        try
        {
            var options = SetupOptions.Parse(e.Args);
            if (options.UninstallRoot is not null && !options.UninstallWorker)
            {
                WindowsRegistration.LaunchUninstallWorker(options.UninstallRoot);
                Shutdown(); return;
            }
            var window = new SetupWindow(options);
            MainWindow = window;
            if (!options.Silent) window.Show();
            else window.StartSilently();
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Blackbird — установка", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }
}
