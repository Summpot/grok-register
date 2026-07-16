"""Test the Turnstile shadow-DOM + iframe traversal chain."""
import sys, time, urllib.parse
sys.path.insert(0, '.')
from grok_register.browser_adapter import Chromium, ChromiumOptions

o = ChromiumOptions(); o.auto_port()
b = Chromium(o); t = b.latest_tab

# Build Turnstile-like DOM:  shadow-host > shadow > iframe > inner body
html = r"""<!DOCTYPE html><html><body><script>
var host = document.createElement('div');
host.id = 'cf-wrapper';
var pageShadow = host.attachShadow({mode:'open'});
pageShadow.innerHTML = '<iframe id="cf-frame" srcdoc="'
  + '<html><head></head><body>'
  + '<div id=inner-chk style=width:40px;height:40px;background:green>OK</div>'
  + '</body></html>" '
  + 'style="width:304px;height:78px;border:none;"></iframe>';
document.body.appendChild(host);
var inp = document.createElement('input');
inp.name = 'cf-turnstile-response';
inp.value = '';
host.appendChild(inp);
</script></body></html>"""
t.get('data:text/html,' + urllib.parse.quote(html))
time.sleep(0.8)

# Step 1: find challenge input
challenge = t.ele('@name=cf-turnstile-response')
print('1. challenge_input:', challenge is not None)

# Step 2: parent -> shadow host
wrapper = challenge.parent()
print('2. parent:', wrapper is not None)

# Step 3: shadow_root
sr = wrapper.shadow_root
print('3. shadow_root:', sr is not None)

# Step 4: iframe inside shadow
iframe_el = sr.ele('tag:iframe')
print('4. iframe in shadow:', iframe_el is not None)

if iframe_el:
    # Step 5: body inside iframe (iframe routing)
    body_el = iframe_el.ele('tag:body')
    print('5. body in iframe:', body_el is not None)

    # Step 6: direct query in iframe
    inner = iframe_el.ele('#inner-chk')
    print('6. direct #inner-chk:', inner is not None, 'text:', inner.text if inner else 'N/A')

    # Step 7: run_js in iframe
    r = iframe_el.run_js('window.dtp = 1; return window.dtp')
    print('7. iframe.run_js:', r)

b.quit()
print('TURNSTILE CHAIN OK')
