import json, pathlib, requests
p=pathlib.Path('config.json')
c=json.loads(p.read_text(encoding='utf-8-sig'))
def mask(v):
    v=str(v or '')
    return v[:4]+'...'+v[-4:] if len(v)>8 else ('***' if v else '')
print('config:', p.resolve())
for k in ['email_provider','register_count','max_mail_retry','code_poll_timeout','cpa_export_enabled','cpa_auth_dir','sub2api_export_enabled','sub2api_combined_file','grok2api_remote_base']:
    print(f'{k}:', c.get(k))
print('yyds_api_key:', mask(c.get('yyds_api_key')))
r=requests.get('https://maliapi.215.im/v1/domains', headers={'X-API-Key': c.get('yyds_api_key')}, timeout=20)
print('YYDS HTTP:', r.status_code, 'success:', r.json().get('success'))
base=str(c.get('grok2api_remote_base') or '').rstrip('/')
app=c.get('grok2api_remote_app_key')
if c.get('grok2api_auto_add_remote') and base and app:
    rr=requests.get(base + '/admin/api/tokens', params={'app_key': app}, timeout=15)
    print('grok2api HTTP:', rr.status_code)
print('VERIFY OK')
