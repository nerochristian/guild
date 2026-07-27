Option Explicit

Dim shell, fso, repoPath, watcherPath, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoPath = fso.GetParentFolderName(WScript.ScriptFullName)
watcherPath = repoPath & "\auto_update_watcher.ps1"
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & watcherPath & """ -RepoPath """ & repoPath & """"

shell.Run command, 0, False
