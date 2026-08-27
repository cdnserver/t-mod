namespace TMod.Control;

internal sealed record Theme(
    string Name,
    string Accent,
    string AccentSoft,
    string Glow,
    string Good,
    string Warn,
    string Bad,
    string Surface,
    string Text,
    string Muted
);

internal static class Themes
{
    internal static readonly IReadOnlyDictionary<string, Theme> All =
        new Dictionary<string, Theme>(StringComparer.OrdinalIgnoreCase)
        {
            ["aurora"] = new("Aurora", "69;224;255", "46;129;255", "29;84;116", "92;245;169", "255;203;92", "255;94;117", "14;24;36", "235;247;255", "112;139;158"),
            ["reactor"] = new("Reactor", "91;246;164", "42;179;112", "24;91;68", "104;255;180", "255;215;91", "255;92;92", "10;31;25", "232;255;244", "104;148;129"),
            ["atlas"] = new("Atlas", "105;165;255", "173;112;255", "47;55;128", "88;224;255", "255;201;90", "255;100;138", "17;20;45", "239;241;255", "126;130;175"),
            ["ember"] = new("Ember", "255;188;79", "255;105;69", "116;55;31", "115;244;170", "255;205;91", "255;85;85", "38;22;16", "255;244;227", "166;126;101"),
        };

    internal static Theme Resolve(string? name) =>
        name is not null && All.TryGetValue(name, out var theme) ? theme : All["aurora"];
}
