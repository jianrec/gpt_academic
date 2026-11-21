import os
import re
import json
import requests
from loguru import logger

ARXIV_API = 'https://export.arxiv.org/api/query'

# 常见会议/期刊名，用于从 arXiv comment / journal_ref 中识别发表场所
VENUE_PATTERNS = [
    'NeurIPS', 'NIPS', 'ICML', 'ICLR', 'COLM', 'CVPR', 'ICCV', 'ECCV', 'WACV',
    'ACL', 'EMNLP', 'NAACL', 'EACL', 'COLING', 'AAAI', 'IJCAI', 'AAMAS',
    'VLDB', 'SIGMOD', 'KDD', 'WWW', 'WSDM', 'CIKM', 'SIGIR', 'RecSys', 'CSCW',
    'OSDI', 'SOSP', 'NSDI', 'EuroSys', 'ATC', 'MLSys', 'ASPLOS', 'ISCA',
    'Nature Mach\\. Intell\\.', 'Nature Communications', 'Nature', 'Science',
    'PNAS', 'JMLR', 'TMLR', 'PAMI',
]
# 命名时停用的虚词（仅用于无冒号标题的简称截取）
STOPWORDS = {'the', 'a', 'an', 'of', 'via', 'for', 'with', 'from', 'in', 'on',
             'and', 'to', 'by', 'at', 'using', 'toward', 'towards', 'its'}


def _clean_title_text(text):
    # 去掉 LaTeX 数学与花括号，规整空白
    text = text.replace('\n', ' ')
    text = re.sub(r'\$[^$]*\$', lambda m: m.group(0).replace('$', ''), text)
    text = re.sub(r'[{}$^\\]', '', text)
    return ' '.join(text.split()).strip()


def derive_short_name(title):
    """
    论文简称：优先取标题冒号前的部分（如 "SnapKV: ..." -> "SnapKV"）；
    无冒号时取前几个实义词（如 "Fast KV Compaction via ..." -> "Fast-KV-Compaction"）。
    """
    title = _clean_title_text(title)
    if not title:
        return None
    if ':' in title:
        short = title.split(':')[0].strip()
    else:
        words = [w for w in title.split() if w.lower() not in STOPWORDS]
        short = '-'.join(words[:3])
    # 去掉文件系统非法字符与下划线（下划线用作命名分隔符），限制长度
    short = re.sub(r'[\\/:*?"<>|\s_]+', '-', short).strip('-. ')
    if short and short.split('-')[0].islower():
        short = short[0].upper() + short[1:]  # 首词全小写时首字母大写，如 risk -> Risk；xKV 等品牌写法保持原样
    return short[:30] if short else None


def derive_venue(comment, journal_ref):
    """从 comment / journal_ref 中提取会议或期刊名（含年份，如 ICLR2024、VLDB2026）"""
    for source in (comment or '', journal_ref or ''):
        for pat in VENUE_PATTERNS:
            m = re.search(rf'\b({pat})\b[^\w]*(\d{{4}})?', source)
            if m:
                venue = re.sub(r'\\', '', m.group(1))
                if re.fullmatch(r'NIPS', venue): venue = 'NeurIPS'
                return f"{venue}{m.group(2) or ''}"
    return None


def _fetch_meta_from_abs_page(arxiv_id, timeout=20):
    """API 被限流(429)或不可用时，从 arXiv abs 网页兜底抓取标题/日期/comment"""
    r = requests.get(f'https://arxiv.org/abs/{arxiv_id}', timeout=timeout,
                     headers={'User-Agent': 'Mozilla/5.0'})
    if r.status_code != 200:
        return None
    html = r.text
    title = re.search(r'<meta name="citation_title" content="([^"]+)"', html)
    if not title:
        return None
    date = re.search(r'<meta name="citation_date" content="([^"]+)"', html)
    published = date.group(1).replace('/', '-') if date else ''
    comment, journal_ref = '', ''
    for pat in (r'<td class="tablecell comments[^"]*">(.*?)</td>',
                r'<td class="tablecell mcell comments[^"]*">(.*?)</td>'):
        m = re.search(pat, html, re.DOTALL)
        if m:
            comment = ' '.join(re.sub(r'<[^>]+>', ' ', m.group(1)).split())
            break
    m = re.search(r'<td class="tablecell jref">(.*?)</td>', html, re.DOTALL)
    if m:
        journal_ref = ' '.join(re.sub(r'<[^>]+>', ' ', m.group(1)).split())
    return {'arxiv_id': arxiv_id, 'title': title.group(1), 'published': published,
            'comment': comment, 'journal_ref': journal_ref}


