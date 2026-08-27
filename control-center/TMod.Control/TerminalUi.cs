using System.Runtime.InteropServices;
using System.Text;

namespace TMod.Control;

internal sealed record MenuItem(string Label, string Hint, string Value, string Glyph = "◆", bool Dangerous = false);
internal sealed record DashboardCard(string Title, string Value, string Kind);

internal sealed class TerminalUi : IDisposable
{
    private const string Esc = "\u001b[";
    private readonly Random _random = new(7429);
    private bool _alternate;
    internal Theme Theme { get; set; }
    internal bool Animations { get; set; }

    internal TerminalUi(Theme theme, bool animations)
    {
        Theme = theme;
        Animations = animations;
        Console.InputEncoding = Encoding.UTF8;
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Title = "T-Mod Control Center";
        EnableVirtualTerminal();
    }

    private static void EnableVirtualTerminal()
    {
        if (!OperatingSystem.IsWindows()) return;
        var handle = GetStdHandle(-11);
        if (handle == IntPtr.Zero || !GetConsoleMode(handle, out var mode)) return;
        _ = SetConsoleMode(handle, mode | 0x0004 | 0x0008);
    }

    internal void Enter()
    {
        if (_alternate) return;
        Console.Write($"{Esc}?1049h{Esc}?25l{Esc}2J{Esc}H");
        _alternate = true;
    }

    internal void Leave()
    {
        if (!_alternate) return;
        Console.Write($"{Esc}?25h{Esc}?1049l{Esc}0m");
        _alternate = false;
    }

    private static string Fg(string rgb) => $"{Esc}38;2;{rgb}m";
    private static string Bg(string rgb) => $"{Esc}48;2;{rgb}m";
    private static string Reset => $"{Esc}0m";
    private static string Bold => $"{Esc}1m";
    private static string Dim => $"{Esc}2m";

    internal void Intro()
    {
        Enter();
        if (!Animations) return;
        var width = Math.Clamp(SafeWindowWidth(), 56, 132);
        var height = Math.Clamp(SafeWindowHeight(), 22, 42);
        var stars = Enumerable.Range(0, 54).Select(_ => (_random.Next(width), _random.Next(height - 5), _random.Next(4))).ToArray();
        for (var frame = 0; frame < 24; frame++)
        {
            var canvas = new char[height, width];
            foreach (var (x0, y0, speed) in stars)
            {
                var x = (x0 + frame * Math.Max(1, speed)) % width;
                var y = (y0 + frame / Math.Max(3, 8 - speed)) % Math.Max(1, height - 5);
                canvas[y, x] = speed >= 3 ? '✦' : speed == 2 ? '·' : '∙';
            }
            DrawComet(canvas, width - 6 - frame * 3, 3 + frame / 4);
            DrawComet(canvas, width / 2 - frame * 2, height - 8 - frame / 7);
            var glow = frame < 7 ? "SYSTEM FABRIC" : frame < 15 ? "T — M O D" : "CONTROL CENTER";
            WriteCanvas(canvas, glow, frame / 23d);
            Thread.Sleep(48);
        }
    }

    private static void DrawComet(char[,] canvas, int x, int y)
    {
        if (y < 0 || y >= canvas.GetLength(0)) return;
        var trail = new[] { '✦', '━', '─', '·' };
        for (var i = 0; i < trail.Length; i++)
        {
            var targetX = x + i;
            if (targetX >= 0 && targetX < canvas.GetLength(1)) canvas[y, targetX] = trail[i];
        }
    }

    private void WriteCanvas(char[,] canvas, string title, double progress)
    {
        var height = canvas.GetLength(0);
        var width = canvas.GetLength(1);
        var builder = new StringBuilder($"{Esc}H");
        for (var y = 0; y < height; y++)
        {
            for (var x = 0; x < width; x++)
            {
                var character = canvas[y, x];
                builder.Append(character == '\0' ? ' ' : character);
            }
            builder.Append('\n');
        }
        var titleX = Math.Max(2, (width - title.Length) / 2);
        var titleY = Math.Max(3, height / 2 - 1);
        var barWidth = Math.Min(42, width - 8);
        var filled = Math.Clamp((int)Math.Round(progress * barWidth), 0, barWidth);
        builder.Append($"{Esc}{titleY};{titleX}H{Fg(Theme.Text)}{Bold}{title}{Reset}");
        builder.Append($"{Esc}{titleY + 2};{Math.Max(2, (width - barWidth) / 2)}H{Fg(Theme.Glow)}");
        builder.Append(new string('━', filled)).Append(Fg(Theme.Surface)).Append(new string('─', barWidth - filled)).Append(Reset);
        Console.Write(builder.ToString());
    }

