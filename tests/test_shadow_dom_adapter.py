import sys; sys.path.insert(0, '.')
from grok_register.browser_adapter import Chromium, ChromiumOptions
import time

o = ChromiumOptions(); o.auto_port()
b = Chromium(o); t = b.latest_tab

t.run_js("document.body.innerHTML='';var d=document.createElement('div');d.id='w';var sr=d.attachShadow({mode:'open'});sr.innerHTML='<div>x</div>';document.body.appendChild(d);")
time.sleep(0.3)

el = t.ele('#w')
print('el found:', el is not None)

has_sr = el._h.evaluate('el => !!el.shadowRoot')
print('has shadowRoot:', has_sr)

sr = el.shadow_root
print('shadow_root:', sr, 'is_shadow:', sr._is_shadow if sr else 'N/A')

if sr:
    inner = sr.ele('tag:div')
    print('shadow div found:', inner is not None)
    if inner:
        print('text:', inner.text)
    # Also test run_js on shadow
    val = sr.run_js("return this.querySelector('div').textContent")
    print('run_js result:', val)

b.quit()
print('DONE')
