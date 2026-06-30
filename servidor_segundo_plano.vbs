' Lanza la plataforma Gestiona mas en segundo plano (ventana oculta).
' Se ejecuta al iniciar sesion en Windows. El link siempre es:
'     http://127.0.0.1:8011
' Usa python (no pythonw) con ventana oculta para que uvicorn tenga stdout.
Set sh = CreateObject("WScript.Shell")
logFile = sh.ExpandEnvironmentStrings("%TEMP%") & "\gestiona_servidor.log"
cmd = "cmd /c cd /d ""C:\Users\diego\gestiona project\backend"" && " & _
      "python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8011 " & _
      "--log-level warning > """ & logFile & """ 2>&1"
' 0 = ventana oculta, False = no esperar.
sh.Run cmd, 0, False
