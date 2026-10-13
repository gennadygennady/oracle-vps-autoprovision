"""Readable diagnostics with credentials removed before logging."""
import datetime
import json
import os
import re
import traceback
import io
import zipfile
from collections.abc import Mapping


def safe_text(value):
    text = str(value)
    for name, secret in os.environ.items():
        if (name.startswith('OCI_') or name in {'GH_TOKEN','GITHUB_TOKEN'}) and len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', '[PRIVATE_KEY_REDACTED]', text)
    text = re.sub(r'(?:github_pat_[A-Za-z0-9_]+|gh[opusr]_[A-Za-z0-9_]+)', '[TOKEN_REDACTED]', text)
    # Resource Manager job IDs identify a resource, not an authentication secret.
    # Keep them so diagnostic Console links work; other OCIDs stay masked.
    text = re.sub(r'ocid1\.(?!ormjob\.)[A-Za-z0-9_.:-]+', '[OCID_REDACTED]', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+|token\s+)?)[^\s,;]+', r'\1[REDACTED]', text)
    def clean_url(match):
        url=match.group(0)
        if re.fullmatch(r'https://cloud\.oracle\.com/resourcemanager/jobs/ocid1\.ormjob\.oc1\.ap-singapore-2\.[A-Za-z0-9_.-]+\?region=ap-singapore-2',url):
            return url
        return 'https://'+match.group(1)+'?[QUERY_REDACTED]'
    text = re.sub(r'https://([^\s?]+)\?[^\s]+', clean_url, text)
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
    if hasattr(error,'headers'):
        fields['rate_limit_headers']=rate_limit_headers(error.headers)
    return fields


def rate_limit_headers(headers):
    """Only diagnostic limit headers; never cookies or authorization headers."""
    if not isinstance(headers,Mapping):
        return {}
    return {str(name).lower():safe_text(value) for name,value in headers.items()
            if str(name).lower() in {'retry-after','date'} or
            str(name).lower().startswith(('ratelimit-','x-ratelimit-','requestlimit-'))}


def read_diagnostic_zip(content):
    if len(content)>1024*1024:
        raise ValueError('Diagnostic artifact is too large')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if archive.getinfo('diagnostic.json').file_size>65536:
            raise ValueError('Diagnostic JSON is too large')
        data=json.loads(archive.read('diagnostic.json'))
    if not isinstance(data,dict) or not isinstance(data.get('outcome'),str):
        raise ValueError('Diagnostic artifact has no valid outcome')
    return data