def fetch_arxiv_metadata(arxiv_id, cache_dir=None, timeout=20):
    """
    拉取 arXiv 论文元数据（标题、发布时间、会议信息），本地缓存到
    gpt_log/arxiv_cache/<id>/meta.json，网络失败时回退缓存。
    返回 dict（含 short_name / time_str / venue）或 None。
    """
    arxiv_id = str(arxiv_id).strip()
    if cache_dir is None:
        from toolbox import get_conf
        cache_dir = os.path.join(get_conf('PATH_LOGGING'), 'arxiv_cache')
    meta_path = os.path.join(cache_dir, arxiv_id, 'meta.json')

    cached = None
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
        except Exception:
            pass

    try:
        resp = requests.get(f'{ARXIV_API}?id_list={arxiv_id}&max_results=1', timeout=timeout)
        resp.raise_for_status()
        ns = {'a': 'http://www.w3.org/2005/Atom', 'ar': 'http://arxiv.org/schemas/atom'}
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        entry = root.find('a:entry', ns)
        if entry is None or entry.find('a:title', ns) is None:
            raise RuntimeError('arXiv API 返回空条目')
        title = _clean_title_text(entry.find('a:title', ns).text or '')
        published = (entry.find('a:published', ns).text or '')[:10]
        comment_el = entry.find('ar:comment', ns)
        comment = ' '.join(comment_el.text.split()) if comment_el is not None and comment_el.text else ''
        jref_el = entry.find('ar:journal_ref', ns)
        journal_ref = ' '.join(jref_el.text.split()) if jref_el is not None and jref_el.text else ''
        meta = {
            'arxiv_id': arxiv_id,
            'title': title,
            'published': published,
            'comment': comment,
            'journal_ref': journal_ref,
        }
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        cached = meta
    except Exception as e:
        logger.info(f'拉取arXiv元数据失败({arxiv_id})，尝试网页兜底: {e}')

    if cached is None:
        # API 限流(429)或不可用时，从 abs 网页兜底抓取
        try:
            cached = _fetch_meta_from_abs_page(arxiv_id, timeout=timeout)
            if cached is not None:
                os.makedirs(os.path.dirname(meta_path), exist_ok=True)
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(cached, f, ensure_ascii=False, indent=2)
                logger.info(f'已通过网页兜底获取arXiv元数据: {arxiv_id}')
        except Exception as e:
            logger.info(f'网页兜底失败({arxiv_id}): {e}')

    if cached is None:
        return None
    cached.setdefault('title', '')
    cached.setdefault('published', '')
    cached['short_name'] = derive_short_name(cached['title'])
    cached['venue'] = derive_venue(cached.get('comment', ''), cached.get('journal_ref', ''))
    cached['time_str'] = cached['published'][:7].replace('-', '') if cached['published'] else None  # 如 202404
    return cached


def build_translation_stem(meta, arxiv_id):
    """生成译文文件名主干：简称_时间_会议（缺失部分自动跳过）"""
    parts = []
    if meta:
        if meta.get('short_name'): parts.append(meta['short_name'])
        if meta.get('time_str'): parts.append(meta['time_str'])
        if meta.get('venue'): parts.append(meta['venue'])
    if not parts:
        parts = [arxiv_id]
    return '_'.join(parts)


def stem_from_pdf_title(pdf_path):
    """
    上传PDF翻译（路径中无arxiv编号）时的命名兜底：
    从译文PDF首页提取英文标题，先按标题检索arXiv补全元数据命名；
    检索不到则直接用标题简称，避免退化为时间戳命名。
    """
    try:
        title = None
        # 优先从同目录tex提取\title（比PDF首页可靠，首页可能带会议proceedings横幅）
        tex_dir = os.path.dirname(pdf_path)
        for tex_name in ('merge_translate_zh.tex', 'main.tex'):
            tex_path = os.path.join(tex_dir, tex_name)
            if os.path.exists(tex_path):
                with open(tex_path, encoding='utf-8', errors='replace') as f:
                    m = re.search(r'\\title\{([^{}]+)\}', f.read())
                if m:
                    title = re.sub(r'\s+', ' ', m.group(1)).strip()
                    break
        if not title:
            import fitz
            doc = fitz.open(pdf_path)
            lines = [l.strip() for l in doc[0].get_text().split('\n') if l.strip()]
            doc.close()
            banner = re.compile(r'(?i)(this paper is included|proceedings of the|isbn|copyright|©|usenix|advanced computing|conference on|santa clara|open access|sponsored|https?://|^(january|february|march|april|may|june|july|august|september|october|november|december)\\s)')
            lines = [l for l in lines if not banner.search(l)]
            if not lines:
                return None
            title = re.sub(r'\s+', ' ', ' '.join(lines[:2])).strip()
        if len(title) < 8:
            return None
        try:
            r = requests.get(ARXIV_API, params={'search_query': f'ti:"{title}"', 'max_results': 1}, timeout=20)
            if r.status_code == 200:
                import xml.etree.ElementTree as ET
                ns = {'a': 'http://www.w3.org/2005/Atom'}
                entry = ET.fromstring(r.text).find('a:entry', ns)
                if entry is not None and entry.find('a:title', ns) is not None:
                    aid = entry.find('a:id', ns).text.split('/abs/')[-1].split('v')[0]
                    meta = fetch_arxiv_metadata(aid)
                    if meta:
                        return build_translation_stem(meta, aid)
        except Exception:
            pass
        # 不在arXiv上：从会议proceedings横幅提取会议与日期（如 USENIX FAST'26）
        venue, time_str = _parse_proceedings_banner(pdf_path)
        parts = [derive_short_name(title), time_str, venue]
        return '_'.join(p for p in parts if p) or None
    except Exception:
        return None


MONTHS = {m: i + 1 for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June',
     'July', 'August', 'September', 'October', 'November', 'December'])}


def _parse_proceedings_banner(pdf_path):
    """从PDF首页的会议横幅提取 (venue, time_str)，如 FAST2026 / 202602"""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = doc[0].get_text()
        doc.close()
        venue, time_str = None, None
        m = re.search(r'usenix\.org/conference/([a-z]+)(\d{2,4})', text)
        if m:
            year = m.group(2) if len(m.group(2)) == 4 else '20' + m.group(2)
            venue = m.group(1).upper() + year
        if not venue:
            m = re.search(r"\(([A-Z]{2,10})\s*['’]?(\d{2,4})\)", text)
            if m:
                year = m.group(2) if len(m.group(2)) == 4 else '20' + m.group(2)
                venue = m.group(1) + year
        m = re.search(r'(' + '|'.join(MONTHS) + r')\s+\d{1,2}(?:[–\-—]\d{1,2})?,?\s+(\d{4})', text)
        if m:
            time_str = m.group(2) + f"{MONTHS[m.group(1)]:02d}"
        return venue, time_str
    except Exception:
        return None, None
