using System.Reflection;
using System.Text;

namespace TMod.Control;

internal static class Program
{
    private const string Version = "1.2.0";
    private static Backend _backend = null!;
    private static TerminalUi _ui = null!;
    private static ControlSettings _settings = new();
    private static SystemSnapshot _snapshot = SystemSnapshot.Offline("initializing");
    private static CancellationTokenSource _monitorCancellation = null!;

    private static async Task<int> Main(string[] args)
    {
        if (TryApplyPendingUpdate()) return 20;
        if (args.Contains("--self-test", StringComparer.OrdinalIgnoreCase))
        {
            Console.WriteLine(Backend.SelfTest() ? "TMOD_CONTROL_SELF_TEST_OK" : "TMOD_CONTROL_SELF_TEST_FAILED");
            return Backend.SelfTest() ? 0 : 1;
        }
        if (args.Contains("--version", StringComparer.OrdinalIgnoreCase))
        {
            Console.WriteLine(Version);
            return 0;
        }
        if (Console.IsInputRedirected)
        {
            Console.Error.WriteLine("T-Mod Control Center требует интерактивное окно консоли.");
            return 2;
        }

        var requestedProject = ArgumentValue(args, "--project");
        var project = Backend.FindProject(requestedProject);
        if (project is null)
        {
            Console.Error.WriteLine("Не найдена папка T-Mod. Ожидалось: %USERPROFILE%\\Desktop\\esgiel");
            Console.Error.WriteLine("Можно указать путь: T-Mod-Control.exe --project C:\\path\\to\\esgiel");
            return 3;
        }
        var persistent = ArgumentValue(args, "--persistent") ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "SGLDiscordBot");

        _settings = ControlSettings.Load(persistent);
        using var backend = new Backend(project, persistent);
        using var ui = new TerminalUi(Themes.Resolve(_settings.Theme), _settings.Animations);
        _backend = backend;
        _ui = ui;
        _monitorCancellation = new CancellationTokenSource();

