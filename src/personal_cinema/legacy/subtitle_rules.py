"""Content-based language labels and player-compatible external subtitle names."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
from catalog_intelligence import simplified

def decode(data):
    for encoding in ('utf-8-sig', 'utf-16', 'gb18030', 'cp932', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    return data.decode('utf-8', 'replace')

def classify(data, name=''):
    text = decode(data)
    lines = re.findall('^Dialogue\\s*:(.*)$', text, re.M | re.I)
    if lines:
        text = '\n'.join((line.split(',', 9)[-1] for line in lines))
    text = re.sub('\\{[^}]*\\}|<[^>]*>|\\d{1,2}:\\d\\d:\\d\\d[,.]\\d+|\\\\[Nnh]', ' ', text)
    han = len(re.findall('[㐀-鿿]', text))
    kana = len(re.findall('[\u3040-ヿ]', text))
    signals = len(re.findall('[这這们們个個说說吗嗎么麼让讓给給还還]|我们|我們|你们|你們|什么|什麼|这样|這樣|就是|不是|可以|不要|因为|因為', text))
    english = len(re.findall('\\b[A-Za-z]{3,}\\b', text))
    zh = han >= 10 and (signals >= 3 or kana < 10)
    ja = kana >= 10
    en = english >= 20 and sum((len(w) for w in re.findall('[A-Za-z]+', text))) > max(60, 0.12 * len(text))
    changed = sum((a != b for a, b in zip(text, simplified(text))))
    traditional = zh and changed >= max(3, han * 0.015)
    label = ('繁体中文' if traditional else '简体中文') if zh else '日文' if ja else '英文' if en else '未识别字幕'
    if zh and en:
        label = '繁英双语' if traditional else '简英双语'
    elif zh and ja:
        label = '中日双语'
    elif ja and en:
        label = '日英双语'
    language = 'chi' if zh else 'jpn' if ja else 'eng' if en else 'und'
    secondary = bool(re.search('forced|signs?|songs?|commentary|仅注释|僅註釋|注释|註釋|次级|次級|特效字幕', name, re.I))
    hearing = bool(re.search('\\bsdh\\b|听障|聽障|字幕组CC', name, re.I))
    return {'label': label + ('·次级注释' if secondary else '·听障' if hearing else ''), 'language': language, 'traditional': traditional, 'bilingual': bool(zh and (en or ja)), 'secondary': secondary, 'hearing_impaired': hearing, 'recognized': language != 'und'}

def suffix(info, extension, default=False):
    flags = '.forced' if info['secondary'] else '.sdh' if info['hearing_impaired'] else ''
    if default and info['language'] == 'chi' and (not info['secondary']):
        flags += '.default'
    return '.' + info['label'] + '.' + info['language'] + flags + extension.lower()
