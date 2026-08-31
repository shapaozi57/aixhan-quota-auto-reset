$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$IconPath = Join-Path $Root "assets\aixhan_quota.ico"
$LauncherPath = Join-Path $Root "start_hidden.vbs"
$Shell = New-Object -ComObject WScript.Shell
$LegacyShortcutName = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("c3RhcnQuYmF0IC0g5b+r5o235pa55byPLmxuaw=="))

$ShortcutPaths = @(
    (Join-Path $Root "AixHan Quota Auto Reset.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "AixHan Quota Auto Reset.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) $LegacyShortcutName)
)

foreach ($ShortcutPath in $ShortcutPaths) {
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
    $Shortcut.Arguments = "//B `"$LauncherPath`""
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.IconLocation = "$IconPath,0"
    $Shortcut.WindowStyle = 7
    $Shortcut.Description = "AixHan Quota Auto Reset"
    $Shortcut.Save()
    Write-Host "Created: $ShortcutPath"
}
