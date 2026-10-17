' Runs run.bat hidden so no console window pops up every 15 minutes
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & dir & "\run.bat""", 0, True
