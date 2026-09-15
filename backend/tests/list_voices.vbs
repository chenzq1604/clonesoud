' 枚举系统已安装的 SAPI 语音
Dim voice, v
Set voice = CreateObject("SAPI.SpVoice")
For Each v In voice.GetVoices()
  WScript.Echo v.GetAttribute("Name") & " | " & v.GetAttribute("Language")
Next
