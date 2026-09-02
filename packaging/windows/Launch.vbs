Option Explicit

Dim fileSystem, scriptDirectory, shell, startCommand

Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
startCommand = fileSystem.BuildPath(scriptDirectory, "Start Azure SRE Agent Demo.cmd")

If Not fileSystem.FileExists(startCommand) Then
  MsgBox "The Azure SRE Agent Demo start command is missing.", 16, _
    "Azure SRE Agent Demo"
  WScript.Quit 1
End If

Set shell = CreateObject("WScript.Shell")
shell.Run """" & startCommand & """", 0, False