    internal string Select(
        string section,
        IReadOnlyList<MenuItem> items,
        Func<IReadOnlyList<DashboardCard>>? cards = null,
        string footer = "↑ ↓ выбрать   Enter открыть   Esc назад",
        IReadOnlyDictionary<ConsoleKey, string>? hotkeys = null)
    {
        var index = 0;
        var phase = 0;
        var nextTick = DateTime.MinValue;
        while (true)
        {
            if (DateTime.UtcNow >= nextTick)
            {
                Render(section, items, index, cards?.Invoke() ?? [], footer, phase++);
                nextTick = DateTime.UtcNow.AddMilliseconds(420);
            }
            if (!Console.KeyAvailable) { Thread.Sleep(18); continue; }
            var key = Console.ReadKey(true).Key;
            if (hotkeys is not null && hotkeys.TryGetValue(key, out var mapped)) return mapped;
            switch (key)
            {
                case ConsoleKey.UpArrow: index = index == 0 ? items.Count - 1 : index - 1; nextTick = DateTime.MinValue; break;
                case ConsoleKey.DownArrow: index = index == items.Count - 1 ? 0 : index + 1; nextTick = DateTime.MinValue; break;
                case ConsoleKey.PageUp: index = Math.Max(0, index - 5); nextTick = DateTime.MinValue; break;
                case ConsoleKey.PageDown: index = Math.Min(items.Count - 1, index + 5); nextTick = DateTime.MinValue; break;
                case ConsoleKey.Home: index = 0; nextTick = DateTime.MinValue; break;
                case ConsoleKey.End: index = items.Count - 1; nextTick = DateTime.MinValue; break;
                case ConsoleKey.Enter: Transition(items[index].Label); return items[index].Value;
                case ConsoleKey.Escape: return "back";
            }
        }
    }

    private void Render(string section, IReadOnlyList<MenuItem> items, int selected, IReadOnlyList<DashboardCard> cards, string footer, int phase)
    {
        var width = Math.Clamp(SafeWindowWidth() - 1, 54, 126);
        var height = Math.Max(18, SafeWindowHeight());
        var narrow = width < 92;
        var fixedRows = cards.Count > 0 ? 12 : 10;
        var visibleCount = Math.Clamp(height - fixedRows, 5, items.Count);
        var firstVisible = Math.Clamp(selected - visibleCount / 2, 0, Math.Max(0, items.Count - visibleCount));
        var lastVisible = Math.Min(items.Count, firstVisible + visibleCount);
        var builder = new StringBuilder($"{Esc}H{Bg("5;10;18")}{Fg(Theme.Text)}");
        builder.Append("  ").Append(Fg(Theme.Accent)).Append(Bold).Append("T—MOD").Append(Reset).Append(Bg("5;10;18"));
        builder.Append(Fg(Theme.Muted)).Append("  /  CONTROL CENTER  ").Append(Fg(Theme.Glow)).Append("v1.2.1");
        builder.Append(Fg(Theme.Muted)).Append("  /  ").Append(DateTime.Now.ToString("HH:mm:ss")).Append('\n');
        var railWidth = Math.Max(8, width - 4);
        var railPosition = Math.Abs(phase % Math.Max(1, railWidth * 2 - 2) - (railWidth - 1));
        builder.Append("  ").Append(Fg(Theme.Glow));
        for (var rail = 0; rail < railWidth; rail++)
            builder.Append(rail == railPosition ? Fg(Theme.Accent) + "◆" + Fg(Theme.Glow) : "─");
        builder.Append('\n');
        builder.Append("  ").Append(Fg(Theme.Text)).Append(Bold).Append(section.ToUpperInvariant()).Append(Reset).Append(Bg("5;10;18")).Append('\n');
        builder.Append("  ").Append(Fg(Theme.Muted)).Append(Clip("Системный пульт T-Mod · безопасные операции · живое состояние", width - 4)).Append("\n\n");
        if (cards.Count > 0)
        {
            foreach (var card in cards)
            {
                var color = card.Kind switch { "good" => Theme.Good, "warn" => Theme.Warn, "bad" => Theme.Bad, _ => Theme.Accent };
                builder.Append(narrow ? "  " : "  ").Append(Fg(Theme.Glow)).Append("┌ ").Append(Fg(Theme.Text)).Append(card.Title).Append(" ");
                builder.Append(Bg(color)).Append(Fg("3;8;13")).Append(Bold).Append(' ').Append(card.Value.ToUpperInvariant()).Append(' ').Append(Reset).Append(Bg("5;10;18")).Append(Fg(Theme.Glow)).Append(" ┐");
                builder.Append(narrow ? "\n" : "  ");
            }
            builder.Append(narrow ? "\n" : "\n\n");
        }
        if (firstVisible > 0) builder.Append("  ").Append(Fg(Theme.Muted)).Append($"↑ ещё {firstVisible}").Append('\n');
        for (var i = firstVisible; i < lastVisible; i++)
        {
            var item = items[i];
            var availableHint = Math.Max(0, width - (narrow ? 30 : 42));
            var hint = Clip(item.Hint, availableHint);
            if (i == selected)
            {
                var accent = item.Dangerous ? Theme.Bad : Theme.Accent;
                builder.Append("  ").Append(Fg(accent)).Append(phase % 2 == 0 ? "◆ " : "◇ ");
                builder.Append(Bg(Theme.Surface)).Append(Fg(Theme.Text)).Append(Bold).Append(' ').Append(Clip(item.Label, narrow ? 22 : 29)).Append(' ').Append(Reset).Append(Bg("5;10;18"));
                builder.Append(Fg(Theme.AccentSoft)).Append("  ").Append(hint).Append('\n');
            }
            else
            {
                var labelWidth = narrow ? 23 : 29;
                builder.Append("    ").Append(Fg(Theme.Glow)).Append(item.Glyph).Append(' ').Append(Fg(Theme.Text)).Append(Clip(item.Label, labelWidth).PadRight(labelWidth));
                builder.Append(Fg(Theme.Muted)).Append(hint).Append('\n');
            }
        }
        if (lastVisible < items.Count) builder.Append("  ").Append(Fg(Theme.Muted)).Append($"↓ ещё {items.Count - lastVisible}").Append('\n');
        builder.Append('\n').Append("  ").Append(Fg(Theme.Glow)).Append(new string('·', width - 4)).Append('\n');
        builder.Append("  ").Append(Fg(Theme.Muted)).Append(Clip(footer, width - 4)).Append('\n');
        builder.Append($"{Esc}J{Reset}");
        Console.Write(builder.ToString());
    }

