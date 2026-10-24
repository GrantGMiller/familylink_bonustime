def get_cookies_dict(cookie_string):
    splits = cookie_string.split(';')
    ret = {}
    for split in splits:
        k, v = split.split('=')
        ret[k.strip()] = v.strip()
    return ret