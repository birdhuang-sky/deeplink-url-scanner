from org_scanner.patterns import extract_params_from_line

def test_extract_basic():
    line = 'url.searchParams.get("utm_medium");'
    params = extract_params_from_line(line)
    assert "utm_medium" in params

def test_extract_java():
    line = 'uri.getQueryParameter("associate_id")'
    params = extract_params_from_line(line)
    assert "associate_id" in params

def test_literal():
    line = 'const x = "utm_campaign";'
    params = extract_params_from_line(line)
    assert "utm_campaign" in params