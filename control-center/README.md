# T-Mod Control Center

Нативный Windows-пульт для управления системой T-Mod. Интерфейс написан на .NET,
а проверенный allowlist управляющих операций встраивается в единственный самодостаточный EXE.

Сборка:

```powershell
python scripts/build_tmod_windows_bundles.py
dotnet publish control-center/TMod.Control/TMod.Control.csproj -c Release -r win-x64 --self-contained true
```

Результат: `T-Mod-Control.exe`. Для запуска на целевом компьютере установка .NET не требуется.
Установщик проверяет размер, SHA-256 и PE-заголовок, а обновление работающего EXE применяется
атомарно при следующем открытии пульта. `tmod_control_windows.bat` остаётся только аварийным
репозиторным вариантом и больше не является ярлыком рабочего стола.
