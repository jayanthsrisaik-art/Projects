from dupetrace.normalize import frame_key, mask_variables, parse_report_text, raw_tokens, tokens

PY = '''Traceback (most recent call last):
  File "/opt/service/releases/3.5.2/framework/handlers.py", line 91, in dispatch
    ...
  File "/opt/service/releases/3.5.2/checkout/cart.py", line 147, in apply_coupon
    ...
KeyError: 'currency_code'
'''

JS = '''Uncaught TypeError: Cannot read properties of undefined (reading 'facet_name')
    at parseFacet (/app/dist/search/filters.js:77:12)
    at applyFilters (/app/dist/search/query.js:31:5)
'''


def test_mask_variables_replaces_run_specific_values():
    s = mask_variables("user a.b@example.com req 6f1c1a2e-0d3b-4b8e-9a51-2d9f0e7c4a10 at 0x7ffd12 "
                       "file /var/tmp/abc/x.lock took 3000ms on 2026-01-02T03:04:05Z see https://x.io/a")
    assert "<email>" in s and "<uuid>" in s and "<hex>" in s and "<path>" in s and "<ts>" in s and "<url>" in s
    assert "example.com" not in s and "7ffd12" not in s


def test_python_traceback_parsed_innermost_last():
    p = parse_report_text(PY)
    assert p.exc_type == "KeyError"
    assert p.message == "'currency_code'"
    assert p.frames == ("framework/handlers:dispatch", "checkout/cart:apply_coupon")
    assert p.top_frame == "checkout/cart:apply_coupon"


def test_js_stack_reversed_to_innermost_last():
    p = parse_report_text(JS)
    assert p.exc_type == "TypeError"
    assert "facet_name" in p.message
    assert p.frames == ("search/query:applyFilters", "search/filters:parseFacet")


def test_frame_identity_ignores_install_prefix_and_line_numbers():
    a = parse_report_text(PY)
    b = parse_report_text(PY.replace("/opt/service/releases/3.5.2", "/srv/app").replace("147", "152"))
    assert a.frames == b.frames
    assert frame_key(r"C:\deploy\app\checkout\cart.py", "apply_coupon") == "checkout/cart:apply_coupon"


def test_message_without_trace_is_still_extracted():
    p = parse_report_text("The console shows `TimeoutError: sort_field request timed out after 22166ms`.")
    assert p.exc_type == "TimeoutError"
    assert p.message == "sort_field request timed out after <num>ms"
    assert not p.has_trace


def test_no_exception_found():
    p = parse_report_text("Dark mode toggle does nothing")
    assert p.exc_type is None and p.message == "" and p.frames == ()


def test_tokens_merge_inflections_and_split_identifiers():
    toks = tokens("Coupon fails; failing on currency_code")
    assert toks.count("fail") == 2
    assert {"currency_code", "currency", "code"} <= set(toks)
    assert "on" not in toks


def test_raw_tokens_keep_noise():
    assert "0x7ffd12" in raw_tokens("at 0x7ffd12")
