"""Post-processing for transcribed text: filler removal and optional LLM refinement."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Chinese filler words and verbal tics
ZH_FILLERS = [
    "嗯", "呃", "啊", "哦", "噢", "唔", "额",
    "那个", "就是说", "怎么说呢",
    "对对对", "对对", "是是是", "是是",
    "好好好", "好好",
]

# English filler words
EN_FILLERS = [
    "um", "uh", "uhm", "hmm", "hm",
    "like", "you know", "I mean",
    "so basically", "basically",
]

_zh_pattern = None
_en_pattern = None


def _get_zh_pattern():
    global _zh_pattern
    if _zh_pattern is None:
        # Single-char fillers: remove anywhere, with surrounding punctuation
        singles = [re.escape(f) for f in ZH_FILLERS if len(f) == 1]
        # Multi-char fillers: remove as phrases
        multis = [re.escape(f) for f in sorted(ZH_FILLERS, key=len, reverse=True) if len(f) > 1]

        parts = []
        # Single-char fillers with optional surrounding punctuation
        if singles:
            parts.append(r'[，。、；：！？\s]*(?:' + '|'.join(singles) + r')[，。、；：！？\s]*')
        # Multi-char filler phrases with optional trailing punctuation
        if multis:
            parts.append(r'(?:' + '|'.join(multis) + r')[，。、；：！？\s]*')

        _zh_pattern = re.compile('|'.join(parts))
    return _zh_pattern


def _get_en_pattern():
    global _en_pattern
    if _en_pattern is None:
        escaped = [re.escape(f) for f in sorted(EN_FILLERS, key=len, reverse=True)]
        _en_pattern = re.compile(
            r'\b(?:' + '|'.join(escaped) + r')\b'
            r'[,.\s]*',
            re.IGNORECASE
        )
    return _en_pattern


def remove_fillers(text: str, language: str = "zh") -> str:
    """Remove filler words from transcribed text.

    Handles both Chinese and English fillers regardless of language setting,
    since bilingual speech is common.
    """
    if not text:
        return text

    original = text

    # Remove Chinese fillers
    text = _get_zh_pattern().sub('', text)

    # Remove English fillers
    text = _get_en_pattern().sub('', text)

    # Clean up artifacts: repeated punctuation, leading punctuation, extra spaces
    text = re.sub(r'[，。、]{2,}', '。', text)  # collapse repeated punctuation
    text = re.sub(r'^\s*[，。、；：]\s*', '', text)  # remove leading punctuation
    text = re.sub(r'\s+', ' ', text).strip()  # normalize whitespace

    # Merge short sentences (under 6 chars) into previous sentence
    text = merge_short_sentences(text)

    if text != original:
        removed = len(original) - len(text)
        logger.debug(f"Fillers removed: {removed} chars stripped")

    return text


def merge_short_sentences(text: str) -> str:
    """Merge very short sentences into neighbors.

    "你好。世界。这是一段。测试。" → "你好世界，这是一段测试。"
    """
    if not text:
        return text

    # Split on Chinese sentence-ending punctuation
    parts = re.split(r'([。！？])', text)

    merged = []
    buffer = ""
    for i in range(0, len(parts), 2):
        segment = parts[i].strip()
        punct = parts[i + 1] if i + 1 < len(parts) else ""

        if not segment:
            continue

        if len(segment) <= 5 and buffer:
            # Short segment: merge with buffer using comma
            buffer += "，" + segment
        elif buffer and len(segment) <= 5:
            buffer += "，" + segment
        else:
            if buffer:
                merged.append(buffer)
            buffer = segment

        # If this segment is long enough or it's the last one, flush with its punctuation
        if len(buffer) > 10 or i + 2 >= len(parts):
            if punct:
                merged.append(buffer + punct)
            else:
                merged.append(buffer)
            buffer = ""

    if buffer:
        merged.append(buffer + "。")

    result = "".join(merged)
    # Clean up double punctuation
    result = re.sub(r'[，。]{2,}', '。', result)
    return result


def refine_with_llm(text: str, endpoint: str, model: str = "",
                    timeout: int = 10) -> str:
    """Optional: refine transcribed text using a local LLM.

    Sends text to a local OpenAI-compatible API endpoint for cleanup:
    punctuation, grammar, formatting.

    Args:
        text: raw transcribed text (fillers already removed)
        endpoint: e.g. "http://localhost:11434/v1" (Ollama)
        model: model name, e.g. "qwen3:8b"
        timeout: request timeout in seconds
    """
    if not endpoint or not text:
        return text

    import json
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    prompt = (
        "请润色以下语音转录文本。要求：\n"
        "1. 修正标点符号\n"
        "2. 修正明显的语音识别错误\n"
        "3. 保持原意，不要添加或删除内容\n"
        "4. 英文专有名词保持原样\n"
        "5. 只返回润色后的文本，不要解释\n\n"
        f"原文：{text}"
    )

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": len(text) * 3,
    }).encode()

    try:
        url = f"{endpoint.rstrip('/')}/chat/completions"
        req = Request(url, data=body, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        refined = data["choices"][0]["message"]["content"].strip()
        if refined:
            logger.info(f"LLM refined: {len(text)} → {len(refined)} chars")
            return refined
    except Exception as e:
        logger.warning(f"LLM refinement failed ({e}), using original text")

    return text
