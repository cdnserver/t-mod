using System.Text.Json;

namespace TMod.Control;

internal sealed record ControlSettings(string Theme = "aurora", bool Animations = true, bool Compact = false)
{
    internal static string SettingsPath(string persistentDir) =>
        Path.Combine(persistentDir, "control", "control-center.json");

    internal static ControlSettings Load(string persistentDir)
    {
        try
        {
            var path = SettingsPath(persistentDir);
            if (!File.Exists(path)) return new();
            var loaded = JsonSerializer.Deserialize<ControlSettings>(File.ReadAllText(path));
            return loaded is not null && Themes.All.ContainsKey(loaded.Theme) ? loaded : new();
        }
        catch
        {
            return new();
        }
    }

    internal void Save(string persistentDir)
    {
        var path = SettingsPath(persistentDir);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(temporary, path, true);
    }
}
