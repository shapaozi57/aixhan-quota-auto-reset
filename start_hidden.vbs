Option Explicit

Dim shell, fso, appDir, pythonwPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = appDir

pythonwPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

cmd = Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & appDir & "\app.py" & Chr(34)
shell.Run cmd, 0, False