    private static int SafeWindowWidth()
    {
        try { return Console.WindowWidth; }
        catch { return 100; }
    }

    private static int SafeWindowHeight()
    {
        try { return Console.WindowHeight; }
        catch { return 30; }
    }

    private static string Clip(string value, int width)
    {
        if (width <= 0) return string.Empty;
        if (value.Length <= width) return value;
        return width <= 1 ? "…" : value[..(width - 1)] + "…";
    }

    internal bool Confirm(string title, string detail) => Select(
        title,
        [new("Продолжить", detail, "yes", "◆", true), new("Отмена", "Ничего не менять", "no", "↩")],
        footer: "Enter подтвердить   Esc отменить") == "yes";

    internal void Transition(string label)
    {
        if (!Animations) return;
        var glyphs = new[] { "·", "∙", "◦", "○", "◉", "◆" };
        for (var i = 0; i < glyphs.Length; i++)
        {
            Console.Write($"{Esc}H{Esc}2J\n\n\n  {Fg(Theme.Accent)}{glyphs[i]}  {Fg(Theme.Text)}{Bold}{label.ToUpperInvariant()}{Reset}");
            Thread.Sleep(42);
        }
    }

    internal void Message(string title, string message, string kind = "info")
    {
        var color = kind switch { "good" => Theme.Good, "warn" => Theme.Warn, "bad" => Theme.Bad, _ => Theme.Accent };
        Console.Write($"{Esc}H{Esc}2J\n\n  {Fg(color)}◆  {Fg(Theme.Text)}{Bold}{title}{Reset}\n\n  {Fg(Theme.Muted)}{message}{Reset}\n\n  {Fg(Theme.Glow)}Нажмите любую клавишу, чтобы вернуться.{Reset}");
        _ = Console.ReadKey(true);
    }

    public void Dispose() => Leave();

    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll")] private static extern bool GetConsoleMode(IntPtr hConsoleHandle, out int lpMode);
    [DllImport("kernel32.dll")] private static extern bool SetConsoleMode(IntPtr hConsoleHandle, int dwMode);
}
