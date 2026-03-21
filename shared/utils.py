import re
from aiogram.types import MessageEntity


def truncate_markdown_v2_safe(text: str, max_len: int = 1024) -> str:
    """
    Truncate MarkdownV2 text so we don't cut inside a link or other entity.
    Telegram fails with "Can't find end of a URL" if we cut inside [text](url).
    """
    if not text or len(text) <= max_len:
        return text
    s = text[:max_len]
    if s.count("(") > s.count(")"):
        last_paren = s.rfind(")")
        if last_paren != -1:
            return s[: last_paren + 1]
        last_open = s.rfind("(")
        if last_open != -1:
            return s[:last_open]
    if s.count("[") > s.count("]"):
        last_bracket = s.rfind("]")
        if last_bracket != -1:
            return s[: last_bracket + 1]
        last_open = s.rfind("[")
        if last_open != -1:
            return s[:last_open]
    return s


# Telegram MarkdownV2 reserved characters (must be escaped with \). Backslash first so we don't double-escape.
_MARKDOWN_V2_RESERVED = (
    ("\\", "\\\\"),
    ("_", "\\_"), ("*", "\\*"), ("[", "\\["), ("]", "\\]"),
    ("(", "\\("), (")", "\\)"), ("~", "\\~"), ("`", "\\`"),
    (">", "\\>"), ("#", "\\#"), ("+", "\\+"), ("-", "\\-"),
    ("=", "\\="), ("|", "\\|"), ("{", "\\{"), ("}", "\\}"),
    (".", "\\."), ("!", "\\!"),
)


def _escape_markdown_v2_link_text(s: str) -> str:
    """Escape reserved characters in link text so MarkdownV2 accepts it inside [...](url)."""
    for char, escaped in _MARKDOWN_V2_RESERVED:
        s = s.replace(char, escaped)
    return s


def escape_markdown_v2_urls(text: str) -> str:
    r"""
    Escape MarkdownV2 reserved chars everywhere: in plain text and inside [link_text](url).
    In link text and plain text, all reserved chars (_*[]()~`>#+-=|{}.! and \) must be escaped.
    In URL only ')' and '\' must be escaped. Avoids "Character '(' is reserved" and URL parse errors.
    """
    if not text:
        return text
    pattern = re.compile(r'\[([^\]]*)\]\(([^)]*)\)')
    result_parts: list[str] = []
    last_end = 0
    for m in pattern.finditer(text):
        # Escape plain text before this link
        result_parts.append(_escape_markdown_v2_link_text(text[last_end : m.start()]))
        link_text, url = m.group(1), m.group(2)
        link_text_escaped = _escape_markdown_v2_link_text(link_text)
        url_escaped = url.replace("\\", "\\\\").replace(")", "\\)")
        result_parts.append(f"[{link_text_escaped}]({url_escaped})")
        last_end = m.end()
    # Escape remaining plain text after last link
    result_parts.append(_escape_markdown_v2_link_text(text[last_end:]))
    return "".join(result_parts)


def get_markdown_text(text: str, entities: list[MessageEntity]) -> str:
    """
    Converts text and entities into a Markdown string.
    """
    if not entities:
        return text

    insertions = []
    for e in entities:
        if e.type == 'bold':
            insertions.append((e.offset, '**'))
            insertions.append((e.offset + e.length, '**'))
        elif e.type == 'italic':
            insertions.append((e.offset, '*'))
            insertions.append((e.offset + e.length, '*'))
        elif e.type == 'underline':
            insertions.append((e.offset, '__'))
            insertions.append((e.offset + e.length, '__'))
        elif e.type == 'strikethrough':
            insertions.append((e.offset, '~~'))
            insertions.append((e.offset + e.length, '~~'))
        elif e.type == 'code':
            insertions.append((e.offset, '`'))
            insertions.append((e.offset + e.length, '`'))
        elif e.type == 'pre':
            insertions.append((e.offset, '```\n'))
            insertions.append((e.offset + e.length, '\n```'))
        elif e.type == 'blockquote':
            # For blockquotes, we ideally want > at the start of every line.
            # But simple insertion is hard. Let's just put > at the start and hope telegramify handles it 
            # or the LLM preserves it.
            insertions.append((e.offset, '> '))
            # No closing tag for blockquote in markdown usually, it ends with newline.
            # But we might have other text after.
            # Let's assume the blockquote is the whole line(s).
        elif e.type == 'spoiler':
            insertions.append((e.offset, '||'))
            insertions.append((e.offset + e.length, '||'))
        elif e.type == 'text_link':
            insertions.append((e.offset, '['))
            insertions.append((e.offset + e.length, f']({e.url})'))
            
    # UTF-16 handling for correct offsets
    utf16_text = text.encode('utf-16-le')
    
    # Sort insertions by offset descending
    insertions.sort(key=lambda x: x[0], reverse=True)
    
    res_text = utf16_text
    
    for offset, tag in insertions:
        byte_offset = offset * 2
        res_text = res_text[:byte_offset] + tag.encode('utf-16-le') + res_text[byte_offset:]
        
    return res_text.decode('utf-16-le')


# Max terms / length per term to keep LLM prompts bounded
MAX_EXCLUDED_TERMS = 80
MAX_EXCLUDED_TERM_LEN = 200


def normalize_excluded_terms(raw: list) -> list[str]:
    """Trim, dedupe (first occurrence wins), drop empty; cap count and per-term length."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        if len(s) > MAX_EXCLUDED_TERM_LEN:
            s = s[:MAX_EXCLUDED_TERM_LEN]
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_EXCLUDED_TERMS:
            break
    return out

