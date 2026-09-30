# Windows service (WinSW)

The repository includes a WinSW service template for the browser viewer. The
setup script expects the WinSW x64 executable at the project root as
`WinSW-x64.exe` and the project's `.venv` to be installed.

Open an elevated PowerShell in the project directory and install the service:

```powershell
uv sync --extra web
Set-ExecutionPolicy -Scope Process Bypass -Force
.\deploy\windows\install-service.ps1
Start-Service wsiPatchkitViewer
```

The installer registers an automatic, delayed-start service and prompts for a
Windows account and password. Use an account that can read the slide directory
and project virtual environment. Credentials are handled by Windows Service
Control Manager; they are not written into the XML configuration. By default,
the slide directory is `D:\张婷乳腺病理`; override it when installing with:

```powershell
.\deploy\windows\install-service.ps1 -SlideDir 'D:\slides'
```

The viewer listens on `127.0.0.1:8000`, so it is available only on the local
machine. WinSW logs, the SQLite KFB index cache, uploaded slides, and saved crops
are stored under `%ProgramData%\wsi-patchkit-viewer`. The service restarts after
an unexpected process failure. To inspect logs, open that folder's `logs`
directory.

To control the service:

```powershell
Get-Service wsiPatchkitViewer
Restart-Service wsiPatchkitViewer
Stop-Service wsiPatchkitViewer
```

To unregister the service, run this from an elevated PowerShell:

```powershell
.\deploy\windows\uninstall-service.ps1
```

Uninstalling removes the Windows service registration but leaves the service
configuration, logs, index cache, uploads, and crops in `%ProgramData%`.
