import os, datetime

LOGDEBUG = 0
LOGINFO = 1
LOGWARNING = 2
LOGERROR = 3
LOGFATAL = 4

level_names = {
    LOGDEBUG: "DEBUG",
    LOGINFO: "INFO",
    LOGWARNING: "WARNING",
    LOGERROR: "ERROR",
    LOGFATAL: "FATAL"
}

IS_LOCAL = os.environ.get("ORAC_ENV", "").upper() == "LOCAL"

# Configuration for file logging
LOG_TO_FILE = os.environ.get("ORAC_LOG_FILE", "").upper() == "TRUE"
LOG_FILE_PATH = os.environ.get("ORAC_LOG_PATH", "orac.log")
MAX_LOG_SIZE = int(os.environ.get("ORAC_MAX_LOG_SIZE", "10485760"))  # 10MB default


def _rotate_log_if_needed():
    """Rotate log file if it gets too large"""
    if not os.path.exists(LOG_FILE_PATH):
        return
    
    if os.path.getsize(LOG_FILE_PATH) > MAX_LOG_SIZE:
        # Keep one backup
        backup_path = LOG_FILE_PATH + ".old"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.rename(LOG_FILE_PATH, backup_path)


def log(message, level=LOGINFO):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[Orac] {timestamp}"
    formatted_message = f"{prefix} [{level_names.get(level, 'INFO')}] {message}"

    if not IS_LOCAL:
        # Running in Kodi
        try:
            import xbmc
            xbmc.log(formatted_message, level)
        except ImportError:
            # Fallback if xbmc not available
            print(formatted_message)
    else:
        # Running locally
        if LOG_TO_FILE:
            # Log to file
            try:
                _rotate_log_if_needed()
                with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                    f.write(formatted_message + "\n")
                    f.flush()  # Ensure immediate write
            except Exception as e:
                # Fallback to print if file logging fails
                print(f"[LOG ERROR] Could not write to log file: {e}")
                print(formatted_message)
        else:
            # Print to console
            print(formatted_message)

    if level == LOGERROR:
        # Additional handling for errors
        pass
