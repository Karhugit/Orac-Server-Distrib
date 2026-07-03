import os

file_path = r'd:\Python Coding\Orac Server\orac_server\resources\scrapers\modules\control.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the specific block safely
target_block = """addonObject = None
addonInfo = lambda x: ""
getLangString = lambda x: str(x)
condVisibility = lambda x: False
infoLabel = lambda x: ""
execute = lambda x: None
jsonrpc = lambda x: None
monitor_class = object
monitor = object()

dialog = xbmcgui.Dialog()
homeWindow = xbmcgui.Window(10000)
progressDialog = xbmcgui.DialogProgress()
progress_line = '%s[CR]%s[CR]%s'

deleteFile = xbmcvfs.delete
existsPath = xbmcvfs.exists
openFile = xbmcvfs.File
makeFile = xbmcvfs.mkdir
makeDirs = xbmcvfs.mkdirs
renameFile = xbmcvfs.rename
transPath = xbmcvfs.translatePath
joinPath = os.path.join

SETTINGS_PATH = transPath(joinPath(addonInfo('path'), 'resources', 'settings.xml'))
dataPath = transPath(addonInfo('profile'))"""

replacement_block = """addonObject = None

# ORAC: Fix paths so data files go into the project root
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.abspath(os.path.join(_script_dir, '..', '..', '..'))

def addonInfo(id):
    if id == 'path': return _root_dir
    if id == 'profile': return _root_dir
    return ""

def transPath(path):
    if path and path.startswith('special://'):
        return _root_dir
    return path

getLangString = lambda x: str(x)
condVisibility = lambda x: False
infoLabel = lambda x: ""
execute = lambda x: None
jsonrpc = lambda x: None
monitor_class = object
monitor = object()

dialog = xbmcgui.Dialog()
homeWindow = xbmcgui.Window(10000)
progressDialog = xbmcgui.DialogProgress()
progress_line = '%s[CR]%s[CR]%s'

deleteFile = xbmcvfs.delete
existsPath = xbmcvfs.exists
openFile = xbmcvfs.File
makeFile = xbmcvfs.mkdir
makeDirs = xbmcvfs.mkdirs
renameFile = xbmcvfs.rename
joinPath = os.path.join

SETTINGS_PATH = transPath(joinPath(addonInfo('path'), 'resources', 'settings.xml'))
dataPath = transPath(addonInfo('profile'))"""

if target_block in content:
    content = content.replace(target_block, replacement_block)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("control.py updated successfully!")
else:
    print("Target block not found in control.py!")
