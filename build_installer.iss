[Setup]
; App Information
AppName=Orac Server
AppVersion=1.0
AppPublisher=Orac Server Community

; Default installation folder: AppData\Local\Orac Server
; This is generally best for apps that modify files in their own directory without admin rights
DefaultDirName={autopf}\Orac Server

; Start Menu Group Name
DefaultGroupName=Orac Server

; Output configuration
OutputDir=Output
OutputBaseFilename=OracServerSetup

; Best compression
Compression=lzma2
SolidCompression=yes

; Ask for admin rights if installing to Program Files, but auto-fallback if needed.
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Include all files and subdirectories from the source directory, EXCEPT development and runtime artifacts.
; The Excludes flag ensures we don't pack your local databases, virtual environments, or git history.
Source: "*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.db,*.db-wal,*.db-shm,*.log,*.log.old,venv\*,__pycache__\*,*.pyc,tokens.json,tzen_tokens.json,flixpatrol_dump.html,flixpatrol_list.json,next_episodes_list.txt,Scrape results.txt,.git\*,Output\*,*.iss"

[Icons]
; Start Menu Shortcut
Name: "{group}\Start Orac Server"; Filename: "{app}\start_server.bat"; WorkingDir: "{app}"
Name: "{group}\Stop Orac Server"; Filename: "{app}\stop_server.bat"; WorkingDir: "{app}"
; Desktop Shortcut
Name: "{autodesktop}\Start Orac Server"; Filename: "{app}\start_server.bat"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autodesktop}\Stop Orac Server"; Filename: "{app}\stop_server.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Option to launch the server immediately after installation finishes
Filename: "{app}\start_server.bat"; Description: "Launch Orac Server"; Flags: postinstall nowait runhidden skipifsilent
