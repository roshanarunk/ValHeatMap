' Launches the ValHeatMap crawler tray icon with no console window.
' Double-click this file, or put a shortcut to it in your Startup folder.
Set shell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir & "\backend"
' 0 = hidden window, False = don't wait for it to finish
shell.Run "pythonw -m app.tray", 0, False
