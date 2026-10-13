"""Readable diagnostics with credentials removed before logging."""
import datetime
import json
import os
import re
import traceback


def safe_text(value):
    text = str(value)
    for name, secret in os.environ.items():
        if (name.startswith('OCI_') or name in {'GH_TOKEN','GITHUB_TOKEN'}) and len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', '[PRIVATE_KEY_REDACTED]', text)
    text = re.sub(r'(?:github_pat_[A-Za-z0-9_]+|gh[opusr]_[A-Za-z0-9_]+)', '[TOKEN_REDACTED]', text)
    text = re.sub(r'ocid1\.[A-Za-z0-9_.:-]+', '[OCID_REDACTED]', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+|token\s+)?)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'https://([^\s?]+)\?[^\s]+', r'https://\1?[QUERY_REDACTED]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)[:2400]


def report(event, **fields):
    stamp = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    clean = {key:safe_text(value) if isinstance(value,str) else value for key,value in fields.items()}
    print(stamp + ' ' + json.dumps({'event':event, **clean}, ensure_ascii=False), flush=True)


def error_fields(error):
    fields = {'error_type':type(error).__name__, 'message':safe_text(getattr(error,'message',str(error)))}
    for name in ['status','code','request_id','operation_name','target_service','request_endpoint']:
        value = getattr(error,name,None)
        if value is not None:
            fields[name] = safe_text(value) if isinstance(value,str) else value
    fields['frames'] = [{'file':os.path.basename(f.filename),'line':f.lineno,'function':f.name}
                        for f in traceback.extract_tb(error.__traceback__)[-5:]]
    return fields