        try
        {
            _ui.Intro();
            var monitor = MonitorStatus(_monitorCancellation.Token);
            ShowMainMenu();
            _monitorCancellation.Cancel();
            await monitor.WaitAsync(TimeSpan.FromSeconds(9));
            return 0;
        }
        catch (OperationCanceledException)
        {
            return 0;
        }
        catch (Exception exception)
        {
            _ui.Leave();
            Console.ForegroundColor = ConsoleColor.Red;
            Console.Error.WriteLine($"[T-MOD CONTROL] {exception.Message}");
            Console.ResetColor();
            return 1;
        }
        finally
        {
            _monitorCancellation.Dispose();
        }
    }

    private static string? ArgumentValue(string[] args, string name)
    {
        var index = Array.FindIndex(args, value => value.Equals(name, StringComparison.OrdinalIgnoreCase));
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private static async Task MonitorStatus(CancellationToken cancellation)
    {
        while (!cancellation.IsCancellationRequested)
        {
            _snapshot = await _backend.GetStatusAsync(TimeSpan.FromSeconds(8));
            try { await Task.Delay(TimeSpan.FromSeconds(12), cancellation); }
            catch (OperationCanceledException) { return; }
        }
    }

    private static IReadOnlyList<DashboardCard> DashboardCards()
    {
        static DashboardCard Contour(string title, IEnumerable<ServiceState> services)
        {
            var list = services.ToList();
            var online = list.Count(item => item.Label is "ONLINE" or "DONE");
            return online == list.Count && list.Count > 0
                ? new(title, "online", "good")
                : online > 0 ? new(title, $"{online}/{list.Count}", "warn") : new(title, "offline", "bad");
        }
        var states = _snapshot.Services;
        var coreNames = new HashSet<string>(["tmod-postgres", "tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy"]);
        var atlasNames = new HashSet<string>(["atlas-qdrant", "atlas-forum-browser"]);
        var minecraftNames = new HashSet<string>(["minecraft", "minecraft-supervisor"]);
        List<DashboardCard> cards =
        [
            Contour("CORE", states.Where(item => coreNames.Contains(item.Service))),
            Contour("ATLAS", states.Where(item => atlasNames.Contains(item.Service))),
            Contour("MINECRAFT", states.Where(item => minecraftNames.Contains(item.Service))),
            new("DOCKER", _snapshot.DockerReady ? "ready" : "offline", _snapshot.DockerReady ? "good" : "bad")
        ];
        return _settings.Compact ? new List<DashboardCard> { cards[0], cards[3] } : cards;
    }

    private static bool TryApplyPendingUpdate()
    {
        if (!OperatingSystem.IsWindows()) return false;
        var current = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(current)) return false;
        var pending = current + ".next";
        if (!File.Exists(pending)) return false;
        try
        {
            var info = new System.Diagnostics.ProcessStartInfo
            {
                FileName = "powershell.exe",
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = System.Diagnostics.ProcessWindowStyle.Hidden,
            };
            info.Environment["TMOD_CONTROL_CURRENT"] = current;
            info.Environment["TMOD_CONTROL_PENDING"] = pending;
            info.Environment["TMOD_CONTROL_PID"] = Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture);
            info.ArgumentList.Add("-NoLogo");
            info.ArgumentList.Add("-NoProfile");
            info.ArgumentList.Add("-WindowStyle");
            info.ArgumentList.Add("Hidden");
            info.ArgumentList.Add("-Command");
            info.ArgumentList.Add("$targetPid=[int]$env:TMOD_CONTROL_PID; Wait-Process -Id $targetPid -ErrorAction SilentlyContinue; Move-Item -LiteralPath $env:TMOD_CONTROL_PENDING -Destination $env:TMOD_CONTROL_CURRENT -Force; Start-Process -FilePath $env:TMOD_CONTROL_CURRENT");
            _ = System.Diagnostics.Process.Start(info);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static void ShowMainMenu()
    {
        while (true)
        {
            var selected = _ui.Select("Центр управления",
            [
                new("Обзор системы", "Версия, здоровье и карта всех сервисов", "status", "◈"),
                new("Безопасно обновить", "Тесты → backup → запуск → healthcheck → rollback", "update", "⇡"),
                new("Питание системы", "Запуск, перезапуск и мягкая остановка", "power", "◉"),
                new("Сервисы", "Управление каждым контейнером и живыми логами", "services", "▦"),
                new("Контуры", "Ядро T-Mod, Atlas и Minecraft", "groups", "⬡"),
                new("Центр обновлений", "Git, история релизов и автоматизация", "updates", "◫"),
                new("Наблюдение", "Инциденты, ресурсы и диагностические отчёты", "observability", "⌁"),
                new("Защита данных", "Backup, состояние и полная проверка базы", "data", "◇"),
                new("Сеть", "Домены, Caddy и публичные сервисы", "network", "◎"),
                new("Внешний пульт", "Запустить защищённый T-Mod Remote", "remote", "⌁"),
                new("Настройки", "Темы, анимации и плотность интерфейса", "settings", "⚙"),
                new("Справка", "Горячие клавиши и границы безопасности", "help", "?"),
                new("Выход", "Закрыть Control Center", "exit", "×")
            ], DashboardCards,
            "R обновить   U обновления   D диагностика   F1 помощь   Q выход",
            new Dictionary<ConsoleKey, string>
            {
                [ConsoleKey.R] = "refresh", [ConsoleKey.U] = "updates", [ConsoleKey.D] = "diagnostics",
                [ConsoleKey.F1] = "help", [ConsoleKey.Q] = "exit"
            });
            switch (selected)
            {
                case "back" or "exit": return;
                case "refresh": _snapshot = _backend.GetStatusAsync(TimeSpan.FromSeconds(8)).GetAwaiter().GetResult(); break;
                case "status": ShowStatus(); break;
                case "update": RunAction("update", "SAFE UPDATE"); break;
                case "power": ShowPower(); break;
                case "services": ShowServices(); break;
                case "groups": ShowGroups(); break;
                case "updates": ShowUpdates(); break;
                case "observability": ShowObservability(); break;
                case "data": ShowData(); break;
                case "network": ShowNetwork(); break;
                case "remote": LaunchRemote(); break;
                case "settings": ShowSettings(); break;
                case "help": ShowHelp(); break;
                case "diagnostics": RunAction("diagnostics", "DIAGNOSTICS"); break;
            }
        }
    }

    private static void ShowStatus()
    {
        _snapshot = _backend.GetStatusAsync(TimeSpan.FromSeconds(8)).GetAwaiter().GetResult();
        var lines = new StringBuilder();
        lines.AppendLine($"Ветка: {_snapshot.Branch}   Commit: {_snapshot.Release}   Docker: {(_snapshot.DockerReady ? "READY" : "OFFLINE")}");
        lines.AppendLine();
        foreach (var service in Backend.Services)
        {
            var state = _snapshot.Services.FirstOrDefault(item => item.Service == service);
            lines.AppendLine($"{service,-27}  {state?.Label ?? "OFFLINE",-10}  {state?.Health ?? string.Empty}");
        }
        _ui.Message("Карта системы", lines.ToString(), _snapshot.DockerReady ? "good" : "warn");
    }

    private static void ShowPower()
    {
        while (true)
        {
            var action = _ui.Select("Питание системы",
            [
                new("Запустить установленную версию", "Полная подготовка и запуск", "start", "▶"),
                new("Перезапустить всю систему", "Пересоздание с соблюдением зависимостей", "restart", "↻", true),
                new("Остановить всю систему", "Данные сохраняются", "stop", "■", true),
                new("Назад", "Главный экран", "back", "↩")
            ], footer: "Volumes и постоянные данные этим экраном не удаляются.");
            if (action == "back") return;
            if (action is "restart" or "stop" && !_ui.Confirm("Подтвердить действие", "Сервисы временно станут недоступны.")) continue;
            RunAction(action, "POWER CONTROL");
        }
    }

    private static void ShowServices()
    {
        while (true)
        {
            var items = Backend.Services.Select(name => new MenuItem(name, "Контейнер T-Mod", name, "▣")).ToList();
            items.Add(new("Назад", "Главный экран", "back", "↩"));
            var service = _ui.Select("Сервисы", items);
            if (service == "back") return;
            while (true)
            {
                var action = _ui.Select(service,
                [
                    new("Запустить", "Создать контейнер при необходимости", "service-start", "▶"),
                    new("Перезапустить", "Мягкий restart", "service-restart", "↻"),
                    new("Остановить", "Данные сохраняются", "service-stop", "■", true),
                    new("Обновить сервис", "Образ и контейнер", "service-update", "⇡"),
                    new("Последние логи", "160 строк", "service-logs", "≡"),
                    new("Живые логи", "Ctrl+C завершает просмотр", "service-logs-follow", "⌁"),
                    new("Назад", "К списку сервисов", "back", "↩")
                ]);
                if (action == "back") break;
                if (action == "service-stop" && !_ui.Confirm($"Остановить {service}?", "Сервис станет недоступен.")) continue;
                RunAction(action, "SERVICE CONTROL", service);
            }
        }
    }

    private static void ShowGroups()
    {
        while (true)
        {
            var group = _ui.Select("Контуры",
            [
                new("Ядро T-Mod", "PostgreSQL, Discord, Web, Worker и Caddy", "core", "⬡"),
                new("Atlas", "Qdrant и браузер форума", "atlas", "✦"),
                new("Minecraft", "Сервер и supervisor", "minecraft", "▦"),
                new("Назад", "Главный экран", "back", "↩")
            ]);
            if (group == "back") return;
            var action = _ui.Select($"Контур / {group}",
            [
                new("Запустить", "Только выбранный контур", "group-start", "▶"),
                new("Перезапустить", "Только выбранный контур", "group-restart", "↻"),
                new("Остановить", "Остальные сервисы продолжат работу", "group-stop", "■", true),
                new("Назад", "К контурам", "back", "↩")
            ]);
            if (action == "back") continue;
            if (action == "group-stop" && !_ui.Confirm("Остановить контур?", "Остановятся только его сервисы.")) continue;
            RunAction(action, "CONTOUR CONTROL", group: group);
        }
    }

    private static void ShowUpdates()
    {
        while (true)
        {
            var action = _ui.Select("Центр обновлений",
            [
                new("Безопасно обновить", "Полный проверенный релиз", "update", "⇡"),
                new("История обновлений", "Updater, watcher и launch guard", "update-status", "◫"),
                new("Состояние Git", "Ветка, commit и рабочая копия", "git-status", "⑂"),
                new("Состояние автообновления", "Windows Task Scheduler", "auto-update-status", "◴"),
                new("Включить автообновление", "Проверка main раз в две минуты", "auto-update", "▶"),
                new("Приостановить автообновление", "Текущая версия продолжит работу", "auto-update-disable", "Ⅱ", true),
                new("Назад", "Главный экран", "back", "↩")
            ], footer: "Неудачный релиз автоматически откатывается и помещается в карантин.");
            if (action == "back") return;
            if (action == "auto-update-disable" && !_ui.Confirm("Остановить автообновление?", "Ручное обновление останется доступно.")) continue;
            RunAction(action, "UPDATE CENTER");
        }
    }

    private static void ShowObservability()
    {
        while (true)
        {
            var action = _ui.Select("Наблюдение",
            [
                new("Полная диагностика", "Docker, Web, Discord, диск и ошибки", "diagnostics", "◈"),
                new("Поток инцидентов", "Критические записи контейнеров за час", "error-log", "⌁"),
                new("Ресурсы", "CPU, RAM, сеть и диск", "resources", "▥"),
                new("Экспортировать отчёт", "Диагностический пакет в Documents", "export-diagnostics", "⇩"),
                new("Очистить старый Docker cache", "Только неиспользуемое старше 7 дней", "docker-clean", "◇", true),
                new("Назад", "Главный экран", "back", "↩")
            ]);
            if (action == "back") return;
            if (action == "docker-clean" && !_ui.Confirm("Очистить старый cache?", "Volumes и рабочие контейнеры не затрагиваются.")) continue;
            RunAction(action, "OBSERVABILITY");
        }
    }

    private static void ShowData()
    {
        while (true)
        {
            var action = _ui.Select("Защита данных",
            [
                new("Создать резервную копию", "Согласованный ручной backup", "backup", "◇"),
                new("Состояние резервов", "Последние копии и свободное место", "db-status", "▤"),
                new("Полная проверка базы", "Проверка целостности", "db-check", "◈"),
                new("Назад", "Главный экран", "back", "↩")
            ]);
            if (action == "back") return;
            RunAction(action, "DATA GUARD");
        }
    }

    private static void ShowNetwork()
    {
        while (true)
        {
            var action = _ui.Select("Сеть",
            [
                new("Проверить все домены", "HTTPS и задержка каждого контура", "domain-check", "◎"),
                new("Проверить и применить Caddy", "Validate перед reload", "caddy-reload", "↻"),
                new("Открыть сервисы", "T-Mod, Reactor, Consensus, Atlas и SGL", "open-sites", "↗"),
                new("Назад", "Главный экран", "back", "↩")
            ]);
            if (action == "back") return;
            RunAction(action, "NETWORK FABRIC");
        }
    }

    private static void ShowSettings()
    {
        while (true)
        {
            var action = _ui.Select("Настройки",
            [
                new("Цветовой контур", $"Сейчас: {_settings.Theme}", "theme", "◉"),
                new("Анимации", _settings.Animations ? "Включены" : "Выключены", "animations", "✦"),
                new("Компактный режим", _settings.Compact ? "Включён" : "Выключен", "compact", "▦"),
                new("Назад", "Главный экран", "back", "↩")
            ]);
            if (action == "back") return;
            if (action == "theme")
            {
                var theme = _ui.Select("Цветовой контур", Themes.All.Select(pair => new MenuItem(pair.Value.Name, "Предпросмотр после выбора", pair.Key, "◆")).Append(new("Назад", "Настройки", "back", "↩")).ToList());
                if (theme != "back")
                {
                    _settings = _settings with { Theme = theme };
                    _ui.Theme = Themes.Resolve(theme);
                    _settings.Save(_backend.PersistentDir);
                    _ui.Transition($"Theme / {theme}");
                }
            }
            else if (action == "animations")
            {
                _settings = _settings with { Animations = !_settings.Animations };
                _ui.Animations = _settings.Animations;
                _settings.Save(_backend.PersistentDir);
            }
            else if (action == "compact")
            {
                _settings = _settings with { Compact = !_settings.Compact };
                _settings.Save(_backend.PersistentDir);
            }
        }
    }

    private static void ShowHelp()
    {
        _ui.Message("Справка",
            "R — обновить состояние     U — центр обновлений     D — диагностика     F1 — справка     Q — выход\n\n" +
            "Безопасное обновление проходит тесты и backup до переключения main.\n" +
            "Пульт не удаляет Docker volumes, рабочие контейнеры или базу данных.\n" +
            "Remote принимает только заранее разрешённые команды и сервисы.");
    }

    private static void LaunchRemote()
    {
        var desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        var remote = Path.Combine(desktop, "T-Mod Remote.bat");
        if (!File.Exists(remote))
        {
            _ui.Message("T-Mod Remote", "Удалённый пульт ещё не установлен. Запустите install_tmod_remote_windows.bat из папки проекта.", "warn");
            return;
        }
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(remote) { UseShellExecute = true });
    }

    private static void RunAction(string action, string title, string? service = null, string? group = null)
    {
        _ui.Leave();
        Console.Clear();
        Console.OutputEncoding = Encoding.UTF8;
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine($"\n  T—MOD / {title}\n");
        Console.ResetColor();
        int code;
        try { code = _backend.RunVisible(action, service, group); }
        catch (Exception exception) { Console.ForegroundColor = ConsoleColor.Red; Console.WriteLine(exception.Message); Console.ResetColor(); code = 1; }
        Console.WriteLine();
        Console.ForegroundColor = code == 0 ? ConsoleColor.Green : ConsoleColor.Red;
        Console.WriteLine(code == 0 ? "  Операция завершена." : $"  Операция завершилась с кодом {code}.");
        Console.ResetColor();
        Console.WriteLine("\n  Нажмите любую клавишу, чтобы вернуться в Control Center.");
        _ = Console.ReadKey(true);
        _ui.Enter();
        _snapshot = _backend.GetStatusAsync(TimeSpan.FromSeconds(8)).GetAwaiter().GetResult();
    }
}
