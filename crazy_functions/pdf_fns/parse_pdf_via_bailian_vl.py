# -*- coding: utf-8 -*-
"""
百炼视觉解析：把PDF逐页渲染成图片，调用百炼兼容端点的视觉模型（默认 qwen3-vl-plus）
转写为LaTeX片段，拼装成tex项目。用于没有 DOC2X/MATHPIX key 时的PDF翻译兜底，
直接复用 config 中已有的百炼 API_KEY 与 API_URL_REDIRECT。
"""
import os
import re
import base64
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

pj = os.path.join

PAGE_PROMPT = (
    "You are a professional academic paper LaTeX transcription engine. "
    "Convert this paper page image into LaTeX source code (body fragment only: "
    "no \\documentclass, no \\usepackage, no \\begin{document}/\\end{document}). "
    "Rules: keep the original English (do NOT translate); preserve all math as LaTeX; "
    "preserve tables with tabular/booktabs; preserve headings with \\section/\\subsection; "
    "preserve \\title/\\author/\\maketitle if present; keep citations as \\cite{readablekey}; "
    "do NOT emit \\includegraphics (represent figures as a LaTeX comment instead); "
    "ignore page numbers, running headers and footers; output ONLY the LaTeX code."
)


def _strip_preamble(tex):
    lines = []
    for line in tex.split('\n'):
        s = line.strip()
        if s.startswith(('\\documentclass', '\\usepackage', '\\begin{document}', '\\end{document}')):
            continue
        lines.append(line)
    return '\n'.join(lines)


def _page_to_tex(job):
    idx, img_b64, endpoint, api_key, model = job
    for attempt in range(2):
        try:
            r = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                        {"type": "text", "text": PAGE_PROMPT},
                    ]}],
                    "max_tokens": 16000,
                    "temperature": 0.1,
                },
                timeout=180,
            )
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"].strip()
                m = re.match(r'^```(?:latex|tex)?\s*\n?(.*?)\n?```$', txt, re.DOTALL)
                if m:
                    txt = m.group(1).strip()
                return idx, _strip_preamble(txt)
            logger.info(f"百炼VL解析第{idx+1}页返回 {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.info(f"百炼VL解析第{idx+1}页异常(重试{attempt+1}): {e}")
        time.sleep(3)
    return idx, None


def 解析PDF_百炼VL_转Latex(pdf_file_path, max_workers=4):
    import fitz
    from toolbox import get_conf, get_log_folder, gen_time_str

    api_key = get_conf("API_KEY")
    redirect = get_conf("API_URL_REDIRECT")
    endpoint = redirect.get("https://api.openai.com/v1/chat/completions",
                            "https://api.openai.com/v1/chat/completions")
    model = get_conf("PDF_VL_MODEL")

    doc = fitz.open(pdf_file_path)
    jobs, plain_texts = [], []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=150)
        jobs.append((i, base64.b64encode(pix.tobytes("png")).decode(), endpoint, api_key, model))
        plain_texts.append(doc[i].get_text())
    n_pages = len(doc)
    doc.close()
    logger.info(f"百炼VL解析: {n_pages} 页, 模型 {model}, 并发 {max_workers}")

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for idx, tex in ex.map(_page_to_tex, jobs):
            results[idx] = tex

    parts = []
    failed = 0
    for i in range(n_pages):
        tex = results.get(i)
        if tex:
            parts.append(tex)
        else:
            failed += 1
            # 单页失败时用纯文本兜底，保证项目仍有内容
            escaped = plain_texts[i].replace('&', '\\&').replace('%', '\\%').replace('#', '\\#')
            parts.append(f"% page {i+1}: VL conversion failed, plain text fallback\n{escaped}")
    if failed:
        logger.info(f"百炼VL解析: {failed} 页失败，已用纯文本兜底")

    preamble = (
        "\\documentclass[10pt]{article}\n"
        "\\usepackage[utf8]{inputenc}\n"
        "\\usepackage{amsmath,amssymb,amsfonts}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{booktabs}\n"
        "\\usepackage{hyperref}\n"
        "\\usepackage[margin=1in]{geometry}\n"
        "\\begin{document}\n"
    )
    out_dir = pj(get_log_folder(plugin_name="pdf_vl_latex"), gen_time_str())
    os.makedirs(out_dir, exist_ok=True)
    with open(pj(out_dir, "main.tex"), "w", encoding="utf-8") as f:
        f.write(preamble + "\n\n".join(parts) + "\n\\end{document}\n")
    logger.info(f"百炼VL解析完成: {out_dir}")
    return out_dir
