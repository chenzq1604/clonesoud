' Temp test: read UTF-8 text file and speak to WAV via SAPI
' Usage: cscript //nologo speak_test.vbs <textfile> <wavfile> <langid>
' Parameter values from Microsoft Speech SDK docs:
'   - SpeechStreamFileMode: SSFMCreateForWrite = 3
'   - SpFileStream default format: SAFT22kHz16BitMono (Format is read-only)
' NOTE: keep this file ASCII-only; cscript parses .vbs as ANSI(GBK),
' Chinese text must be passed through the UTF-8 text file instead.
Option Explicit

Dim objArgs, objStream, strText, langId
Set objArgs = WScript.Arguments
If objArgs.Count < 3 Then
  WScript.Echo "Usage: cscript speak_test.vbs <textfile> <wavfile> <langid>"
  WScript.Quit 1
End If

' Read UTF-8 text via ADODB.Stream to avoid encoding issues
Set objStream = CreateObject("ADODB.Stream")
objStream.Type = 2
objStream.Charset = "utf-8"
objStream.Open
objStream.LoadFromFile objArgs(0)
strText = objStream.ReadText
objStream.Close

' Create SAPI voice
Dim objVoice
Set objVoice = CreateObject("SAPI.SpVoice")

' Select voice by language (804=zh-CN 409=en-US), fallback to default
langId = objArgs(2)
On Error Resume Next
Dim colVoices
Set colVoices = objVoice.GetVoices("Language=" & langId)
If Err.Number = 0 And colVoices.Count > 0 Then
  Set objVoice.Voice = colVoices.Item(0)
End If
On Error GoTo 0

' Speak to WAV file
Dim objFileStream
Set objFileStream = CreateObject("SAPI.SpFileStream")
objFileStream.Open objArgs(1), 3
Set objVoice.AudioOutputStream = objFileStream
objVoice.Speak strText, 0
objFileStream.Close
WScript.Echo "OK"
