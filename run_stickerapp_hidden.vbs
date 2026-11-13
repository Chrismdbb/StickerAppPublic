' Starts StickerApp with no console window.
' To start it automatically at login: press Win+R, type shell:startup, press Enter,
' then put a shortcut to this file in the folder that opens.
' The dashboard is at http://localhost:8080/ and the log is app\data\stickerapp.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
py = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw"
sh.Run """" & py & """ """ & here & "\app\supervisor.py""", 0, False
