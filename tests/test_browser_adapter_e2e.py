"""Comprehensive end-to-end test for the Patchright browser adapter.

Tests all the DrissionPage-compatible APIs used by the registration
flows, including shadow DOM, iframes, cookies, and Turnstile patterns.
"""
import sys
import time

sys.path.insert(0, ".")

from grok_register.browser_adapter import Chromium, ChromiumOptions  # noqa: E402

PASS = 0
FAIL = 0


def check(desc: str, result: bool):
    global PASS, FAIL
    if result:
        PASS += 1
        print(f"  [PASS] {desc}")
    else:
        FAIL += 1
        print(f"  [FAIL] {desc}")
    return result


# ── Test 1: Basic browser lifecycle ──
print("\n=== Test 1: Browser lifecycle ===")
opts = ChromiumOptions()
opts.auto_port()
opts.set_timeouts(2)
b = Chromium(opts)
check("Chromium(opts) returned a browser", b is not None)

# ── Test 2: Tab management ──
print("\n=== Test 2: Tab management ===")
t = b.latest_tab
check("latest_tab returns a page", t is not None)
check("page.url is about:blank", t.url == "about:blank")

t2 = b.new_tab("https://example.com")
check("new_tab(url) navigates correctly", "example.com" in t2.url)

tab_ids = b.tab_ids
check("tab_ids returns list", len(tab_ids) == 2)

tabs = b.get_tabs()
check("get_tabs returns all tabs", len(tabs) == 2)
for tab in tabs:
    check(f"tab url exists: {tab.url[:50]}", bool(tab.url))

t2.close()
check("close() reduces tab count", len(b.tab_ids) == 1)

retrieved = b.get_tab(tab_ids[0])
check("get_tab by id works", retrieved is not None)

# ── Test 3: Navigation ──
print("\n=== Test 3: Navigation ===")
t.get("https://httpbin.org/html")
t.wait.doc_loaded()
check("navigate + wait.doc_loaded", "httpbin" in t.url)

# ── Test 4: JS execution ──
print("\n=== Test 4: JS execution ===")
r = t.run_js("return 42")
check("run_js returns value", r == 42)

r2 = t.run_js("return arguments[0] + arguments[1]", "hello", "world")
check("run_js with arguments[]", r2 == "helloworld")

r3 = t.run_js("return !!document.body")
check("run_js document.body exists", r3 is True)

big_js = """
function add(a, b) { return a + b; }
function multiply(x, y) { return x * y; }
return add(multiply(3, 4), 5);
"""
r4 = t.run_js(big_js)
check("run_js with function definitions", r4 == 17)

# ── Test 5: Element finding ──
print("\n=== Test 5: Element finding ===")
t.get("https://httpbin.org/forms/post")
t.wait.doc_loaded()

el = t.ele("@name=custname")
check("ele(@name=) finds element", el is not None)

els = t.eles("tag:input")
check("eles(tag:) finds multiple", len(els) >= 1)

el2 = t.ele("xpath://input[@name='custname']")
check("ele(xpath://) finds element", el2 is not None)

# ── Test 6: Element properties ──
print("\n=== Test 6: Element properties ===")
if el:
    attr = el.attr("name")
    check("attr() returns value", attr == "custname" or attr == "custname")

# ── Test 7: Element click ──
print("\n=== Test 7: Element click ===")
t.get("https://httpbin.org/links/10")
t.wait.doc_loaded()
link = t.ele("tag:a")
if link:
    link.click(by_js=True)
    time.sleep(0.5)
    check("click(by_js=True) navigates", "0" in t.url.split("/")[-1])

# ── Test 8: Shadow DOM ──
print("\n=== Test 8: Shadow DOM traversal ===")
t.run_js("""
document.body.innerHTML = '';
var d = document.createElement('div');
d.id = 'wrapper';
var sr = d.attachShadow({mode: 'open'});
sr.innerHTML = '<div id=inner>shadow-text</div>';
document.body.appendChild(d);
""")
time.sleep(0.3)

wrapper_el = t.ele("#wrapper")
check("host element found", wrapper_el is not None)
sr = wrapper_el.shadow_root
check("shadow_root property", sr is not None)
inner = sr.ele("tag:div")
check("shadow child found", inner is not None)
if inner:
    check("shadow child text", "shadow-text" in inner.text)

# ── Test 9: Shadow DOM + iframe ──
print("\n=== Test 9: Shadow DOM + iframe (Turnstile pattern) ===")
t.run_js("""
document.body.innerHTML = '';
var w = document.createElement('div');
w.id = 'turnstile-wrap';
var sr = w.attachShadow({mode: 'open'});
sr.innerHTML = '<iframe id=cf-frame src=\"about:blank\"></iframe>';
// Input is nested inside wrapper so parent() finds shadow-host
var inp = document.createElement('input');
inp.name = 'cf-turnstile-response';
inp.value = '';
w.appendChild(inp);
document.body.appendChild(w);
""")
time.sleep(0.5)

challenge = t.ele("@name=cf-turnstile-response")
check("cf-turnstile-response found", challenge is not None)

wrapper = challenge.parent()
check("parent element found", wrapper is not None)

shadow = wrapper.shadow_root
check("wrapper.shadow_root", shadow is not None)

iframe_el = shadow.ele("tag:iframe")
check("iframe in shadow", iframe_el is not None)

if iframe_el:
    result = iframe_el.run_js("window.dtp = 1; return window.dtp")
    check("iframe.run_js works", result == 1)

# ── Test 10: Cookies ──
print("\n=== Test 10: Cookies ===")
t.get("https://httpbin.org/cookies")
t.set.cookies({"name": "adapter_test", "value": "ok", "domain": "httpbin.org", "path": "/"})
time.sleep(0.3)
cks = t.cookies()
check("page.cookies() returns list", isinstance(cks, list))
check("injected cookie present", any(c.get("name") == "adapter_test" for c in cks))

t.set.cookies.clear()
cks2 = t.cookies()
check("cookies.clear() works", not any(c.get("name") == "adapter_test" for c in cks2))

# ── Test 11: Browser cookies ──
print("\n=== Test 11: Browser cookies ===")
b.set.cookies({"name": "bulk", "value": "test", "domain": "httpbin.org", "path": "/"})
time.sleep(0.3)
bcks = b.cookies()
check("browser.cookies()", isinstance(bcks, list))
b.set.cookies.clear()

# ── Test 12: HTML content ──
print("\n=== Test 12: HTML content ===")
t.get("https://httpbin.org/html")
t.wait.doc_loaded()
html = t.html
check("page.html returns content", len(html) > 0 and "<" in html)

# ── Cleanup ──
b.quit()

# ── Summary ──
print(f"\n{'='*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAIL:
    print("SOME TESTS FAILED!")
    sys.exit(1)
else:
    print("ALL TESTS PASSED!")
    sys.exit(0)
