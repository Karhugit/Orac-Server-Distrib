from datetime import datetime, timedelta
from resources.lib.log_utils import log, LOGERROR

def parse_date_param(value):
    """Parses special date values like 'T-7' into 'YYYY-MM-DD' format."""
    if isinstance(value, list):
        value = value[0]
    
    if isinstance(value, str) and value.upper().startswith('T') and len(value) > 1:
        try:
            offset_str = value[1:]
            offset = int(offset_str) if offset_str else 0
            target_date = datetime.now() + timedelta(days=offset)
            return target_date.strftime('%Y-%m-%d')
        except (ValueError, IndexError):
            log(f"Invalid T-based date format: {value}", level=LOGERROR)
            return None
    return value
