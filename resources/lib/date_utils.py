from datetime import datetime, timedelta
from resources.lib.log_utils import log, LOGERROR

def parse_date_param(value):
    """Parses special date values like 'T', 'T-0', 'T-7', 'T+30' into 'YYYY-MM-DD' format."""
    if isinstance(value, list):
        value = value[0]
    
    if isinstance(value, str) and value.strip().upper().startswith('T'):
        val_clean = value.strip().upper()
        if val_clean == 'T':
            return datetime.now().strftime('%Y-%m-%d')
        if len(val_clean) > 1 and val_clean[1] in ('+', '-'):
            try:
                offset = int(val_clean[1:])
                target_date = datetime.now() + timedelta(days=offset)
                return target_date.strftime('%Y-%m-%d')
            except (ValueError, IndexError):
                log(f"Invalid T-based date format: {value}", level=LOGERROR)
                return None
    return value
