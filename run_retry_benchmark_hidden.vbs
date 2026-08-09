Option Explicit

' Double-click this file to rerun the retry benchmark without opening a console.
Dim shell, fso, folder, pythonw, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\scoop\apps\python\current\pythonw.exe"
script = folder & "\retry_hard_benchmark.py"

command = "\"" & pythonw & "\" \"" & script & "\""
shell.Run command, 0, False
